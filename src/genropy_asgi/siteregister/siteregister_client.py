# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyRegisterClient — the in-process register for legacy GenroPy, on the core worker.

The GenroPy ``GnrWsgiSite`` talks to a register through ``site.register``, calling one
command at a time while it serves a request. Historically that register was a daemon
(Pyro4, then the genro-nodaemon TCP daemon); here it is the DAEMONLESS register,
standalone, and every command the legacy calls is an EXPLICIT public method with its
own body and a docstring saying who calls it and when. No ``__getattr__`` magic, no
per-string dispatch table: what the register serves is exactly the set of methods below.

The register reaches its worker as ``site.spa_worker`` — a
:class:`~genropy_asgi.spa.genropy_worker.GenropyWorker`, the core ``UserStickyWorker``
hosting this very site — and calls its op methods DIRECTLY: the ops are sync, take
``dispatch_lock``, and the call runs on the worker's http_pool thread, where the CALL
sinks are open by context copy. There is no fold, no event sink read from the environ:
the op itself announces what it did on the CALL that caused it.

Who serves what (FIXED):
- Lifecycle — ``new_connection``/``new_page``/``change_connection_user``/``drop_page``/
  ``drop_connection`` call the worker's homonymous ops. LOGIN STAYS: the login is a
  local re-label on the live connection row (core 0.29) — nothing ships, and the WSGI
  request keeps finding its pages on this worker to the end.
- Reads — answered from the worker's registers directly (``page_items.get`` etc.);
  the ``_filter_items`` grammar stays client-local (the single sees everything).
- Datachanges — ``set_datachange``/``setInClientData`` build the genro-bag change dict
  and call ``worker.set_datachange`` with the TYTX-encoded parcel; a page address is a
  SIGNAL deposit on that page's collector, ``register_name='user'`` is a STATE write on
  the user's live store, found by every subscribed page in its own ``user_view``.
  ``subscribeTable``/``notifyDbEvents``/``setStoreSubscription`` map to the homonymous
  ops (``client_path`` becomes ``prefix``).
- The pull — ``subscription_storechanges``/``handle_ping`` drain via
  ``worker.collect_page`` and rebuild the legacy envelope. The ``dbevents`` species is
  DRESSED at delivery as datachanges on ``gnr.dbchanges.<table>``: the disguise is the
  bridge's, at the envelope — the core keeps the species separate.
- Stores — each row's ``data`` IS its live ``store`` (one legacy Bag, the object the
  collectors watch and a move would package); ``ServerStore`` locks the item and
  reads/writes it in-process. The legacy ``data`` seed of ``new_page`` becomes the
  page's store, so channel-A writes, the dbenv walk and the capture all see one Bag.
- Global store — the ONLY copy lives on the commander (owner design, 2026-08-21).
  Writes: one stable legacy Bag (``global_bag``), write-by-reference — every LEAF
  write ships up as ``store_set``/``store_del`` with a FULL-PATH key and a
  TYTX-encoded SCALAR value. Reads: a lock-less ``globalStore().getItem(path)``
  PAYS one ``store_get`` CALL and answers the master at the moment it was asked —
  no local copy that ages. A read-modify-write block holds the real lease, whose
  grant materializes the master snapshot into ``global_bag`` for the block's
  duration (``_materialize_global_snapshot``).

NOT served (explicit, PROVISIONAL): dump/load (future Service Store),
sendProcessCommand/pendingProcessCommands (inter-process bus, will move to the
commander), the daemon-only admin browser. They answer as documented no-ops.

Wiring: this class IS the ``SiteRegisterClient`` the legacy imports as
``gnr.web.daemon.siteregister_client`` (the ``siteregister`` submodule provides the
``gnr.web:daemon`` entry-point), so the ``GnrWsgiSite`` builds it directly at
``site.register`` — no daemon connection at construction, no rebind. Extra state is
created lazily through ``__dict__`` (the site touches ``self.register`` before the
worker has attached itself via ``site.spa_worker``).
"""

from __future__ import annotations

import copy
import datetime
import re
import threading
import time
from typing import Any

from genro_bag import Bag as CoreBag
from genro_tytx import from_tytx, to_tytx
from gnr.core.gnrbag import Bag
from gnr.core.gnrclasses import GnrClassCatalog
from gnr.web import logger
from gnr.web.gnrwebpage import ClientDataChange

from .exceptions import GnrDaemonLocked

# Lock retry budget for a ServerStore context (in-process contention is rare and short).
LOCK_MAX_RETRY = 50
RETRY_DELAY = 0.05
RETRY_DELAY_MAX = 2.0

# The 5-second window the daemon used for the runningBatch flag in the ping envelope.
RUNNING_BATCH_WINDOW = 5.0

# The site-facing answer for each register kind: exactly the fields the daemon's
# own registers put on a register item — ``ConnectionRegister.create``, ``UserRegister.create``,
# ``PageRegister.create`` in ``gnr/web/daemon/siteregister.py`` — plus the three
# queue fields ``BaseRegister.addRegisterItem`` puts on all three, added by
# ``_adapt_to_legacy`` itself. What the core keeps besides these is its own
# bookkeeping (the store, the three clocks the expiry sweep reads, the
# collectors, the page tree) and never reaches the site.
LEGACY_REGISTER_ITEM_FIELDS = {
    "connection": ("register_item_id", "start_ts", "connection_name", "user", "user_id",
                   "user_name", "user_tags", "user_ip", "user_agent", "electron_static",
                   "browser_name", "pages", "avatar_extra"),
    "user": ("register_item_id", "start_ts", "user", "user_id", "user_name", "user_tags",
             "avatar_extra", "connections"),
    "page": ("register_item_id", "pagename", "connection_id", "start_ts",
             "subscribed_tables", "user", "user_ip", "user_agent", "relative_url"),
}



class ServerStore:
    """Context manager over one register item: lock on enter, unlock on exit.

    The legacy ``pageStore``/``userStore``/``connectionStore``/``globalStore`` return one
    of these. ``__enter__`` acquires the item lock (retrying with capped backoff),
    ``__exit__`` releases it; the datachange methods and the Bag delegation on ``data``
    go straight to the register client, which serves them in-process. Same shape as the
    daemon's ServerStore, without the network.
    """

    def __init__(
        self,
        parent: Any,
        register_name: str | None = None,
        register_item_id: Any = None,
        triggered: bool = True,
    ) -> None:
        self.siteregister = parent
        self.register_name = register_name
        self.register_item_id = register_item_id
        self.triggered = triggered
        self.thread_id = threading.get_ident()

    def __enter__(self) -> ServerStore:
        if self.register_name == "global":
            # The REAL lease (develop == deploy): the block holds the master.
            self._lease = self.siteregister._open_global_lease()
            return self
        delay = RETRY_DELAY
        for attempt in range(LOCK_MAX_RETRY + 1):
            if self.siteregister.lock_item(
                self.register_item_id, reason=self.thread_id, register_name=self.register_name
            ):
                return self
            if attempt < LOCK_MAX_RETRY:
                time.sleep(delay)
                delay = min(delay * 2, RETRY_DELAY_MAX)
        raise GnrDaemonLocked(
            f"Lock timed out for {self.register_name!r} item {self.register_item_id!r}"
        )

    def __exit__(self, exc_type: Any, exc_value: Any, tb: Any) -> None:
        if self.register_name == "global":
            lease = self.__dict__.pop("_lease", None)
            if lease is not None:
                self.siteregister._close_global_lease(lease, exc_type)
            return
        self.siteregister.unlock_item(
            self.register_item_id, reason=self.thread_id, register_name=self.register_name
        )

    @property
    def datachanges(self) -> list:
        """Peek at the item's pending changes WITHOUT consuming them.

        Served from the collector peek (``drain(reset=False)``), returning legacy
        ``ClientDataChange`` objects — the read a serverbatch makes between its own
        writes, which the daemon-era store answered with an empty list (the latent
        serverbatch defect this property heals). Only a page has collectors; any
        other register answers empty.
        """
        return self.siteregister._pending_datachanges(
            self.register_item_id, register_name=self.register_name
        )

    @property
    def subscribed_paths(self) -> set:
        """The prefixes this page's own capture watches (empty off the page register)."""
        return self.siteregister._item_subscribed_paths(
            self.register_item_id, register_name=self.register_name
        )

    def reset_datachanges(self) -> Any:
        return self.siteregister.reset_datachanges(
            self.register_item_id, register_name=self.register_name
        )

    def set_datachange(
        self,
        path: str,
        value: Any = None,
        attributes: Any = None,
        fired: bool = False,
        reason: Any = None,
        replace: bool = False,
        delete: bool = False,
    ) -> Any:
        return self.siteregister.set_datachange(
            self.register_item_id, path, value=value, attributes=attributes, fired=fired,
            reason=reason, replace=replace, delete=delete, register_name=self.register_name,
        )

    def drop_datachanges(self, path: str) -> None:
        self.siteregister.drop_datachanges(
            self.register_item_id, path, register_name=self.register_name
        )

    def subscribe_path(self, path: str) -> None:
        self.siteregister.subscribe_path(
            self.register_item_id, path, register_name=self.register_name
        )

    @property
    def register_item(self) -> Any:
        return self.siteregister.get_item(
            self.register_item_id, include_data="lazy", register_name=self.register_name
        )

    @property
    def data(self) -> Any:
        item = self.register_item
        return item.get("data") if item else None

    def getItem(self, path: str, default: Any = None) -> Any:  # noqa: N802 - legacy Bag surface
        """Read one path — on the bare global store, a paid ``store_get`` CALL.

        A lock-less global read answers the commander's master at the moment it
        was asked (owner design, 2026-08-21): no local copy that ages. Inside a
        ``with`` block the lease already materialized the master locally, so the
        block reads (and sees its own writes) on the local Bag; every other
        register reads its in-process item as before.

        **What comes back is a copy.** The daemon answered this read over the
        wire, so what the site received was a pickle round-trip: a mutation
        applied to it never reached the register. In-process the live object
        would be handed out instead, and the same site line would write straight
        into the register item. Two site paths measured on 2026-08-25, both of
        which the bridge answered differently before this copy existed:
        ``WebPage._get_workdate`` (``gnrwebpage.py:541``) reads ``rootenv`` and
        assigns ``rootenv['workdate']`` into the Bag it read, and
        ``GnrApp.getAvatar`` (``gnrapp.py:1468``) POPS ``user_id``, ``user_name``
        and ``tags`` out of the dict the ``user_authenticate`` cache holds — so
        the second login of the same page found an avatar stripped of them and
        fell back to the username.
        """
        if self.register_name == "global" and "_lease" not in self.__dict__:
            return self.siteregister._global_read(path, default)
        data = self.data
        if data is None:
            return default
        return self._copied(data.getItem(path, default))

    def _copied(self, value: Any) -> Any:  # wf:phase-7:new
        """The value as the wire handed it over: nothing the site can write through.

        A Bag is rebuilt node by node, because ``Bag.deepcopy`` keeps a node's
        non-Bag value by reference and that reference is the whole defect. What
        sits under a node is copied when it is mutable — every value a store held
        on the legacy crossed a pickle, so anything in there is copyable.
        """
        if isinstance(value, Bag):
            copied = Bag()
            for node in value:
                copied.addItem(node.label, self._copied(node.getStaticValue()),
                               dict(node.getAttr()))
            return copied
        if isinstance(value, (dict, list, set)):
            return copy.deepcopy(value)
        return value

    def __getattr__(self, fname: str) -> Any:
        # Delegate Bag methods (getItem/setItem/...) to the item's data Bag.
        def decore(*args: Any, **kwargs: Any) -> Any:
            data = self.data
            if data is not None:
                return getattr(data, fname)(*args, **kwargs)
            return None

        return decore


class RemoteStoreBag:
    """Unused in-process (item ``data`` is a real local Bag); kept for import compat."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("RemoteStoreBag is not used by the in-process register")


class RegisterResolver:
    """Re-exported by the legacy shim (daemon admin browser); not available in-process."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("RegisterResolver is not available in-process")


class GenropyRegisterClient:
    """The in-process register: every ``site.register`` command is a method.

    Not a subclass of any daemon client. It holds the ``site`` and answers every
    command directly against the worker's op vocabulary and registers. A command the
    legacy might call that is not a method here would simply raise ``AttributeError``
    — the served set is exactly what is written below, on purpose.
    """

    # The exception the legacy catches around a store lock (touched on the client).
    locked_exception = GnrDaemonLocked

    def __init__(self, site: Any) -> None:
        """Built directly by the ``GnrWsgiSite`` at ``site.register``; no daemon here."""
        self.site = site

    # ------------------------------------------------------------------
    # Lazy state (the site touches self.register before the worker attaches)
    # ------------------------------------------------------------------

    @property
    def global_bag(self) -> Bag:
        """The in-process legacy Bag backing the global store (one stable object).

        ``get_item(register_name='global')`` hands it back on every call so a ``setItem``
        on it persists (write-by-reference), exactly as the daemon-backed store did.
        The Bag is subscribed to the global-store RAIL: every local leaf write ships
        up as a full-path scalar (``_on_global_change``). It is a WRITE vehicle, not
        a read cache: a lock-less read pays ``store_get`` (``_global_read``), and the
        only descent is the lease grant (``_materialize_global_snapshot``).
        """
        bag = self.__dict__.get("_global_bag")
        if bag is None:
            bag = Bag()
            self.__dict__["_global_bag"] = bag
            bag.subscribe("global_rail", any=self._on_global_change)
        return bag

    @property
    def _global_rail_state(self) -> threading.local:
        """Thread-local rail state: ``applying`` is True only on the thread that is
        materializing a commander push, so its Bag writes do not re-dispatch while
        legacy writes on other threads keep shipping."""
        state = self.__dict__.get("_global_rail_local")
        if state is None:
            state = threading.local()
            self.__dict__["_global_rail_local"] = state
        return state

    @property
    def catalog(self) -> GnrClassCatalog:
        """The typed-text catalog (parses the client's serverstore change values)."""
        catalog = self.__dict__.get("_catalog")
        if catalog is None:
            catalog = GnrClassCatalog()
            self.__dict__["_catalog"] = catalog
        return catalog

    @property
    def item_locks(self) -> dict:
        """(register_name, item_id) -> {"reason", "count"}: the in-process item locks."""
        locks = self.__dict__.get("_item_locks")
        if locks is None:
            locks = {}
            self.__dict__["_item_locks"] = locks
            self.__dict__["_locks_mutex"] = threading.Lock()
        return locks

    @property
    def locks_mutex(self) -> threading.Lock:
        self.item_locks  # ensure created
        return self.__dict__["_locks_mutex"]

    @property
    def spa_worker(self) -> Any:
        """The GenropyWorker hosting this site (set by its constructor), or None."""
        return getattr(self.site, "spa_worker", None)

    # ==================================================================
    # Lifecycle commands: direct calls onto the worker's op vocabulary
    # ==================================================================

    def new_connection(self, connection_id: Any, connection: Any = None, **kwargs: Any) -> dict | None:
        """A browser's first connection is born (login-less, guest included).

        Called by ``Connection.register`` (gnrwebpage_proxy/connection.py) the first time
        a browser is seen. In production the connection arrives already guest-named —
        ``Connection.create`` sets ``user = 'guest_<cid>'`` before registering
        (connection.py:91) — and the core recognizes the reserved prefix; with no user
        named, the core mints ``GUEST_PREFIX + cid`` itself (0.33, the name carries the
        rule). A connection the worker already holds is a re-registration (a browser
        re-presenting its cookie): the live row answers.
        Returns the local connection item with its data Bag attached.

        The row is born with ``start_ts`` — the daemon's own birth stamp
        (siteregister.py:322), which the core does not keep and the legacy reads
        without a guard.
        """
        worker = self.spa_worker
        if worker.connection_items.get(connection_id) is None:
            fields = self._conn_kwargs(connection, kwargs)
            fields["start_ts"] = datetime.datetime.now()
            worker.new_connection(connection_id, **fields)
        return self._item_with_data(connection_id, "connection")

    def new_page(self, page_id: Any, page: Any = None, **kwargs: Any) -> dict | None:
        """A new page (browser tab) opens.

        Called by ``WebPage._register_new_page`` (gnrwebpage.py) on page registration.
        The op is user-addressed (``new_page(user, page_id, connection_id=cid, ...)``);
        a page with no user named belongs to its connection's user — the name the core
        minted at the ``new_connection`` door (``guest_<cid>`` for an anonymous
        browser), read off the live row, never re-minted here: composing a guest name
        would mis-own the page when the connection has already logged in. The legacy
        ``data`` seed becomes the page's live STORE: one
        Bag serves the dbenv walk, the channel-A writes and the capture, and a move
        would package exactly it. Returns the local page item with that Bag as
        ``data``.

        The row is born with ``start_ts`` — the daemon's own birth stamp
        (siteregister.py:387), read without a guard by the async user tree
        (``gnrasync.registerPage``).
        """
        worker = self.spa_worker
        fields = self._page_kwargs(page, kwargs)
        user = fields.pop("user", None)
        connection_id = fields.pop("connection_id", None)
        if user is None:
            user = worker.connection_items.get(connection_id)["user"]
        data = fields.pop("data", None)
        if data is not None:
            fields["store"] = data
        fields["start_ts"] = datetime.datetime.now()
        worker.new_page(user, page_id=page_id, connection_id=connection_id, **fields)
        return self._item_with_data(page_id, "page")

    def change_connection_user(self, connection_id: Any, **kwargs: Any) -> dict | None:
        """A connection's user changes — LOGIN / avatar switch.

        Called by ``Connection.change_user`` (connection.py) at login. A LOCAL mutation
        on the live row (login-stays, core 0.29): nothing ships, the request keeps
        finding its pages here. Returns the updated local connection item.
        """
        worker = self.spa_worker
        user = kwargs.pop("user")
        worker.change_connection_user(connection_id, user=user, **kwargs)
        return self._item_with_data(connection_id, "connection")

    def drop_page(self, page_id: Any, cascade: bool = False, **kwargs: Any) -> None:
        """A page closes (client onClosePage, or a page flagged closed at end of RPC).

        Called by ``WebPage`` at RPC end when closed (gnrwebpage.py:624, with
        ``cascade=False``) and by ``onClosedPage`` (gnrwsgisite.py:1429, fired by
        the browser's pagehide beacon, passing nothing). A page close NEVER
        climbs to the connection — the tab is gone, the browser is still there
        with its cookie — so ``cascade`` defaults to the legacy False and is
        forwarded to the worker op. A page already gone — expired, reported
        closed twice, or demolished between this lock-free read and the op's
        own ``dispatch_lock`` (a pagehide beacon racing the sweep) — is a
        legitimate no-op: the owner is resolved with the tolerant walk, and
        the op's ``KeyError`` on a page that vanished meanwhile is the same
        no-op. That window is re-checked: a ``KeyError`` with the page row
        still present is not the race but a half-applied demolition, and it
        propagates.
        """
        worker = self.spa_worker
        user = self._page_owner(page_id)
        if user is None:
            return
        try:
            worker.drop_page(user, page_id=page_id, cascade=cascade)
        except KeyError:
            if worker.page_items.get(page_id) is not None:
                raise
            return

    def drop_connection(self, connection_id: Any, **kwargs: Any) -> None:
        """A connection ends — LOGOUT / browser gone.

        Called by ``Connection.unregister`` and ``rpc_logout`` (connection.py). The
        core op demolishes pages first, then the connection, then the user when it
        was its last one. A connection already gone is a legitimate no-op (a double
        logout). The legacy ``cascade`` kwarg is absorbed — the cascade is the core's.
        """
        worker = self.spa_worker
        if worker.connection_items.get(connection_id) is None:
            return
        worker.drop_connection(connection_id, connection_id=connection_id)

    def refresh(self, page_id: Any, ts: Any = None, lastRpc: Any = None, pageProfilers: Any = None) -> dict | None:
        """Bump the last-seen timestamps for a page, up through connection to user.

        The server stamp is ``worker.refresh_chain`` — the server's own clock, which a
        client value never touches; the client-reported clocks land as row fields
        (``last_user_ts``/``last_rpc_ts``). Returns the user item at the top of the
        chain (or None if the page is gone).
        """
        return self._local_refresh(page_id, last_user_ts=ts, last_rpc_ts=lastRpc)

    # ==================================================================
    # Read commands (answered from the worker's registers)
    # ==================================================================

    def get_item(self, register_item_id: Any, include_data: Any = False, register_name: Any = None) -> Any:
        """Return one register item by id (page/connection/user), or the global store.

        The read primitive the whole read side builds on. ``register_name='global'``
        returns the stable global Bag; ``include_data == 'lazy'`` attaches the item's
        in-process Bag — the item's own live store. What goes out is the LEGACY
        ANSWER (``_adapt_to_legacy``): the daemon's own field set, the live Bag, the
        daemon-era keys — never the core's own bookkeeping.
        """
        if register_name == "global":
            return {"register_item_id": "*", "register_name": "global", "data": self.global_bag}
        item = self.local_item(register_item_id, register_name)
        adapted = self._adapt_to_legacy(item, register_name=register_name)
        if adapted is not None and (include_data == "lazy" or include_data):
            adapted["data"] = self._ensure_item_data(item)["data"]
        return adapted

    def page(self, page_id: Any, include_data: Any = None) -> Any:
        """The local page item.

        Called on every RPC to validate the page and by the commit path (a hidden
        transaction reads ``page(page_id)['subscribed_tables']``). The subscriptions
        live on the page row itself (``table_subscriptions``) and the legacy view
        translates the name.
        """
        return self.get_item(page_id, include_data=include_data, register_name="page")

    def connection(self, connection_id: Any, include_data: Any = None) -> Any:
        """The local connection item. Called on every request to validate the cookie."""
        return self.get_item(connection_id, include_data=include_data, register_name="connection")

    def user(self, user: Any, include_data: Any = None) -> Any:
        """The local user item."""
        return self.get_item(user, include_data=include_data, register_name="user")

    def exists(self, register_item_id: Any, register_name: Any = None, **kwargs: Any) -> bool:
        """True if an item exists. Called by selections before operating on a page."""
        return self.local_item(register_item_id, register_name or "page") is not None

    def pages(self, connection_id: Any = None, user: Any = None, filters: Any = None, **kwargs: Any) -> dict:
        """Pages by connection and/or user, keyed by page_id (the ad-hoc filter grammar).

        Returns ``{register_item_id: item}`` — the daemon-client contract (``adaptListToDict``):
        the legacy does ``page_id in register.pages(...)`` (Connection.validate_page_id), so
        the keys must be the page ids. ``connection_id`` reads its own index;
        ``user`` walks the ownership edges (user -> connections -> pages). Called for the
        ``setInClientData`` broadcast (with filters) and by monitoring.

        Filtering happens on the live rows — a ``user`` clause needs the ownership
        walk, done with the tolerant ``_page_owner`` (a chain mid-demolition simply
        does not match) — and what goes out is the legacy view of each match. Every
        live edge set is snapshot (``list``) before iteration: the demolitions
        mutate them in place on other threads.
        """
        worker = self.spa_worker
        if worker is None:
            return {}
        page_items = worker.page_items
        if connection_id:
            items = self._live_rows(page_items, page_items.keys_by("connection_id", connection_id))
            if user:
                items = [
                    p for p in items
                    if self._page_owner(p["register_item_id"]) == user
                ]
        elif user:
            items = []
            entry = worker.user_items.get(user)
            for cid in list((entry or {}).get("connections", ())):
                connection = worker.connection_items.get(cid)
                if connection is None:
                    continue
                items.extend(self._live_rows(page_items, list(connection["pages"])))
        else:
            items = self._live_rows(page_items, page_items.keys())
        return {
            item["register_item_id"]: self._adapt_to_legacy(item, register_name="page")
            for item in self._filter_items(items, filters)
        }

    def connections(self, user: Any = None, **kwargs: Any) -> dict:
        """Connections optionally by user, keyed by connection_id (``adaptListToDict``).

        Called by ``connected_users_bag`` and cleanup. ``user`` reads the ownership
        edge (the user entry's ``connections`` set), snapshot before iteration —
        the demolitions mutate the live set in place on other threads.
        """
        worker = self.spa_worker
        if worker is None:
            return {}
        connection_items = worker.connection_items
        if user:
            entry = worker.user_items.get(user)
            items = self._live_rows(connection_items, list((entry or {}).get("connections", ())))
        else:
            items = self._live_rows(connection_items, connection_items.keys())
        return {
            item["register_item_id"]: self._adapt_to_legacy(item, register_name="connection")
            for item in items
        }

    def users(self, **kwargs: Any) -> dict:
        """Active users keyed by user id (``adaptListToDict``): lists connected users.

        Polled every 2 seconds by the chat component's connected-users grid, through
        ``Connection.connected_users_bag`` — the reader that captions with
        ``user_name`` and dates the item from ``start_ts`` when no client clock is
        on it, which on this stack is always the case (``_adapt_to_legacy``).
        """
        worker = self.spa_worker
        if worker is None:
            return {}
        return {
            item["register_item_id"]: self._adapt_to_legacy(item, register_name="user")
            for item in self._live_rows(worker.user_items, worker.user_items.keys())
        }

    def get_dbenv(self, register_item_id: Any, **kwargs: Any) -> Bag:
        """Build a page's database-environment Bag from its data (= the daemon's walk).

        Called by ``WebPage._get_db`` on first ``self.db`` access to seed the db env.
        """
        item = self._ensure_item_data(self.local_item(register_item_id, "page"))
        if item is None:
            return Bag()
        data = item["data"]
        dbenvbag = data.getItem("dbenv") or Bag()
        dbenvbag.update(data.getItem("rootenv") or Bag())

        def add_to_dbenv(node: Any, _pathlist: Any = None) -> None:
            if node.attr.get("dbenv"):
                path = node.label if node.attr["dbenv"] is True else node.attr["dbenv"]
                dbenvbag[path] = node.value

        data.walk(add_to_dbenv, _pathlist=[])
        return dbenvbag

    # ==================================================================
    # Store factories (context managers over one item)
    # ==================================================================

    def connectionStore(self, connection_id: Any, triggered: bool = False) -> ServerStore:
        """Lockable store over a connection item."""
        return self._make_store("connection", connection_id, triggered=triggered)

    def userStore(self, user: Any, triggered: bool = False) -> ServerStore:
        """Lockable store over a user item — a write into it is found by every
        subscribed page in its own ``user_view``."""
        return self._make_store("user", user, triggered=triggered)

    def pageStore(self, page_id: Any, triggered: bool = False) -> ServerStore:
        """Lockable store over a page item — batch thermo/result writes go here."""
        return self._make_store("page", page_id, triggered=triggered)

    def globalStore(self, triggered: bool = False) -> ServerStore:
        """Lockable store over the site-wide global item (the shared TS cache)."""
        return self._make_store("global", "*", triggered=triggered)

    def _make_store(self, register_name: str, register_item_id: Any, triggered: bool = False) -> ServerStore:
        return ServerStore(self, register_name, register_item_id=register_item_id, triggered=triggered)

    # ==================================================================
    # Locks (used by ServerStore): reentrant per reason, in-process
    # ==================================================================

    def lock_item(self, register_item_id: Any, reason: Any = None, register_name: Any = None) -> bool:
        """Acquire the in-process item lock (reentrant for the same reason).

        The global store has a single writer in-process, so its lock is a no-op grant.
        """
        if register_name == "global":
            return True
        key = (register_name, register_item_id)
        with self.locks_mutex:
            held = self.item_locks.get(key)
            if held is None:
                self.item_locks[key] = {"reason": reason, "count": 1}
                return True
            if held["reason"] == reason:
                held["count"] += 1
                return True
            return False

    def unlock_item(self, register_item_id: Any, reason: Any = None, register_name: Any = None) -> bool:
        """Release the in-process item lock (pairs with ``lock_item``)."""
        if register_name == "global":
            return True
        key = (register_name, register_item_id)
        with self.locks_mutex:
            held = self.item_locks.get(key)
            if held is None:
                return True
            held["count"] -= 1
            if held["count"] <= 0:
                del self.item_locks[key]
            return True

    # ==================================================================
    # Global-store rail: local leaf writes ship up, pushes materialize back
    # ==================================================================

    def _on_global_change(
        self,
        node: Any = None,
        pathlist: Any = None,
        evt: str | None = None,
        oldvalue: Any = None,
        ind: Any = None,
        reason: Any = None,
    ) -> None:
        """Bag trigger on ``global_bag``: ship each LEAF write on the store rail.

        Full path: for ``ins``/``del`` it is ``pathlist + [node.label]``; for the
        update events the trigger's pathlist already ends with the node's label. A Bag
        value is walked to its leaves (one write per key), so a wholesale subtree
        set/delete becomes per-key writes — an autocreated parent (empty Bag) ships
        nothing, and a subtree REPLACE also drops the old leaves that are gone. Inert
        while this thread is materializing a commander push (the echo must not bounce).
        """
        if getattr(self._global_rail_state, "applying", False):
            return
        if evt in ("ins", "del"):
            path = ".".join(list(pathlist or []) + [node.label])
        else:
            path = ".".join(list(pathlist or []))
        op = "store_del" if evt == "del" else "store_set"
        value = node.value
        if isinstance(value, Bag):
            new_leaves = self._global_leaves(value, path)
            for leaf_path, leaf_value in new_leaves:
                self._ship_global(op, leaf_path, leaf_value)
            if isinstance(oldvalue, Bag):
                kept = {leaf_path for leaf_path, _ in new_leaves}
                for leaf_path, _ in self._global_leaves(oldvalue, path):
                    if leaf_path not in kept:
                        self._ship_global("store_del", leaf_path, None)
            return
        if isinstance(oldvalue, Bag):
            for leaf_path, _ in self._global_leaves(oldvalue, path):
                self._ship_global("store_del", leaf_path, None)
        self._ship_global(op, path, value)

    def _global_leaves(self, bag: Any, prefix: str) -> list[tuple[str, Any]]:
        """The ``(full_path, scalar)`` leaves under *bag*, prefixed; resolvers skipped."""
        leaves: list[tuple[str, Any]] = []

        def collect(node: Any, _pathlist: Any = None) -> None:
            if getattr(node, "resolver", None) is not None:
                logger.debug("global-store rail: resolver at %r not replicated", node.label)
                return
            if not isinstance(node.value, Bag):
                full = ".".join([prefix] + list(_pathlist or []) + [node.label])
                leaves.append((full, node.value))

        bag.walk(collect, _pathlist=[])
        return leaves

    def _ship_global(self, op: str, path: str, value: Any) -> None:
        """One rail write: ``store_set`` ships the TYTX-encoded scalar, ``store_del``
        the key alone — directly on the worker's store ops. While this THREAD holds
        the global lease, the write is COLLECTED on the lease instead of shipping:
        the block's writes travel once, on the release, all-or-nothing. Best-effort:
        a missing worker never breaks the legacy write (the boot writes before the
        worker attaches; nothing to replicate yet)."""
        if op == "store_set" and callable(value):
            logger.debug("global-store rail: callable at %r not replicated", path)
            return
        state = self._global_rail_state
        if getattr(state, "lease_writes", None) is not None:
            state.lease_writes.append(
                (op, path, None if op == "store_del" else self._encode_leased(value))
            )
            return
        worker = self.spa_worker
        if worker is None:
            return
        if op == "store_del":
            worker.store_del(None, path)
            return
        worker.store_set(None, path, value=self._encode_global(value))

    def _encode_global(self, value: Any) -> str:
        """Scalar -> TYTX wire text, ALWAYS suffixed.

        A bare ``asTypedText`` leaves plain strings unsuffixed, so a string that
        LOOKS typed (``'42::L'``) would decode as an int on the other side.

        This is the text the MASTER holds: the ascending store op carries it
        untouched (the commander is a blind courier and writes what arrived),
        and the descending push decodes it exactly once — so what every replica
        and every legacy Bag reads back is the value that was written.
        """
        text, cls = self.catalog.asTextAndType(value)
        return f"{text}::{cls}"

    def _encode_leased(self, value: Any) -> str:
        """The same wire text, encoded ONCE MORE for the lease's extra hop.

        A lease write does not ascend on a store op: it is applied to the lease's
        working copy and the release carries the drained changes through a
        ``to_tytx``/``from_tytx`` hop of its own (core ``release_global_lock`` ->
        ``apply_changes``) before reaching the master. That hop decodes the typed
        text, so a value encoded once would land on the master DECODED —
        ``'42::L'`` as the int 42 on the way out, one decode ahead of the
        immediate rail. Encoding twice spends the extra hop and leaves the master
        holding the same text a lock-less write leaves there, which is what makes
        the two rails agree on what a replica reads.
        """
        return self._encode_global(self._encode_global(value))

    def _open_global_lease(self) -> Any:
        """Acquire the REAL global-store lock and hand back the lease (D4, ratified).

        Runs on the WSGI thread — the sync ``with`` form of the core lease, which
        parks this thread on the worker's loop until the commander grants the
        master. On grant, the master content — whose leaves arrive DECODED, the
        grant having crossed a tytx hop of its own — is materialized into
        ``global_bag`` under the ``applying`` flag, and this
        THREAD's rail switches to collecting: the block's leaf writes join the
        lease instead of shipping one by one. A lease that cannot be acquired —
        channel down, worker not started — maps to ``GnrDaemonLocked``, the
        exception the legacy already catches around a store lock.
        """
        worker = self.spa_worker
        if worker is None:
            raise GnrDaemonLocked("global store lease: no worker attached")
        try:
            lease = worker.global_store_lock()
            master = lease.__enter__()
        except Exception as exc:
            raise GnrDaemonLocked(f"global store lease not acquired: {exc}") from exc
        try:
            # The grant crossed a tytx hop, so its leaves arrive DECODED.
            self._materialize_global_snapshot(
                {
                    path: node.value
                    for path, node in master.walk()
                    if not isinstance(node.value, CoreBag)
                }
            )
            self._global_rail_state.lease_writes = []
        except Exception as exc:
            # The grant is already in force here, and the core lock has neither a
            # TTL nor a wait timeout: anything raised on the way out would hold
            # the master forever, parking the WSGI thread of every later block.
            lease.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return lease

    def _close_global_lease(self, lease: Any, exc_type: Any) -> None:
        """Apply the block's collected writes to the lease's working copy and release.

        The writes travel ONCE, on ``store_unlock``, all-or-nothing: a body that
        raised releases with nothing applied (the core's own apply-on-success
        rule), and the collecting state ends with the block either way. Lock-less
        writes on other threads kept shipping immediately throughout.

        A path the working copy REJECTS (the core Bag raises on ``'#3'`` and the
        other index forms) fails the same way: the lease is released applying
        nothing, so the master never sees half a block — and it IS released,
        which is what keeps the next block from parking forever on a lock that
        has no timeout.
        """
        state = self._global_rail_state
        writes = getattr(state, "lease_writes", None) or []
        state.lease_writes = None
        try:
            if exc_type is None:
                for op, path, value in writes:
                    if op == "store_del":
                        lease.copy.delete(path)
                    else:
                        lease.copy.set(path, value)
        except Exception as exc:
            lease.__exit__(type(exc), exc, exc.__traceback__)
            raise
        lease.__exit__(exc_type, None, None)

    def _global_read(self, path: str, default: Any = None) -> Any:
        """One lock-less read of the global store: a ``store_get`` CALL on the lane.

        The owner design (2026-08-21): the only copy lives on the commander, so a
        read pays its round trip and answers the master at the moment it was asked
        — never a local copy that ages. Before the worker attaches, the local Bag
        answers (the site touches the register during its own construction). A
        subtree comes back as a core Bag and is translated leaf by leaf into a
        legacy Bag; aware datetimes are normalized to naive local, the same
        boundary convention as ``_materialize_global_snapshot``.
        """
        worker = self.spa_worker
        if worker is None:
            return self.global_bag.getItem(path, default)
        value = worker.store_get(None, path)
        if value is None:
            return default
        if isinstance(value, CoreBag):
            bag = Bag()
            for leaf_path, node in value.walk():
                leaf = node.value
                if isinstance(leaf, CoreBag):
                    continue
                if isinstance(leaf, datetime.datetime) and leaf.tzinfo is not None:
                    leaf = leaf.astimezone().replace(tzinfo=None)
                bag.setItem(leaf_path, leaf)
            return bag
        if isinstance(value, datetime.datetime) and value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        return value

    def _materialize_global_snapshot(self, leaves: dict) -> None:
        """Replace the whole Bag content from DECODED ``{full_path: value}`` leaves.

        Validate-then-apply: every leaf lands in a SCRATCH legacy Bag first,
        and only a fully materialized scratch clears and refills the live Bag,
        so a failing leaf leaves the legacy ``global_bag`` untouched instead
        of empty-to-partial for the process lifetime. The scratch validates
        KEY GRAMMAR only — a path the Bag grammar rejects raises there — not
        the full live-write semantics: the live Bag carries backref and
        subscribers, the bare scratch neither. The live Bag
        stays THE SAME object (write-by-reference, subscribers attached —
        cemented). The channel is FIFO: later writes apply on top, no partial
        window. The refill runs under the ``applying`` flag — the rebuild
        never re-ships. Aware datetimes are normalized like every
        materialized value.
        """
        scratch = Bag()
        normalized: list[tuple[str, Any]] = []
        for key in sorted(leaves):
            value = leaves[key]
            if isinstance(value, datetime.datetime) and value.tzinfo is not None:
                value = value.astimezone().replace(tzinfo=None)
            scratch.setItem(key, value)
            normalized.append((key, value))
        state = self._global_rail_state
        state.applying = True
        try:
            bag = self.global_bag
            bag.clear()
            for key, value in normalized:
                bag.setItem(key, value)
        finally:
            state.applying = False

    # ==================================================================
    # Datachange writes (used by ServerStore and setInClientData)
    # ==================================================================

    def set_datachange(
        self,
        register_item_id: Any,
        path: Any,
        value: Any = None,
        attributes: Any = None,
        fired: Any = False,
        reason: Any = None,
        replace: bool = False,
        delete: bool = False,
        register_name: Any = None,
        **kwargs: Any,
    ) -> None:
        """Queue one datachange toward an item (page = SIGNAL deposit, stores = STATE write).

        Called by ``ServerStore.set_datachange`` (batch thermo/result, chat, mixin_set)
        and by ``setInClientData``. The change travels as the genro-bag plain dict,
        TYTX-encoded (a gnr Bag value rides ``::BAG``); ``register_name`` picks the
        address kind: a page is a deposit on that page's collector, ``user``/
        ``connection`` are real writes on the addressed row's live store, captured by
        every subscribed ``user_view``.
        """
        worker = self.spa_worker
        if worker is None:
            return
        change = {
            "key": {"path": path, "reason": reason, "fired": bool(fired)},
            "value": value,
            "attributes": attributes,
            "delete": bool(delete),
            "change_ts": datetime.datetime.now(datetime.UTC),
            "change_idx": 0,
        }
        kinds = {"page": "page", "user": "user_store", "connection": "connection_store"}
        kind = kinds[register_name or "page"]
        worker.set_datachange(
            None,
            change=to_tytx(change, "json"),
            kind=kind,
            target=register_item_id,
            replace=replace,
        )

    def reset_datachanges(self, register_item_id: Any, register_name: Any = None) -> None:
        """Empty a page's datachange queue without reading it.

        Only a page has a pending queue in this world; the store registers carry
        STATE (their live Bag), which a reset has no meaning for.
        """
        worker = self.spa_worker
        if worker is None or (register_name or "page") != "page":
            return
        worker.reset_datachanges(None, target=register_item_id)

    def drop_datachanges(self, register_item_id: Any, path: Any, register_name: Any = None) -> None:
        """Remove a page's queued datachanges under a path prefix."""
        worker = self.spa_worker
        if worker is None or (register_name or "page") != "page":
            return
        worker.drop_datachanges(None, path=path, target=register_item_id)

    def subscribe_path(self, register_item_id: Any, path: Any, register_name: Any = None) -> None:
        """Widen a page's own capture with a server path (setPendingContext uses this).

        Maps to ``setStoreSubscription(storename='page')``: the row's
        ``subscribed_paths`` and the collector's prefix set move together. Only a
        page has a capture to widen; any other register is a documented no-op.
        """
        worker = self.spa_worker
        if worker is None or (register_name or "page") != "page":
            return
        worker.setStoreSubscription(
            None, page_id=register_item_id, storename="page", prefix=path, active=True
        )

    def subscribeTable(self, page_id: Any, table: Any = None, subscribe: bool = True, **kwargs: Any) -> None:
        """A page subscribes/unsubscribes a db table (channel-C surface).

        Called by ``WebPage.subscribeTable`` when a selection/query binds a table.
        The worker keeps the row's ``table_subscriptions`` and its index together.
        """
        worker = self.spa_worker
        if worker is None:
            return
        worker.subscribeTable(
            None, table=table, page_id=page_id, subscribe=subscribe,
            subscribeMode=kwargs.get("subscribeMode"),
        )

    def setStoreSubscription(self, page_id: Any, storename: Any = None, client_path: Any = None, active: Any = None) -> None:
        """A page subscribes a store path (``storename='user'`` opens its user_view).

        Called by ``WebPage.setStoreSubscription``. ``client_path`` becomes the
        worker op's ``prefix``; user-store subscriptions create or widen the page's
        ``user_view`` on the owner's live store.
        """
        worker = self.spa_worker
        if worker is None:
            return
        worker.setStoreSubscription(
            None, page_id=page_id, storename=storename, prefix=client_path,
            active=bool(active),
        )

    def notifyDbEvents(self, dbeventsDict: Any, register_name: Any = None, origin_page_id: Any = None, dbevent_reason: Any = None, **kwargs: Any) -> None:
        """Fan db-commit events out to the subscribed pages (channel C).

        Called by ``GnrWsgiWebApp.onDbCommitted`` after a db commit. The worker
        deposits on its local subscribers at once (origin page NOT excluded — legacy
        semantics) and ascends the same deposits for the other workers.
        """
        worker = self.spa_worker
        if worker is None:
            return
        worker.notifyDbEvents(
            None, dbevents=dbeventsDict, reason=dbevent_reason, page_id=origin_page_id
        )

    def setInClientData(self, path: Any, value: Any = None, attributes: Any = None, page_id: Any = None, filters: Any = None, fired: bool = False, reason: Any = None, public: bool = False, replace: bool = False, register_name: Any = None, **kwargs: Any) -> None:
        """Push data to one page or broadcast to a filtered set (legacy/polling mode).

        Called by ``WebPage.setInClientData_legacy``. Resolves the target pages (a single
        page_id, or every page matching ``filters``) and queues the change(s) on each.
        """
        if filters:
            page_ids = list(self.pages(filters=filters).keys())
        else:
            page_ids = [page_id]
        for pid in page_ids:
            if not pid:
                continue
            if isinstance(path, Bag):
                for change_node in path:
                    attr = dict(change_node.attr)
                    self.set_datachange(
                        pid, attr.pop("_client_path"), value=change_node.value,
                        attributes=attr, fired=attr.pop("fired", None), register_name="page",
                    )
            else:
                self.set_datachange(
                    pid, path, value=value, reason=reason, attributes=attributes,
                    fired=fired, replace=replace, register_name="page",
                )

    # ==================================================================
    # Page-data commands (channel A: server-path writes, pending context)
    # ==================================================================

    def set_serverstore_changes(self, page_id: Any, datachanges: Any = None, **kwargs: Any) -> None:
        """Write the client's server-path changes into the page's live store.

        Called at the start of every RPC (and inside ``handle_ping``) when the client
        sends ``_serverstore_changes``. Worker-local (channel A): stays on the page row.
        """
        item = self._ensure_item_data(self.local_item(page_id, "page"))
        if item is None or not datachanges:
            return
        data = item["data"]
        for path, value in list(datachanges.items()):
            data.setItem(path, self._parse_typed(value))

    def setPendingContext(self, page_id: Any, pendingContext: Any = None, **kwargs: Any) -> None:
        """Persist the page's pending server context at end of page.

        Called by ``WebPage`` at page teardown. Writes each (path, value, attr) into the
        page's live store, then widens the page's own capture with the path — future
        writes on it become datachanges, the daemon's contract.
        """
        item = self._ensure_item_data(self.local_item(page_id, "page"))
        if item is None or not pendingContext:
            return
        data = item["data"]
        for serverpath, value, attr in pendingContext:
            data.setItem(serverpath, value, attr)
            if isinstance(value, Bag):
                data.clearBackRef()
                data.setBackRef()
            self.subscribe_path(page_id, serverpath, register_name="page")

    # ==================================================================
    # The pull: subscription_storechanges + handle_ping (both channels)
    # ==================================================================

    def subscription_storechanges(self, user: Any, page_id: Any) -> list:
        """The page's pull, served in-process: one drain on the page's own worker.

        Called by ``WebPage.collectClientDatachanges`` at the end of every RPC.
        Channel D was already captured by the page's ``user_view`` at the write, so
        *user* plays no part in the read.
        """
        return self._collect_local_datachanges(page_id)

    def handle_ping(self, page_id: Any = None, reason: Any = None, **kwargs: Any) -> Any:
        """The page's periodic ping, served in-process (= the daemon's ``handle_ping``).

        Called by ``gnrwsgisite.serve_ping`` on the polling endpoint. Refreshes the page
        (timestamps up to the user; a dead page answers ``False`` and the client stops),
        applies the client's serverstore changes (page and children), then builds the
        envelope: ``dataChanges`` (both species, the dbevents dressed), ``childDataChanges.<id>``,
        and the ``runningBatch`` flag from the user store's ``lastBatchUpdate``.
        """
        user_item = self._local_refresh(
            page_id, last_user_ts=kwargs.get("_lastUserEventTs"), last_rpc_ts=kwargs.get("_lastRpc")
        )
        if not user_item:
            return False
        if kwargs.get("_serverstore_changes"):
            self.set_serverstore_changes(page_id, datachanges=kwargs["_serverstore_changes"])
        children_info = kwargs.get("_children_pages_info") or {}
        for child_id, child_changes in list(children_info.items()):
            child_changes = dict(child_changes or {})
            child_user_ts = child_changes.pop("_lastUserEventTs", None)
            child_rpc = child_changes.pop("_lastRpc", None)
            child_changes.pop("_pageProfilers", None)
            if child_changes:
                self.set_serverstore_changes(child_id, datachanges=child_changes)
            self._local_refresh(
                child_id, last_user_ts=self._parse_typed(child_user_ts),
                last_rpc_ts=self._parse_typed(child_rpc),
            )
        envelope = Bag(dict(result=None))
        changes = self._changes_to_bag(self._collect_local_datachanges(page_id))
        if changes is not None:
            envelope.setItem("dataChanges", changes)
        for child_id in children_info:
            child_bag = self._changes_to_bag(self._collect_local_datachanges(child_id))
            if child_bag is not None:
                envelope.setItem(f"childDataChanges.{child_id}", child_bag)
        self._flag_running_batch(envelope, user_item)
        return envelope

    # ==================================================================
    # Commit-path helper: which tables have a subscriber
    # ==================================================================

    def filter_subscribed_tables(self, table_list: Any, **kwargs: Any) -> list:
        """The commit gate: which tables of ``table_list`` deserve db events.

        Called by ``site.getSubscribedTables`` on every db commit to decide whether to
        build and send the db events. A worker on the wire passes the whole list
        through and lets the fan-out target the real subscribers (unchanged cemented
        rule; over-notifying is innocuous). A bare worker — no wire, every page its
        own — answers from the worker's subscription cache (``subscribed_tables``,
        refreshed by every exchange and subscribe reply).
        """
        worker = self.spa_worker
        if worker is None:
            return []
        if worker.pool_member:
            return list(table_list or [])
        return [table for table in (table_list or []) if table in worker.subscribed_tables]

    def subscribed_tables(self, register_name: Any = None, **kwargs: Any) -> list:
        """Every table observed by at least one live page — the write-time gate reads it.

        The daemon grew this command with the write-time broadcast gate
        (genropy #968: ``site.allSubscribedTables`` asks the register instead
        of unioning per-page lists at commit time). On the wire the worker's
        subscription cache IS the whole pool's view, refreshed synchronously
        by every subscribe and exchange reply; a bare worker unions its own
        page rows, which are all the pages there are.
        """
        worker = self.spa_worker
        if worker is None:
            return []
        if worker.pool_member:
            return sorted(worker.subscribed_tables)
        with worker.dispatch_lock:
            tables: set = set()
            for page_id in worker.page_items.keys():
                page = worker.page_items.get(page_id)
                if page is not None:
                    tables.update(page["table_subscriptions"])
        return sorted(tables)

    # ==================================================================
    # Maintenance / cleanup (in-process, single node)
    # ==================================================================

    def setMaintenance(self, status: Any = None, allowed_users: Any = None, **kwargs: Any) -> None:
        """Enter/leave maintenance mode (per-process state)."""
        self.__dict__["_maintenance"] = bool(status)
        self.__dict__["_allowed_users"] = allowed_users

    def isInMaintenance(self, user: Any = None, **kwargs: Any) -> bool:
        """True if the site is in maintenance for *user* (``*forced*`` always passes)."""
        maintenance = self.__dict__.get("_maintenance", False)
        allowed = self.__dict__.get("_allowed_users")
        if not maintenance or user == "*forced*":
            return False
        if not user or not allowed:
            return maintenance
        return user not in allowed

    def allowedUsers(self) -> Any:
        """The users allowed during maintenance."""
        return self.__dict__.get("_allowed_users")

    def claim_cleanup(self, interval: Any = 60, **kwargs: Any) -> bool:
        """The cleanup lottery is never granted: the WORKER sweeps.

        The core sweep (armed on GenropyWorker, ``sweep_interval=60``) owns
        both halves of the eviction — the register rows and their disk
        folders — so the legacy site's own cleanup thread must never run.
        """
        return False

    def expire_pages(self, max_age: Any = None, **kwargs: Any) -> list:
        """Documented no-op: the worker's sweep expires pages (``page_max_age``)."""
        return []

    def expire_connection(self, max_age: Any = None, **kwargs: Any) -> list:
        """Documented no-op: the worker's sweep expires connections
        (``connection_max_age``, ``guest_max_age`` for a guest)."""
        return []

    def on_reloader_restart(self, *args: Any, **kwargs: Any) -> None:
        """Dev reloader restart hook — nothing to persist in-process."""
        return None

    def on_site_stop(self, *args: Any, **kwargs: Any) -> None:
        """Site shutdown hook — persistence is the future Service Store's business."""
        return None

    def updatePageProfilers(self, *args: Any, **kwargs: Any) -> None:
        """Page profilers update — not collected in-process."""
        return None

    # ==================================================================
    # Not served in-process (PROVISIONAL): inter-process bus, persistence.
    # ==================================================================

    def sendProcessCommand(self, *args: Any, **kwargs: Any) -> None:
        """Inter-process command bus — the commander will host it (PROVISIONAL no-op)."""
        return None

    def pendingProcessCommands(self, *args: Any, **kwargs: Any) -> list:
        """Inter-process command bus — the commander will host it (PROVISIONAL empty)."""
        return []

    def dump(self) -> None:
        """No in-process persistence yet (future Service Store)."""
        logger.info("register dump skipped: no in-process persistence yet")

    def load(self) -> None:
        """No in-process persistence yet (future Service Store)."""
        logger.info("register load skipped: no in-process persistence yet")

    # ==================================================================
    # Boot-only compatibility: the DataCollector reference
    # ==================================================================

    @property
    def siteregister(self) -> Any:
        """Present only so the site boot does not break; NOT a monitoring surface.

        The legacy site boot builds ``DataCollector(self.register.siteregister)``
        (gnrwsgisite.py) — it only stores the reference, so returning the client itself
        lets the site start. The DataCollector read views are exercised only by the
        optional ``gnrinspect`` developer CLI, which this build does not support (see
        genropy#974). Intentionally not a working monitor.
        """
        return self

    # ==================================================================
    # Internal helpers (in-process bodies shared by the commands above)
    # ==================================================================

    def local_item(self, register_item_id: Any, register_name: Any) -> dict | None:
        """The live register row (page/connection/user) from the worker, or None."""
        worker = self.spa_worker
        if worker is None or not register_name:
            return None
        registers = {
            "page": worker.page_items,
            "connection": worker.connection_items,
            "user": worker.user_items,
        }
        return registers[register_name].get(register_item_id)

    def _live_rows(self, register: Any, keys: Any) -> list:
        """The rows of *keys* still in *register* — a read races the demolitions.

        The read commands walk a key snapshot and re-fetch each row WITHOUT
        ``dispatch_lock``: they are hot paths (the chat poll asks every two
        seconds, every broadcast resolves its targets), and the lock belongs to
        the writers. Meanwhile the sweep, a logout and a page close demolish rows
        on other threads, so a key of the snapshot can have no row left by the
        time it is read. A row that vanished mid-read is a legitimate state — the
        item simply is not in the answer — and the alternative is what the legacy
        saw: ``None`` handed out as a register item, subscripted on the spot by
        the chat poll (connection.py:195).
        """
        return [row for row in (register.get(key) for key in keys) if row is not None]

    def _page_owner(self, page_id: Any) -> Any:
        """The page's user, walked page -> connection -> user with ``.get()``.

        The registry's own ``page_user`` raises on a vanished link (``KeyError``
        on the page, ``TypeError`` on the connection); the lock-free read sites
        must not — a chain with a link gone is being demolished on another
        thread (the sweep, a logout and a page close are concurrent by design)
        and answers None: absent, never an error.
        """
        worker = self.spa_worker
        page = worker.page_items.get(page_id)
        if page is None:
            return None
        connection = worker.connection_items.get(page["connection_id"])
        if connection is None:
            return None
        return connection["user"]

    def _item_with_data(self, item_id: Any, register_name: str) -> dict | None:
        """The answer of a lifecycle command: the legacy view, with its data Bag.

        The Bag is the SAME live object the register item holds — the site writes
        into it and the collectors watch it — so what goes out is the reference.
        """
        item = self.local_item(item_id, register_name)
        adapted = self._adapt_to_legacy(item, register_name=register_name)
        if adapted is not None:
            adapted["data"] = self._ensure_item_data(item)["data"]
        return adapted

    def _adapt_to_legacy(self, register_item: dict | None, register_name: str) -> dict | None:
        """The legacy answer for one core register item — the one site-facing surface.

        A PROJECTION, not a copy: what goes out carries exactly the fields the
        daemon's own register put on its register item (``LEGACY_REGISTER_ITEM_FIELDS``),
        so nothing the core keeps for itself leaks to the site and nothing the
        daemon guaranteed is missing. Both directions were divergences the
        replica measured on the first bridge cycle (2026-08-25): the connection
        register item carried five core fields the daemon never had and lacked
        five the daemon always had, and the page register item diverged the same
        way two calls later.

        The live objects stay the same objects — the data Bag, the ``pages`` and
        ``connections`` edge sets — and the core's own register item is never
        touched: the expiry sweep keeps reading its clocks as floats where they
        are.

        What the projection adds on top of the field list:

        - **The three queue fields** the daemon put on every register item
          (``addRegisterItem``, siteregister.py:135). ``subscribed_paths`` is the
          page register item's own set, cheap to read. ``datachanges`` and
          ``datachanges_idx`` go out as the empty shape: on this stack the queue
          is not on the register item but in the page's collectors, and the
          surface that reads it is ``ServerStore.datachanges``, which drains them
          there. The bridge also numbers each change and not each register item,
          so there is no per-item counter to answer with.
        - **``register_name``**, which the daemon seeded on the register item
          itself.
        - **``user`` on a PAGE register item.** The daemon stored it; here
          ownership is derived through the connection (``_page_owner``,
          cemented) — so the key the legacy reads is answered, and still nothing
          is stored.
        - **``user`` on a USER register item.** The daemon seeded ``user=user``
          (siteregister.py:319-323) and the chat keys its rooms on that attribute
          (``prepare_usersbag``, chat_component.js:180 — ``setItem(n.attr.user,
          ...)``, which crashes the client Bag on undefined). The core keys the
          entry by name instead of storing it, so the answer restores the field.
        - **``subscribed_tables``**, the daemon's name for what the core register
          item carries as ``table_subscriptions``.
        - **``start_ts``**, read unconditionally as the "no client clock reported
          yet" fallback (connection.py:196) and as a page's birth instant
          (gnrasync.py:379). The connection and page register items are born with
          it (the lifecycle commands stamp it); a USER entry is created by the
          core itself, implicitly, under a new connection, so it cannot be
          stamped from here and falls back to the item's server clock — the
          creation instant, converted out of the core's epoch float.
        """
        if register_item is None:
            return None
        item_id = register_item["register_item_id"]
        adapted: dict[str, Any] = {
            "register_name": register_name,
            "datachanges": [],
            "datachanges_idx": 0,
            "subscribed_paths": self._item_subscribed_paths(item_id, register_name=register_name),
        }
        for field in LEGACY_REGISTER_ITEM_FIELDS[register_name]:
            adapted[field] = register_item.get(field)
        if register_name == "page":
            adapted["user"] = self._page_owner(item_id)
            adapted["subscribed_tables"] = set(register_item.get("table_subscriptions") or ())
        elif register_name == "user":
            adapted["user"] = item_id
        if adapted["start_ts"] is None:
            adapted["start_ts"] = datetime.datetime.fromtimestamp(register_item["last_refresh_ts"])
        return adapted

    def _ensure_item_data(self, item: dict | None) -> dict | None:
        """Alias the row's live store as ``data`` — the name the legacy reads.

        One Bag, two keys: ``store`` is the core's name (the collectors watch it,
        a move packages it), ``data`` is the daemon-era name every legacy consumer
        uses. The alias is set once on the live row.
        """
        if item is not None and not isinstance(item.get("data"), Bag):
            store = item.get("store")
            item["data"] = store if isinstance(store, Bag) else Bag()
        return item

    def _conn_kwargs(self, connection: Any, kwargs: dict) -> dict:
        """Extract the scalar fields the connection registry needs from a Connection.

        ``new_connection`` receives the legacy ``Connection`` object; the worker registry
        only wants scalars. When ``connection`` is None the caller already passed kwargs.
        """
        if connection is None:
            return kwargs
        out = dict(kwargs)
        for field in ("connection_name", "user", "user_id", "user_tags", "user_ip",
                      "user_agent", "browser_name", "electron_static", "user_name"):
            if field not in out:
                out[field] = getattr(connection, field, None)
        return out

    def _page_kwargs(self, page: Any, kwargs: dict) -> dict:
        """Extract the scalar fields the page registry needs from a WebPage.

        ``relative_url`` is NOT an attribute of the page: the daemon client read
        it off the request (``daemon/siteregister_client.py``:220), so this one
        does too. Read as an attribute it answered None on every page, and the
        replica measured it as a difference on the page register item.
        """
        if page is None:
            return kwargs
        out = dict(kwargs)
        for field in ("pagename", "connection_id", "user", "user_ip", "user_agent"):
            if field not in out:
                out[field] = getattr(page, field, None)
        if "relative_url" not in out:
            out["relative_url"] = page.request.path_info
        return out

    def _filter_items(self, items: list, filters: Any) -> list:
        """The daemon's ad-hoc page filter grammar: ``name:regex AND name:value``.

        The grammar stays client-local (cemented): the single sees every page, so it
        answers the whole question itself. Names read off the page row exactly as the
        daemon read them (``pagename``, ``user_ip``, ``relative_url``, ...) — with ONE
        exception:

        ``user`` is resolved through ``_page_owner``, the tolerant walk page ->
        connection -> user. The daemon's page row carried a ``user`` field; the core's
        does not, on purpose — ownership is derived so it cannot go stale, and the
        bridge's ``new_page`` pops the field before the op. Reading ``item['user']``
        here therefore matched nothing, and ``user:X`` broadcasts
        (``gnr.chat.room_alert``, every filtered ``sendMessageToClient``) reached
        nobody. The derived owner is the same answer, from the one place that holds
        it; a chain gone mid-read answers None and the row simply does not match —
        it is being demolished, no broadcast belongs on it.
        """
        if not filters or filters == "*":
            return items
        fltdict: dict[str, Any] = {}
        for flt in filters.split(" AND "):
            fltname, fltvalue = flt.split(":", 1)
            try:
                fltdict[fltname] = re.compile(fltvalue)
            except re.error:
                fltdict[fltname] = fltvalue
        filtered = []
        for item in items:
            for fltname, fltpat in fltdict.items():
                if fltname == "user":
                    value = self._page_owner(item["register_item_id"])
                else:
                    value = item.get(fltname)
                if not value:
                    continue
                if not isinstance(value, (bytes, str)):
                    if str(fltpat) == value:
                        filtered.append(item)
                elif isinstance(fltpat, re.Pattern):
                    if fltpat.match(value):
                        filtered.append(item)
                elif fltpat == value:
                    filtered.append(item)
        return filtered

    def _local_refresh(self, page_id: Any, last_user_ts: Any = None, last_rpc_ts: Any = None) -> dict | None:
        """Stamp the chain: the server clock via ``refresh_chain``, the client clocks as fields.

        ``last_refresh_ts`` is NEVER touched with client values — a page cannot buy
        immortality by lying about its own activity. The client-reported clocks land
        as ``last_user_ts``/``last_rpc_ts`` on the three rows, under ``dispatch_lock``,
        converted datetime -> epoch float at this boundary: the rows keep the core's
        own stamp type (the freeze valve compares these floats), and they stay
        there: the legacy view does not carry them out, because a daemon row
        carried them only once a refresh had reported client clocks.
        Returns the USER item (``handle_ping`` reads the user from it), or None when
        the chain is broken (a dead page: the ping answers False).
        """
        worker = self.spa_worker
        if worker is None:
            return None
        if isinstance(last_user_ts, datetime.datetime):
            last_user_ts = last_user_ts.timestamp()
        if isinstance(last_rpc_ts, datetime.datetime):
            last_rpc_ts = last_rpc_ts.timestamp()
        with worker.dispatch_lock:
            page = worker.page_items.get(page_id)
            if page is None:
                return None
            worker.refresh_chain(page_id)
            connection = worker.connection_items.get(page["connection_id"])
            user_item = worker.user_items.get(connection["user"])
            for item in (page, connection, user_item):
                for field, value in (("last_user_ts", last_user_ts), ("last_rpc_ts", last_rpc_ts)):
                    if value is not None:
                        current = item.get(field)
                        item[field] = max(current, value) if current else value
        return user_item

    def _parse_typed(self, value: Any) -> Any:
        """Parse a typed-text value from the client wire (the daemon used its catalog)."""
        if isinstance(value, (bytes, str)):
            return self.catalog.fromTypedText(value)
        return value

    def _collect_local_datachanges(self, page_id: Any) -> list:
        """Drain the page's pending species and dress them for the legacy client.

        ``worker.collect_page`` drains the page's own collector, its ``user_view``
        and the ``dbevents`` list under one lock. The datachanges become legacy
        ``ClientDataChange`` objects; the dbevents are DRESSED here as datachanges
        on ``gnr.dbchanges.<table>`` — the envelope disguise is the bridge's, the
        core keeps the species separate. A page already gone answers empty.

        The ``autocreate`` changes — the parents the legacy Bag inserts on a
        first write under a fresh prefix — stay OUT of the envelope: the daemon
        built its changes from the write's arguments, so only the explicitly
        written leaves ever travelled. The capture keeps them (they ARE the net
        store state); the envelope is where the daemon contract rules.
        """
        worker = self.spa_worker
        if worker is None:
            return []
        try:
            collected = worker.collect_page(page_id)
        except KeyError:
            return []
        changes = [
            self._change_to_client(raw)
            for raw in collected["datachanges"]
            if raw["key"]["reason"] != "autocreate"
        ]
        changes.extend(self._dbevent_to_client(deposit) for deposit in collected["dbevents"])
        return changes

    def _change_to_client(self, change: dict) -> ClientDataChange:
        """One genro-bag change dict -> the legacy ClientDataChange.

        ``change_ts`` is normalized aware -> naive local at this boundary: the legacy
        world compares naive clocks (same convention as ``_materialize_global_snapshot``).
        """
        key = change["key"]
        change_ts = change["change_ts"]
        if isinstance(change_ts, datetime.datetime) and change_ts.tzinfo is not None:
            change_ts = change_ts.astimezone().replace(tzinfo=None)
        return ClientDataChange(
            key["path"], change["value"], attributes=change["attributes"],
            reason=key["reason"], fired=key["fired"], change_ts=change_ts,
            change_idx=change["change_idx"], delete=change["delete"],
        )

    def _dbevent_to_client(self, deposit: dict) -> ClientDataChange:
        """One dbevent deposit -> the legacy datachange on ``gnr.dbchanges.<table>``.

        The table dots become underscores (the legacy path grammar, gnrwebpage.py
        ``notifyLocalDbEvents``); the origin page and the reason ride as attributes.
        """
        table_code = deposit["table"].replace(".", "_")
        attributes = {
            "from_page_id": deposit["from_page_id"],
            "dbevent_reason": deposit["reason"],
        }
        return ClientDataChange(
            f"gnr.dbchanges.{table_code}", deposit["batch"], attributes=attributes
        )

    def _pending_datachanges(self, register_item_id: Any, register_name: Any = None) -> list:
        """Peek at a page's pending changes without consuming them (ServerStore.datachanges).

        The ``drain(reset=False)`` equivalent of ``collect_page``: both collectors
        peeked and merged by ``change_ts``, under the worker's lock, PLUS the
        caller's own request slot — the writes this request has queued for the
        exchange. The core lays addressed writes on the slot and applies them at
        the exchange, so without the slot a serverbatch would stop reading its
        own writes back mid-request: the healed defect, healed on this leg too.
        Only a page has collectors; any other register answers empty. The
        ``autocreate`` parents stay out, as in ``_collect_local_datachanges`` —
        same envelope, same rule.
        """
        worker = self.spa_worker
        if worker is None or (register_name or "page") != "page":
            return []
        with worker.dispatch_lock:
            page = worker.page_items.get(register_item_id)
            if page is None:
                return []
            changes = page["collector"].drain(reset=False)
            if page["user_view"] is not None:
                changes.extend(page["user_view"].drain(reset=False))
        changes.extend(
            from_tytx(entry["change"], "json")
            for entry in worker.request_slot.datachanges
            if entry["kind"] == "page" and entry["target"] == register_item_id
        )
        changes.sort(key=lambda change: change["change_ts"])
        return [
            self._change_to_client(raw)
            for raw in changes
            if raw["key"]["reason"] != "autocreate"
        ]

    def _item_subscribed_paths(self, register_item_id: Any, register_name: Any = None) -> set:
        """The page row's ``subscribed_paths`` set, copied (ServerStore.subscribed_paths)."""
        worker = self.spa_worker
        if worker is None or (register_name or "page") != "page":
            return set()
        page = worker.page_items.get(register_item_id)
        if page is None:
            return set()
        return set(page["subscribed_paths"])

    def _changes_to_bag(self, changes: list) -> Bag | None:
        """Number the changes ``sc_%i`` into the envelope Bag (the daemon's shape)."""
        if not changes:
            return None
        result = Bag()
        for j, change in enumerate(changes):
            result.setItem(
                f"sc_{j}",
                change.value,
                change_path=change.path,
                change_reason=change.reason,
                change_fired=change.fired,
                change_attr=change.attributes,
                change_ts=change.change_ts,
                change_delete=change.delete,
            )
        return result

    def _flag_running_batch(self, envelope: Bag, user_item: dict) -> None:
        """Set ``runningBatch`` while a batch touched the user store within the window."""
        data = self._ensure_item_data(user_item)["data"]
        last_batch_update = data.getItem("lastBatchUpdate")
        if not last_batch_update:
            return
        if (datetime.datetime.now() - last_batch_update).seconds < RUNNING_BATCH_WINDOW:
            envelope.setItem("runningBatch", True)
        else:
            data.setItem("lastBatchUpdate", None)


# The legacy imports ``SiteRegisterClient`` from ``gnr.web.daemon.siteregister_client``
# and instantiates it as ``site.register``. This standalone client IS that class.
SiteRegisterClient = GenropyRegisterClient
