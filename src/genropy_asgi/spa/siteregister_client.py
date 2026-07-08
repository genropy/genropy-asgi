# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyRegisterClient — the in-process register: every command served locally.

The GenroPy ``GnrWsgiSite`` talks to a register via ``site.register``, calling commands
(``new_connection``, ``new_page``, ``subscription_storechanges`` …) one at a time while
it serves. Historically that register was a daemon (Pyro4, then the genro-nodaemon TCP
daemon). This subclass is the DAEMONLESS register: the commands are served by the
SpaApplication's own machinery, no external process.

Who serves what (FIXED):

- **Lifecycle** (new/change/drop of connection/page/user, refresh): the worker's
  ``RegistryHandler`` registers, fed through ``worker.dispatch`` — the same fold that
  feeds the app-level surface. Reads (``page``/``pages``/``connections``/``users``/
  ``get_item``/``exists``) answer from those registers.
- **Datachanges**: channel C (``subscribeTable``/``notifyDbEvents`` -> surface +
  mailbox fan-out) and channel D (``setStoreSubscription`` surface +
  ``userStore().set_datachange`` queues with offset/dedup) live in the commander side
  of the app (the single is its own commander; a pool child reaches the commander at
  ``GENRO_COMMANDER_URL``). The pull (``subscription_storechanges``, ``handle_ping``)
  collects from there.
- **Stores**: each item's ``data`` is a real in-process legacy Bag (born lazily on the
  local register item); the ``ServerStore`` context manager works unchanged on top of
  the local ``lock_item``/``unlock_item`` (reentrant per reason, like the daemon's).
- **Global store**: a single stable legacy Bag (``global_bag``), write-by-reference.

NOT served in-process (explicit choices): ``dump``/``load`` (persistence is the future
Service Store), ``sendProcessCommand``/``pendingProcessCommands`` (inter-process bus —
the commander will host it, PROVISIONAL no-ops here). Any command outside the served
set raises ``NotImplementedError`` — no silent fallback.

Wiring: the ``GnrWsgiSite`` builds its own register client during ``__init__`` (the
``self.register`` touch it insists on), so this class is installed by REBINDING the
existing instance's class (``install_inprocess_register``) — no second ``__init__``,
captures stay valid. All extra state is created lazily through ``__dict__`` for the
same reason. The inherited TCP client (``self.siteregister``) becomes inert: nothing
calls it once every command is served here.
"""

from __future__ import annotations

import datetime
import os
import re
import threading
import time
from typing import Any

import httpx

from gnr.core.gnrbag import Bag
from gnr.core.gnrclasses import GnrClassCatalog
from gnr.web import logger
from gnr.web.gnrwebpage import ClientDataChange

from genro_asgi.applications.spa_application import LIFECYCLE_EVENTS_KEY
from genro_nodaemon.siteregister_client import SiteRegisterClient

# Datachange commands folded by the local dispatch (Worker POST_OPS): once dispatched
# they are fully applied (surface/mailbox) — answered True, nothing else to do.
LOCAL_POST_OPS = frozenset(
    {
        "subscribeTable",
        "notifyDbEvents",
        "setStoreSubscription",
        "set_datachange",
        "reset_datachanges",
        "drop_datachanges",
    }
)

# The 5-second window the daemon used for the runningBatch flag in the ping envelope.
RUNNING_BATCH_WINDOW = 5.0


class GenropyRegisterClient(SiteRegisterClient):
    """The in-process register client: every ``site.register`` command served locally.

    ``_sr_call`` first folds the command into the SpaApplication (``worker.dispatch``:
    registers + surface + mailbox), then answers it from a ``_serve_<command>`` handler.
    A command with no handler raises ``NotImplementedError`` — the served set is the
    contract, not a best effort.
    """

    # ------------------------------------------------------------------
    # Lazy state (the class is installed by rebind: no __init__ runs here)
    # ------------------------------------------------------------------

    @property
    def global_bag(self) -> Bag:
        """The in-process legacy Bag backing the global store (one stable object).

        ``get_item`` hands it back on every call so a ``setItem`` on it persists
        (write-by-reference), exactly as the daemon-backed store did. Read/write through
        ``__dict__`` because the base class has a catch-all ``__getattr__``.
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
        return getattr(self.spa_application, "worker", None)

    # ------------------------------------------------------------------
    # The command entry: fold locally, serve locally
    # ------------------------------------------------------------------

    def _sr_call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Fold the command into the in-process registries, then answer it locally.

        The dispatch feeds the worker's registers and, for the POST commands, the
        commander's surface/mailbox (the single applies to itself; a pool child sends
        up). The answer comes from the matching ``_serve_<command>`` handler; a command
        outside the served set is an explicit error, never a silent no-op.
        """
        self._dispatch_to_registries(method_name, args, kwargs)
        if method_name in LOCAL_POST_OPS:
            return True
        handler = getattr(type(self), f"_serve_{method_name}", None)
        if handler is None:
            raise NotImplementedError(
                f"register command {method_name!r} is not served in-process"
            )
        return handler(self, args, kwargs)

    def _dispatch_to_registries(self, op: str, args: tuple, kwargs: dict) -> None:
        """Route a command into the SpaApplication's worker (the canonical entry).

        Reaches the worker lazily through the site: the SpaApplication sets
        ``site.spa_application = self`` after creating the site, so by request time both
        the ref and the worker exist. ``worker.dispatch`` folds the command into the
        local registers and delivers the lifecycle event: on the single it applies to
        the app itself; on a pool child it rides the request's event sink (the
        synchronous rail — read here from the current request's environ, where the
        hosting mixin copied it) or falls to the outbox. Best-effort: a missing ref or
        a broken dispatch must never break the legacy register call.
        """
        worker = self.spa_worker
        if worker is None:
            return
        try:
            worker.dispatch(op, args, kwargs, events=self._request_events_sink())
        except Exception:
            logger.exception("registry dispatch failed for op %r", op)

    def _request_events_sink(self) -> Any:
        """The per-request lifecycle sink, from the current request's environ (or None)."""
        current_request = getattr(self.site, "currentRequest", None)
        environ = getattr(current_request, "environ", None)
        if not environ:
            return None
        return environ.get(LIFECYCLE_EVENTS_KEY)

    # ------------------------------------------------------------------
    # Local item access
    # ------------------------------------------------------------------

    def local_item(self, register_item_id: Any, register_name: str) -> dict | None:
        """The local register item (page/connection/user), or None if absent."""
        worker = self.spa_worker
        if worker is None:
            return None
        return worker.dispatch(
            "get_item", (register_item_id,), {"register_name": register_name}
        )

    def _ensure_item_data(self, item: dict | None) -> dict | None:
        """Give the item its in-process legacy Bag ``data`` (born on first access)."""
        if item is not None and not isinstance(item.get("data"), Bag):
            item["data"] = Bag()
        return item

    def _add_data_to_register_item(self, register_item: Any) -> Any:
        """The local Bag replaces the daemon's RemoteStoreBag proxy."""
        return self._ensure_item_data(register_item)

    def get_item(self, register_item_id: Any, include_data: Any = False, register_name: Any = None) -> Any:
        """Serve every item in-process: the global store Bag or a local register item."""
        if register_name == "global":
            return {"register_item_id": "*", "register_name": "global", "data": self.global_bag}
        return super().get_item(register_item_id, include_data=include_data, register_name=register_name)

    def page(self, page_id: Any, include_data: Any = None) -> Any:
        """The local page item enriched with the surface's ``subscribed_tables``.

        The commit path reads ``page(page_id)['subscribed_tables']`` for hidden
        transactions; the subscriptions live in the channel-C surface.
        """
        item = super().page(page_id, include_data=include_data)
        registry = self._app_registry
        if item is not None and registry is not None:
            item["subscribed_tables"] = set(registry.page_tables.get(page_id, ()))
        return item

    @property
    def _app_registry(self) -> Any:
        """The SpaApplication's surface (subscriptions), reached through the site."""
        return getattr(self.spa_application, "app_registry", None)

    # ------------------------------------------------------------------
    # The pull: subscription_storechanges + handle_ping (both channels)
    # ------------------------------------------------------------------

    def subscription_storechanges(self, user: Any, page_id: Any) -> list:
        """The page's pull, served in-process: channel C + channel D, no daemon.

        Called by ``collectClientDatachanges`` at the end of every RPC. The single asks
        its own commander-of-itself (page queue destructive + user-store offset scan);
        a pool child asks the commander over the synchronous RPC.
        """
        return self._collect_local_datachanges(page_id, user=user)

    def handle_ping(self, page_id: Any = None, reason: Any = None, **kwargs: Any) -> Any:
        """The page's ping, served in-process (= the daemon's ``handle_ping``).

        Refresh the page (timestamps propagate page -> connection -> user; a missing
        page answers ``False`` — the client stops pinging), apply the client's
        serverstore changes, do the same for each child page, then build the envelope:
        ``dataChanges`` (both channels), ``childDataChanges.<id>``, and the
        ``runningBatch`` flag from the user store's ``lastBatchUpdate``.
        """
        user_item = self._local_refresh(
            page_id,
            last_user_ts=kwargs.get("_lastUserEventTs"),
            last_rpc_ts=kwargs.get("_lastRpc"),
        )
        if not user_item:
            return False
        if kwargs.get("_serverstore_changes"):
            self._serve_set_serverstore_changes((page_id,), {"datachanges": kwargs["_serverstore_changes"]})
        children_info = kwargs.get("_children_pages_info") or {}
        for child_id, child_changes in list(children_info.items()):
            child_changes = dict(child_changes or {})
            child_user_ts = child_changes.pop("_lastUserEventTs", None)
            child_rpc = child_changes.pop("_lastRpc", None)
            child_changes.pop("_pageProfilers", None)
            if child_changes:
                self._serve_set_serverstore_changes((child_id,), {"datachanges": child_changes})
            self._local_refresh(
                child_id,
                last_user_ts=self._parse_typed(child_user_ts),
                last_rpc_ts=self._parse_typed(child_rpc),
            )
        envelope = Bag(dict(result=None))
        user = user_item.get("user")
        changes = self._changes_to_bag(self._collect_local_datachanges(page_id, user))
        if changes is not None:
            envelope.setItem("dataChanges", changes)
        for child_id in children_info:
            child_bag = self._changes_to_bag(self._collect_local_datachanges(child_id, user))
            if child_bag is not None:
                envelope.setItem(f"childDataChanges.{child_id}", child_bag)
        self._flag_running_batch(envelope, user_item)
        return envelope

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

    def _local_refresh(self, page_id: Any, last_user_ts: Any = None, last_rpc_ts: Any = None) -> dict | None:
        """Propagate the refresh timestamps page -> connection -> user (= the daemon's).

        Returns the USER item (the daemon did the same — ``handle_ping`` reads the user
        from it), or None when the chain is broken (dead page: the ping answers False).
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
        return self._refresh_item(
            "user", connection.get("user"), last_user_ts, last_rpc_ts, refresh_ts
        )

    def _refresh_item(
        self, register_name: str, item_id: Any, last_user_ts: Any, last_rpc_ts: Any, refresh_ts: Any
    ) -> dict | None:
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

    def _collect_local_datachanges(self, page_id: Any, user: Any = None) -> list:
        """The in-process pull: the single asks itself, a pool child asks the commander.

        Channel C (page queue, destructive) plus, when *user* is known, channel D (the
        user-store offset scan). Returns legacy ``ClientDataChange`` objects built from
        the raw mailbox dicts (``change_ts`` is stamped here, at collect time, as the
        daemon did).
        """
        app = self.spa_application
        if app is None:
            return []
        worker = getattr(app, "worker", None)
        if worker is not None and worker.name is not None:
            raw_changes = self._pull_commander_datachanges(page_id, user)
        else:
            raw_changes = app.collect_datachanges(page_id, user or None)
        return [ClientDataChange(**raw) for raw in raw_changes]

    def _pull_commander_datachanges(self, page_id: Any, user: Any = None) -> list:
        """Synchronous blocking pull from the commander (the legacy thread must wait).

        A pool child holds no mailbox: the queues live in the commander, reached with a
        plain synchronous ``httpx`` call (like the daemon call it replaces — this runs on
        the legacy WSGI thread, which must block until the changes are in hand). The
        commander address comes from ``GENRO_COMMANDER_URL``, injected at spawn.
        """
        url = os.environ.get("GENRO_COMMANDER_URL")
        if not url:
            return []
        client = self.__dict__.get("_commander_client")
        if client is None:
            client = httpx.Client(base_url=url, timeout=10.0)
            self.__dict__["_commander_client"] = client
        params = {"page_id": page_id}
        if user:
            params["user"] = user
        response = client.post("/_commander/datachanges", params=params)
        response.raise_for_status()
        return response.json().get("datachanges", [])

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

    # ------------------------------------------------------------------
    # Served commands (the _sr_call table). Each takes the raw (args, kwargs).
    # ------------------------------------------------------------------

    def _item_after_fold(self, args: tuple, kwargs: dict, register_name: str, key: str) -> dict | None:
        item_id = args[0] if args else kwargs.get(key)
        return self._ensure_item_data(self.local_item(item_id, register_name))

    def _serve_new_page(self, args: tuple, kwargs: dict) -> dict | None:
        return self._item_after_fold(args, kwargs, "page", "page_id")

    def _serve_new_connection(self, args: tuple, kwargs: dict) -> dict | None:
        return self._item_after_fold(args, kwargs, "connection", "connection_id")

    def _serve_new_user(self, args: tuple, kwargs: dict) -> dict | None:
        return self._item_after_fold(args, kwargs, "user", "user")

    def _serve_change_connection_user(self, args: tuple, kwargs: dict) -> dict | None:
        return self._item_after_fold(args, kwargs, "connection", "connection_id")

    def _serve_drop_page(self, args: tuple, kwargs: dict) -> None:
        page_id = args[0] if args else kwargs.get("page_id")
        app = self.spa_application
        if app is not None and page_id and hasattr(app, "mailbox"):
            app.mailbox.drop_page(page_id)

    def _serve_drop_pages(self, args: tuple, kwargs: dict) -> None:
        return None

    def _serve_drop_connection(self, args: tuple, kwargs: dict) -> None:
        return None

    def _serve_drop_connections(self, args: tuple, kwargs: dict) -> None:
        return None

    def _serve_drop_user(self, args: tuple, kwargs: dict) -> None:
        user = args[0] if args else kwargs.get("user")
        app = self.spa_application
        if app is not None and user and hasattr(app, "mailbox"):
            app.mailbox.drop_user(user)

    def _serve_refresh(self, args: tuple, kwargs: dict) -> dict | None:
        page_id = args[0] if args else kwargs.get("page_id")
        return self._local_refresh(
            page_id,
            last_user_ts=kwargs.get("last_user_ts"),
            last_rpc_ts=kwargs.get("last_rpc_ts"),
        )

    def _serve_get_item(self, args: tuple, kwargs: dict) -> dict | None:
        item_id = args[0] if args else kwargs.get("register_item_id")
        item = self.local_item(item_id, kwargs.get("register_name"))
        if kwargs.get("include_data"):
            self._ensure_item_data(item)
        return item

    def _serve_exists(self, args: tuple, kwargs: dict) -> bool:
        item_id = args[0] if args else kwargs.get("register_item_id")
        register_name = kwargs.get("register_name") or "page"
        return self.local_item(item_id, register_name) is not None

    def _serve_pages(self, args: tuple, kwargs: dict) -> list:
        registry = self._registers()
        if registry is None:
            return []
        connection_id = kwargs.get("connection_id")
        user = kwargs.get("user")
        pages_register = registry.pages
        if connection_id:
            keys = pages_register.keys_by("connection_id", connection_id)
            pages = [pages_register[k] for k in keys]
            if user:
                pages = [p for p in pages if p.get("user") == user]
        elif user:
            pages = [pages_register[k] for k in pages_register.keys_by("user", user)]
        else:
            pages = [item for _, item in pages_register.items()]
        return self._filter_items(pages, kwargs.get("filters"))

    def _serve_connections(self, args: tuple, kwargs: dict) -> list:
        registry = self._registers()
        if registry is None:
            return []
        user = kwargs.get("user")
        register = registry.connections
        if user:
            return [register[k] for k in register.keys_by("user", user)]
        return [item for _, item in register.items()]

    def _serve_users(self, args: tuple, kwargs: dict) -> list:
        registry = self._registers()
        if registry is None:
            return []
        return [item for _, item in registry.users.items()]

    def _registers(self) -> Any:
        worker = self.spa_worker
        return getattr(worker, "registry_handler", None)

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

    # --- locks: reentrant per reason, in-process (the ServerStore contract) ---

    def _serve_lock_item(self, args: tuple, kwargs: dict) -> bool:
        item_id = args[0] if args else kwargs.get("register_item_id")
        register_name = kwargs.get("register_name")
        if register_name == "global":
            return True  # single writer in-process: no critical section to guard
        key = (register_name, item_id)
        reason = kwargs.get("reason")
        with self.locks_mutex:
            held = self.item_locks.get(key)
            if held is None:
                self.item_locks[key] = {"reason": reason, "count": 1}
                return True
            if held["reason"] == reason:
                held["count"] += 1
                return True
            return False

    def _serve_unlock_item(self, args: tuple, kwargs: dict) -> bool:
        item_id = args[0] if args else kwargs.get("register_item_id")
        register_name = kwargs.get("register_name")
        if register_name == "global":
            return True
        key = (register_name, item_id)
        with self.locks_mutex:
            held = self.item_locks.get(key)
            if held is None:
                return True
            held["count"] -= 1
            if held["count"] <= 0:
                del self.item_locks[key]
            return True

    # --- page-data commands (channel A and friends) ---

    def _serve_set_serverstore_changes(self, args: tuple, kwargs: dict) -> None:
        """Write the client's server-path changes into the page's local data Bag."""
        page_id = args[0] if args else kwargs.get("page_id")
        datachanges = kwargs.get("datachanges") or (args[1] if len(args) > 1 else None)
        item = self._ensure_item_data(self.local_item(page_id, "page"))
        if item is None or not datachanges:
            return
        data = item["data"]
        for path, value in list(datachanges.items()):
            data.setItem(path, self._parse_typed(value))

    def _serve_setPendingContext(self, args: tuple, kwargs: dict) -> None:
        page_id = args[0] if args else kwargs.get("page_id")
        pending = args[1] if len(args) > 1 else kwargs.get("pendingContext")
        item = self._ensure_item_data(self.local_item(page_id, "page"))
        if item is None or not pending:
            return
        data = item["data"]
        subscribed = item.setdefault("subscribed_paths", [])
        for serverpath, value, attr in pending:
            data.setItem(serverpath, value, attr)
            if isinstance(value, Bag):
                data.clearBackRef()
                data.setBackRef()
            if serverpath not in subscribed:
                subscribed.append(serverpath)

    def _serve_get_dbenv(self, args: tuple, kwargs: dict) -> Bag:
        """Build the page's db environment Bag from its data (= the daemon's walk)."""
        page_id = args[0] if args else kwargs.get("register_item_id")
        item = self._ensure_item_data(self.local_item(page_id, "page"))
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

    def _serve_setInClientData(self, args: tuple, kwargs: dict) -> None:
        """Deposit a change on the target pages (single page or filter broadcast)."""
        path = args[0] if args else kwargs.get("path")
        filters = kwargs.get("filters")
        if filters:
            page_ids = [p["register_item_id"] for p in self._serve_pages((), {"filters": filters})]
        else:
            page_ids = [kwargs.get("page_id")]
        for pid in page_ids:
            if not pid:
                continue
            if isinstance(path, Bag):
                for change_node in path:
                    attr = dict(change_node.attr)
                    self._sr_call(
                        "set_datachange",
                        pid,
                        attr.pop("_client_path"),
                        value=change_node.value,
                        attributes=attr,
                        fired=attr.pop("fired", None),
                        register_name="page",
                    )
            else:
                self._sr_call(
                    "set_datachange",
                    pid,
                    path,
                    value=kwargs.get("value"),
                    reason=kwargs.get("reason"),
                    attributes=kwargs.get("attributes"),
                    fired=kwargs.get("fired", False),
                    replace=kwargs.get("replace", False),
                    register_name="page",
                )

    def _serve_filter_subscribed_tables(self, args: tuple, kwargs: dict) -> list:
        table_list = args[0] if args else kwargs.get("table_list") or []
        registry = self._app_registry
        if registry is None:
            return []
        return [table for table in table_list if registry.pages_subscribing(table)]

    def filter_subscribed_tables(self, table_list: Any, **kwargs: Any) -> list:
        """The subset of ``table_list`` with at least one subscribed page, from the surface.

        Called by ``site.getSubscribedTables`` on every db commit (with
        ``register_name='page'``, absorbed here — the surface only holds page
        subscriptions) to decide whether to build and send the db events.
        """
        return self._serve_filter_subscribed_tables((table_list,), kwargs)

    # --- maintenance / cleanup / process bus ---

    def _serve_setMaintenance(self, args: tuple, kwargs: dict) -> None:
        self.__dict__["_maintenance"] = bool(args[0] if args else kwargs.get("status"))
        self.__dict__["_allowed_users"] = kwargs.get("allowed_users")

    def _serve_isInMaintenance(self, args: tuple, kwargs: dict) -> bool:
        user = args[0] if args else kwargs.get("user")
        maintenance = self.__dict__.get("_maintenance", False)
        allowed = self.__dict__.get("_allowed_users")
        if not maintenance or user == "*forced*":
            return False
        if not user or not allowed:
            return maintenance
        return user not in allowed

    def _serve_allowedUsers(self, args: tuple, kwargs: dict) -> Any:
        return self.__dict__.get("_allowed_users")

    def _serve_claim_cleanup(self, args: tuple, kwargs: dict) -> bool:
        """Grant the cleanup lottery when the interval elapsed (single process: always us)."""
        interval = args[0] if args else kwargs.get("interval") or 60
        now = time.monotonic()
        last = self.__dict__.get("_last_cleanup_claim")
        if last is not None and (now - last) < float(interval):
            return False
        self.__dict__["_last_cleanup_claim"] = now
        return True

    def _serve_expire_pages(self, args: tuple, kwargs: dict) -> list:
        """Drop pages whose last refresh is older than ``max_age`` seconds."""
        max_age = args[0] if args else kwargs.get("max_age") or 120
        expired = self._expired_keys("page", max_age)
        for page_id in expired:
            self._sr_call("drop_page", page_id, cascade=True)
        return expired

    def _serve_expire_connection(self, args: tuple, kwargs: dict) -> list:
        max_age = args[0] if args else kwargs.get("max_age") or 3600
        expired = self._expired_keys("connection", max_age)
        for connection_id in expired:
            self._sr_call("drop_connection", connection_id, cascade=True)
        return expired

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

    def _serve_on_reloader_restart(self, args: tuple, kwargs: dict) -> None:
        return None

    def _serve_on_site_stop(self, args: tuple, kwargs: dict) -> None:
        return None  # persistence (dump) is the future Service Store's business

    def _serve_sendProcessCommand(self, args: tuple, kwargs: dict) -> None:
        return None  # inter-process bus: the commander will host it (PROVISIONAL)

    def _serve_pendingProcessCommands(self, args: tuple, kwargs: dict) -> list:
        return []

    def _serve_updatePageProfilers(self, args: tuple, kwargs: dict) -> None:
        return None

    # --- persistence: not served (the future Service Store) ---

    def dump(self) -> None:
        logger.info("register dump skipped: no in-process persistence yet")

    def load(self) -> None:
        logger.info("register load skipped: no in-process persistence yet")


def install_inprocess_register(site: Any) -> GenropyRegisterClient:
    """Promote the site's existing register client to the in-process one, in place.

    The ``GnrWsgiSite`` has already built its register client during its own ``__init__``
    (and captured ``.siteregister`` elsewhere, e.g. the DataCollector). Building a second
    client would leave a stale capture, so the existing instance's CLASS is rebound: our
    overrides apply, no second ``__init__``, existing refs stay valid. All the extra
    state this class needs is created lazily through ``__dict__``, so the rebind is safe.
    """
    client = site.domains[site.rootDomain].register
    client.__class__ = GenropyRegisterClient
    return client


if __name__ == "__main__":
    pass
