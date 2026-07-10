# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyRegisterClient — the standalone in-process register for legacy GenroPy.

The GenroPy ``GnrWsgiSite`` talks to a register through ``site.register``, calling one
command at a time while it serves a request. Historically that register was a daemon
(Pyro4, then the genro-nodaemon TCP daemon) and the client funnelled every command
through a single ``_sr_call`` that serialized it onto the wire. There is no wire here:
this is the DAEMONLESS register, standalone (no daemon-client base class). So there is
no funnel either — every command the legacy calls is an EXPLICIT public method with its
own body and a docstring saying who calls it and when. No ``__getattr__`` magic, no
per-string dispatch table: what the register serves is exactly the set of methods below.

Two things a mutating command does, both explicit in the method:
1. ``_fold(op, args, kwargs)`` — hand the command to the SPA worker so the local
   registries update and (on a pool child) the lifecycle/POST event rides up to the
   commander on the pool CHANNEL. Reads do NOT fold.
2. its own in-process body — read the local registries / surface / pending lists and
   return what the legacy expects.

Who serves what (FIXED):
- Lifecycle (new/change/drop of connection/page/user, refresh) — folded into the
  worker's registries; the read side (page/connection/pages/…/get_item/exists) answers
  from those registries.
- Datachanges — channel C (subscribeTable/notifyDbEvents) and channel D
  (setStoreSubscription + userStore writes) fold; the deposit lands on the page's OWN
  worker (switch model — a cross-worker change arrives via the commander's
  ``/datachange_in`` forward), so the pull (subscription_storechanges / handle_ping)
  drains the LOCAL pending list.
- Stores — each item's ``data`` is a real in-process legacy Bag; ``ServerStore`` locks
  the item (reentrant per reason) and reads/writes it in-process.
- Global store — one stable legacy Bag (``global_bag``), write-by-reference.

NOT served (explicit, PROVISIONAL): dump/load (future Service Store),
sendProcessCommand/pendingProcessCommands (inter-process bus, will move to the
commander), the daemon-only admin browser. They answer as documented no-ops.

Wiring: this class IS the ``SiteRegisterClient`` the legacy imports as
``gnr.web.daemon.siteregister_client`` (the ``siteregister`` submodule provides the
``gnr.web:daemon`` entry-point), so the ``GnrWsgiSite`` builds it directly at
``site.register`` — no daemon connection at construction, no rebind. Extra state is
created lazily through ``__dict__`` (the site touches ``self.register`` before the SPA
application has attached itself via ``site.spa_application``).
"""

from __future__ import annotations

import datetime
import re
import threading
import time
from typing import Any

from gnr.core.gnrbag import Bag
from gnr.core.gnrclasses import GnrClassCatalog
from gnr.web import logger
from gnr.web.gnrwebpage import ClientDataChange

from genro_asgi.applications.spa_application import LIFECYCLE_EVENTS_KEY

from .exceptions import GnrDaemonLocked

# Lock retry budget for a ServerStore context (in-process contention is rare and short).
LOCK_MAX_RETRY = 50
RETRY_DELAY = 0.05
RETRY_DELAY_MAX = 2.0

# The 5-second window the daemon used for the runningBatch flag in the ping envelope.
RUNNING_BATCH_WINDOW = 5.0


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
        self.siteregister.unlock_item(
            self.register_item_id, reason=self.thread_id, register_name=self.register_name
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
    """The standalone in-process register: every ``site.register`` command is a method.

    Not a subclass of any daemon client. It holds the ``site`` and answers every command
    directly. Mutating commands call ``_fold`` (hand to the worker) and then run their
    in-process body; reads skip the fold. A command the legacy might call that is not a
    method here would simply raise ``AttributeError`` — the served set is exactly what is
    written below, on purpose.
    """

    # The exception the legacy catches around a store lock (touched on the client).
    locked_exception = GnrDaemonLocked

    def __init__(self, site: Any) -> None:
        """Built directly by the ``GnrWsgiSite`` at ``site.register``; no daemon here."""
        self.site = site

    # ------------------------------------------------------------------
    # Lazy state (the site touches self.register before the app attaches)
    # ------------------------------------------------------------------

    @property
    def global_bag(self) -> Bag:
        """The in-process legacy Bag backing the global store (one stable object).

        ``get_item(register_name='global')`` hands it back on every call so a ``setItem``
        on it persists (write-by-reference), exactly as the daemon-backed store did.
        """
        bag = self.__dict__.get("_global_bag")
        if bag is None:
            bag = Bag()
            self.__dict__["_global_bag"] = bag
        return bag

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
    def spa_application(self) -> Any:
        """The hosting SpaApplication, reached through the site (set by the app)."""
        return getattr(self.site, "spa_application", None)

    @property
    def spa_worker(self) -> Any:
        """The SPA worker that holds the local registries and the executor."""
        return getattr(self.spa_application, "worker", None)

    @property
    def _app_registry(self) -> Any:
        """The SpaApplication's surface (subscriptions), reached through the site."""
        return getattr(self.spa_application, "app_registry", None)

    # ------------------------------------------------------------------
    # The one shared step: fold a mutating command into the SPA worker
    # ------------------------------------------------------------------

    def _fold(self, op: str, args: tuple = (), kwargs: dict | None = None) -> None:
        """Hand a mutating command to the SPA worker (the only thing common to them all).

        ``worker.dispatch`` folds the command into the local registries and, for a
        lifecycle/POST op, shapes the event and delivers it: on the single it applies to
        the app's own surface/mailbox; on a pool child it rides the request's event sink
        (the synchronous rail — read from the current request's environ, where the
        hosting mixin copied it) or falls to the outbox. Best-effort: a missing worker
        or a broken fold must never break the legacy call. Reads never call this.
        """
        worker = self.spa_worker
        if worker is None:
            return
        try:
            worker.dispatch(op, args, kwargs or {}, events=self._request_events_sink())
        except Exception:
            logger.exception("registry fold failed for op %r", op)

    def _request_events_sink(self) -> Any:
        """The per-request lifecycle sink, from the current request's environ (or None)."""
        current_request = getattr(self.site, "currentRequest", None)
        environ = getattr(current_request, "environ", None)
        if not environ:
            return None
        return environ.get(LIFECYCLE_EVENTS_KEY)

    # ==================================================================
    # Lifecycle commands (mutating: fold + local body)
    # ==================================================================

    def new_connection(self, connection_id: Any, connection: Any = None, **kwargs: Any) -> dict | None:
        """A browser's first connection is born (login-less, guest included).

        Called by ``Connection.register`` (gnrwebpage_proxy/connection.py) the first time
        a browser is seen. Folds into the connection registry (and auto-creates the user
        if named), then returns the local connection item with its data Bag attached.
        """
        self._fold("new_connection", (connection_id,), self._conn_kwargs(connection, kwargs))
        return self._item_with_data(connection_id, "connection")

    def new_page(self, page_id: Any, page: Any = None, **kwargs: Any) -> dict | None:
        """A new page (browser tab) opens.

        Called by ``WebPage._register_new_page`` (gnrwebpage.py) on page registration.
        Folds into the page registry, returns the local page item with its data Bag.
        """
        self._fold("new_page", (page_id,), self._page_kwargs(page, kwargs))
        return self._item_with_data(page_id, "page")

    def change_connection_user(self, connection_id: Any, **kwargs: Any) -> dict | None:
        """A connection's user changes — LOGIN / avatar switch.

        Called by ``Connection.change_user`` (connection.py) at login. Folds the rebind
        (reindex user, propagate to the connection's pages, drop the old orphan user),
        returns the updated local connection item.
        """
        self._fold("change_connection_user", (connection_id,), kwargs)
        return self._item_with_data(connection_id, "connection")

    def drop_page(self, page_id: Any, **kwargs: Any) -> None:
        """A page closes (client onClosePage, or a page flagged closed at end of RPC).

        Called by ``WebPage`` at RPC end when closed and by ``onClosedPage``
        (gnrwsgisite.py). Folds the drop (cascading to an emptied connection when
        ``cascade``); the surface fold also retires the page's mailbox queues.
        """
        self._fold("drop_page", (page_id,), kwargs)

    def drop_connection(self, connection_id: Any, **kwargs: Any) -> None:
        """A connection ends — LOGOUT / browser gone.

        Called by ``Connection.unregister`` and ``rpc_logout`` (connection.py). Folds the
        drop with its page cascade (and user cascade when ``cascade``).
        """
        self._fold("drop_connection", (connection_id,), kwargs)

    def refresh(self, page_id: Any, ts: Any = None, lastRpc: Any = None, pageProfilers: Any = None) -> dict | None:
        """Bump the last-seen timestamps for a page, up through connection to user.

        The daemon exposed ``refresh`` separately; in-process it is the same timestamp
        propagation ``handle_ping`` also does. Returns the user item at the top of the
        chain (or None if the page is gone).
        """
        return self._local_refresh(page_id, last_user_ts=ts, last_rpc_ts=lastRpc)

    # ==================================================================
    # Read commands (no fold: answer from the local registries)
    # ==================================================================

    def get_item(self, register_item_id: Any, include_data: Any = False, register_name: Any = None) -> Any:
        """Return one register item by id (page/connection/user), or the global store.

        The read primitive the whole read side builds on. ``register_name='global'``
        returns the stable global Bag; ``include_data == 'lazy'`` attaches the item's
        in-process Bag (the daemon attached a RemoteStoreBag proxy — here it is local).
        """
        if register_name == "global":
            return {"register_item_id": "*", "register_name": "global", "data": self.global_bag}
        item = self.local_item(register_item_id, register_name)
        if item is not None and (include_data == "lazy" or include_data):
            self._ensure_item_data(item)
        return item

    def page(self, page_id: Any, include_data: Any = None) -> Any:
        """The local page item, enriched with the surface's ``subscribed_tables``.

        Called on every RPC to validate the page and by the commit path (a hidden
        transaction reads ``page(page_id)['subscribed_tables']``); the subscriptions
        live in the channel-C surface, so they are attached here.
        """
        item = self.get_item(page_id, include_data=include_data, register_name="page")
        registry = self._app_registry
        if item is not None and registry is not None:
            item["subscribed_tables"] = set(registry.page_tables.get(page_id, ()))
        return item

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
        the keys must be the page ids. Called for the ``setInClientData`` broadcast (with
        filters) and by monitoring.
        """
        register = self._page_register()
        if register is None:
            return {}
        if connection_id:
            items = [register[k] for k in register.keys_by("connection_id", connection_id)]
            if user:
                items = [p for p in items if p.get("user") == user]
        elif user:
            items = [register[k] for k in register.keys_by("user", user)]
        else:
            items = [item for _, item in register.items()]
        return {item["register_item_id"]: item for item in self._filter_items(items, filters)}

    def connections(self, user: Any = None, **kwargs: Any) -> dict:
        """Connections optionally by user, keyed by connection_id (``adaptListToDict``).

        Called by ``connected_users_bag`` and cleanup.
        """
        registry = self._registers()
        if registry is None:
            return {}
        register = registry.connections
        if user:
            items = [register[k] for k in register.keys_by("user", user)]
        else:
            items = [item for _, item in register.items()]
        return {item["register_item_id"]: item for item in items}

    def users(self, **kwargs: Any) -> dict:
        """Active users keyed by user id (``adaptListToDict``): lists connected users."""
        registry = self._registers()
        if registry is None:
            return {}
        return {item["register_item_id"]: item for _, item in registry.users.items()}

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
        """Lockable store over a user item — the channel-D user-store writes go here."""
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
    # Datachange writes (used by ServerStore and setInClientData): POST ops.
    # They fold to the commander's surface/mailbox; no local body.
    # ==================================================================

    def set_datachange(self, register_item_id: Any, path: Any, register_name: Any = None, **kwargs: Any) -> None:
        """Queue one datachange on an item (page queue = channel C, user queue = channel D).

        Called by ``ServerStore.set_datachange`` (batch thermo/result, chat, mixin_set).
        """
        self._fold("set_datachange", (register_item_id, path), dict(register_name=register_name, **kwargs))

    def reset_datachanges(self, register_item_id: Any, register_name: Any = None) -> None:
        """Empty an item's datachange queue without reading it."""
        self._fold("reset_datachanges", (register_item_id,), {"register_name": register_name})

    def drop_datachanges(self, register_item_id: Any, path: Any, register_name: Any = None) -> None:
        """Remove an item's queued datachanges under a path prefix."""
        self._fold("drop_datachanges", (register_item_id, path), {"register_name": register_name})

    def subscribe_path(self, register_item_id: Any, path: Any, register_name: Any = None) -> None:
        """Record that a page subscribes a server path (setPendingContext uses this)."""
        # The subscribed path is tracked on the page item by setPendingContext; there is
        # no separate surface for server-path subscriptions, so this is a local note.
        item = self.local_item(register_item_id, register_name or "page")
        if item is not None:
            subscribed = item.setdefault("subscribed_paths", [])
            if path not in subscribed:
                subscribed.append(path)

    def subscribeTable(self, page_id: Any, table: Any = None, subscribe: bool = True, **kwargs: Any) -> None:
        """A page subscribes/unsubscribes a db table (channel-C surface).

        Called by ``WebPage.subscribeTable`` when a selection/query binds a table. Folds
        into the commander's page->tables surface.
        """
        self._fold("subscribeTable", (page_id,), dict(table=table, subscribe=subscribe, **kwargs))

    def setStoreSubscription(self, page_id: Any, storename: Any = None, client_path: Any = None, active: Any = None) -> None:
        """A page subscribes a store path (channel-D surface for ``storename='user'``).

        Called by ``WebPage.setStoreSubscription``. Only user-store subscriptions feed
        the pull; the fold routes them to the commander's channel-D surface.
        """
        self._fold(
            "setStoreSubscription", (page_id,),
            dict(storename=storename, client_path=client_path, active=active),
        )

    def notifyDbEvents(self, dbeventsDict: Any, register_name: Any = None, origin_page_id: Any = None, dbevent_reason: Any = None, **kwargs: Any) -> None:
        """Fan db-commit events out to the subscribed pages (channel C).

        Called by ``GnrWsgiWebApp.onDbCommitted`` after a db commit. Folds to the
        commander, which deposits ``gnr.dbchanges.<table>`` on every subscribing page.
        """
        self._fold(
            "notifyDbEvents", (dbeventsDict,),
            dict(register_name=register_name, origin_page_id=origin_page_id, dbevent_reason=dbevent_reason, **kwargs),
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
        """Write the client's server-path changes into the page's local data Bag.

        Called at the start of every RPC (and inside ``handle_ping``) when the client
        sends ``_serverstore_changes``. Worker-local (channel A): stays on the page item.
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
        page data Bag and records the subscribed path.
        """
        item = self._ensure_item_data(self.local_item(page_id, "page"))
        if item is None or not pendingContext:
            return
        data = item["data"]
        subscribed = item.setdefault("subscribed_paths", [])
        for serverpath, value, attr in pendingContext:
            data.setItem(serverpath, value, attr)
            if isinstance(value, Bag):
                data.clearBackRef()
                data.setBackRef()
            if serverpath not in subscribed:
                subscribed.append(serverpath)

    # ==================================================================
    # The pull: subscription_storechanges + handle_ping (both channels)
    # ==================================================================

    def subscription_storechanges(self, user: Any, page_id: Any) -> list:
        """The page's pull, served in-process: the local pending list, no daemon.

        Called by ``WebPage.collectClientDatachanges`` at the end of every RPC. The
        page's queue lives on its own worker (switch model): channel D was already
        applied at the deposit, so *user* plays no part in the read.
        """
        return self._collect_local_datachanges(page_id)

    def handle_ping(self, page_id: Any = None, reason: Any = None, **kwargs: Any) -> Any:
        """The page's periodic ping, served in-process (= the daemon's ``handle_ping``).

        Called by ``gnrwsgisite.serve_ping`` on the polling endpoint. Refreshes the page
        (timestamps up to the user; a dead page answers ``False`` and the client stops),
        applies the client's serverstore changes (page and children), then builds the
        envelope: ``dataChanges`` (both channels), ``childDataChanges.<id>``, and the
        ``runningBatch`` flag from the user store's ``lastBatchUpdate``.
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
        """The subset of ``table_list`` with at least one subscribed page, from the surface.

        Called by ``site.getSubscribedTables`` on every db commit to decide whether to
        build and send the db events.
        """
        registry = self._app_registry
        if registry is None:
            return []
        return [table for table in (table_list or []) if registry.pages_subscribing(table)]

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
        """The cleanup-lottery gate: grant it when the interval elapsed (single node: us).

        Called by ``gnrwsgisite`` to decide whether this process runs the periodic
        page/connection eviction.
        """
        now = time.monotonic()
        last = self.__dict__.get("_last_cleanup_claim")
        if last is not None and (now - last) < float(interval or 60):
            return False
        self.__dict__["_last_cleanup_claim"] = now
        return True

    def expire_pages(self, max_age: Any = 120, **kwargs: Any) -> list:
        """Drop pages whose last refresh is older than ``max_age`` seconds."""
        expired = self._expired_keys("page", max_age or 120)
        for page_id in expired:
            self.drop_page(page_id, cascade=True)
        return expired

    def expire_connection(self, max_age: Any = 3600, **kwargs: Any) -> list:
        """Drop connections whose last refresh is older than ``max_age`` seconds."""
        expired = self._expired_keys("connection", max_age or 3600)
        for connection_id in expired:
            self.drop_connection(connection_id, cascade=True)
        return expired

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
        """The local register item (page/connection/user) from the worker, or None."""
        worker = self.spa_worker
        if worker is None or not register_name:
            return None
        return worker.dispatch("get_item", (register_item_id,), {"register_name": register_name})

    def _item_with_data(self, item_id: Any, register_name: str) -> dict | None:
        """The local item after a fold, with its data Bag attached (new_* return this)."""
        return self._ensure_item_data(self.local_item(item_id, register_name))

    def _ensure_item_data(self, item: dict | None) -> dict | None:
        """Give the item its in-process legacy Bag ``data`` (born on first access)."""
        if item is not None and not isinstance(item.get("data"), Bag):
            item["data"] = Bag()
        return item

    def _add_data_to_register_item(self, register_item: Any) -> Any:
        """The local Bag replaces the daemon's RemoteStoreBag proxy (compat name)."""
        return self._ensure_item_data(register_item)

    def _conn_kwargs(self, connection: Any, kwargs: dict) -> dict:
        """Extract the scalar fields the connection registry needs from a Connection.

        ``new_connection`` receives the legacy ``Connection`` object; the worker registry
        only wants scalars. When ``connection`` is None the caller already passed kwargs.
        """
        if connection is None:
            return kwargs
        out = dict(kwargs)
        for field in ("connection_name", "user", "user_id", "user_tags", "user_ip",
                      "user_agent", "browser_name", "avatar_extra", "user_name"):
            if field not in out:
                out[field] = getattr(connection, field, None)
        return out

    def _page_kwargs(self, page: Any, kwargs: dict) -> dict:
        """Extract the scalar fields the page registry needs from a WebPage."""
        if page is None:
            return kwargs
        out = dict(kwargs)
        for field in ("pagename", "connection_id", "user", "user_ip", "user_agent",
                      "relative_url"):
            if field not in out:
                out[field] = getattr(page, field, None)
        return out

    def _registers(self) -> Any:
        """The worker's registry handler (users/connections/pages), or None."""
        return getattr(self.spa_worker, "registry_handler", None)

    def _page_register(self) -> Any:
        registry = self._registers()
        return getattr(registry, "pages", None)

    def _filter_items(self, items: list, filters: Any) -> list:
        """The daemon's ad-hoc page filter grammar: ``name:regex AND name:value``."""
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
        """Propagate the refresh timestamps page -> connection -> user (= the daemon's).

        Returns the USER item (``handle_ping`` reads the user from it), or None when the
        chain is broken (a dead page: the ping answers False).
        """
        refresh_ts = datetime.datetime.now()
        page = self._refresh_item("page", page_id, last_user_ts, last_rpc_ts, refresh_ts)
        if not page:
            return None
        connection = self._refresh_item(
            "connection", page.get("connection_id"), last_user_ts, last_rpc_ts, refresh_ts
        )
        if not connection:
            return None
        return self._refresh_item("user", connection.get("user"), last_user_ts, last_rpc_ts, refresh_ts)

    def _refresh_item(self, register_name: str, item_id: Any, last_user_ts: Any, last_rpc_ts: Any, refresh_ts: Any) -> dict | None:
        item = self.local_item(item_id, register_name)
        if not item:
            return None
        for field, value in (
            ("last_user_ts", last_user_ts),
            ("last_rpc_ts", last_rpc_ts),
            ("last_refresh_ts", refresh_ts),
        ):
            if value is not None:
                current = item.get(field)
                item[field] = max(current, value) if current else value
        return item

    def _parse_typed(self, value: Any) -> Any:
        """Parse a typed-text value from the client wire (the daemon used its catalog)."""
        if isinstance(value, (bytes, str)):
            return self.catalog.fromTypedText(value)
        return value

    def _expired_keys(self, register_name: str, max_age: Any) -> list:
        registry = self._registers()
        if registry is None:
            return []
        register = registry.registers[register_name]
        now = datetime.datetime.now()
        expired = []
        for key, item in list(register.items()):
            last_seen = item.get("last_refresh_ts")
            if last_seen is None:
                continue  # never pinged: birth handling stays the site's business
            if (now - last_seen).total_seconds() > float(max_age):
                expired.append(key)
        return expired

    def _collect_local_datachanges(self, page_id: Any) -> list:
        """Drain the page's pending list from its OWN worker (the switch model).

        Every worker holds its pages' datachange queues locally; a cross-worker change
        was already deposited here by the commander's ``/datachange_in`` forward. One
        local read for the single and the pool child alike — no RPC, no mailbox.
        Returns legacy ``ClientDataChange`` objects.
        """
        app = self.spa_application
        if app is None:
            return []
        return [ClientDataChange(**raw) for raw in app.collect_datachanges(page_id)]

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


if __name__ == "__main__":
    pass
