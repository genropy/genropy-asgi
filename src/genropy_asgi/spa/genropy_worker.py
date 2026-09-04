# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyWorker: the core ``SpaWorker`` hosting a ``GnrWsgiSite``.

The execution unit of the GenroPy bridge. The core worker owns the site
verbs, the registers and the lane to its commander; this subclass adds
exactly the legacy site:

- the constructor takes ``source`` (a site name or a site directory path)
  and ``debug``, builds the ``GnrWsgiSite`` (the Werkzeug debugger wrapper
  when ``debugger``, ``site._local_mode = True``, atexit ``on_site_stop``) and
  assigns the possibly-wrapped site to ``self.wsgi_app`` — the core's
  consumer seam for the http CALL form;
- the site's lazy per-process state is settled right after creation,
  single-threaded (``site.resources_dirs``; ``site.storage("gnr")`` —
  genropy#984): the first concurrent request must not race the resource
  scan;
- the idle valve follows the site: unless the caller named
  ``user_idle_freeze_minutes``, it is read from the site's ``<cleanup>``
  section as ``connection_max_age`` seconds (the one legacy age with an
  equivalent — the silence past which the legacy register dropped a logged
  connection, and this worker parks the user in the freezer instead);
  ``page_max_age`` and ``guest_max_age`` have no equivalent on this base: a
  silent tab's row lives until the site drops it or its user freezes, and a
  guest is distinguished only at the commander's frozen expiry;
- ``site.spa_worker = self`` (the ratified name) is how the in-process
  register client reaches the worker's site verbs, and the client itself is
  captured at construction (the site's lazy ``register`` property does
  db-touching work that must not run on the event loop);
- the client reads ``user_items``/``connection_items``/``page_items``:
  translating properties over the core's ``*_register`` names (decision
  §7a, 2026-08-20 — the core keeps its names, the bridge translates);
- ``drop_page`` keeps the legacy ``cascade`` flag and absorbs it (decision
  D7, 2026-08-20): the core demolition — drop the page, and the connection
  and user its departure empties — is the sanctioned semantics;
- the three drop verbs remove the connection folders under
  ``data/_connections`` together with the rows. Freeze and transfer use the
  registry's internal removers and are deliberately not hooked: a frozen or
  moved user's folders must survive for the wake. A frozen user the
  commander expires leaves folders behind — the declared debt that replaces
  the retired orphan sweep;
- ``build_registry()`` returns :class:`~genropy_asgi.spa.genropy_register.GenropyRegistry`:
  the site's page row (:class:`~genropy_asgi.spa.genropy_register.GenropyPageRow`)
  and legacy Bag stores under :class:`~genropy_asgi.spa.legacy_bag.LegacyBagCollector`
  capture — the registry module says how;
- ``exit_process()`` stops the site (``on_site_stop``) before the core
  teardown.
- the site's verbs of the data plane (genro-asgi #59: they left the core and
  live here): ``setStoreSubscription``, ``subscribeTable``, ``notifyDbEvents``,
  ``set_datachange``, ``reset_datachanges``, ``drop_datachanges``,
  ``collect_page`` — the register client calls them by name on
  ``site.spa_worker``. An addressed write to a page of the caller's own user
  living here lands on the row at once; every other address climbs to the desk
  as ONE CALL on ``/commander/delivery/on_datachange``; a subscription is
  filed at the desk synchronously; a commit's deposits accumulate on the
  request's own slot (:class:`GenropyRequestSlot`) and leave at its end — in
  ``collect_page``'s exchange, or alone through ``deliver_slot_deposits`` from
  ``on_request_served``. ``subscribed_tables`` is the source filter the desk
  pushes down on ``/commander/delivery/subscribed_tables``
  (:class:`DeliveryOrders`); ``_install_carried_store`` re-attaches the
  ``user_view`` of every watching page on the carried Bag.

This module imports ``gnr.*`` at the top BY DESIGN (via ``genropy_register``):
it is loaded through the ``worker_class`` dotted path only where GenroPy
is installed.
"""

from __future__ import annotations

import heapq
import logging
import os
import shutil
import time
from collections.abc import Callable
from typing import Any

from genro_asgi.spa import RegisterRegistry
from genro_asgi.spa.orchestration import SpaWorker
from genro_asgi.spa.orchestration.spa_worker import RequestSlot
from genro_asgi.spa.orchestration.worker_connector import CommanderCallFailed
from genro_routes import RoutingClass, route
from genro_tytx import from_tytx

from .delivery_desk import STATE_KINDS
from .genropy_register import GenropyRegistry
from .site_engine_factory import GenropySiteEngineFactory

log = logging.getLogger("genropy_asgi.spa")

__all__ = [
    "DESK_DEPOSIT_PATH",
    "DESK_EXCHANGE_PATH",
    "DESK_ON_DATACHANGE_PATH",
    "DESK_SUBSCRIBE_TABLE_PATH",
    "SIGNAL_KIND",
    "STATE_KINDS",
    "DeliveryOrders",
    "GenropyRegistry",
    "GenropyRequestSlot",
    "GenropyWorker",
]

# The one legacy <cleanup> age with an equivalent on this base: the seconds of
# silence past which the legacy register dropped a logged connection become
# the minutes past which the core's valve parks the user in the freezer.
# 7200s (the daemon-parity connection_max_age) -> 120 minutes.
IDLE_FREEZE_LEGACY_KEY = "connection_max_age"
IDLE_FREEZE_DEFAULT_SECONDS = 7200

#: The routing keys of the lane going UP are paths on the tree the commander
#: hosts: ``delivery`` is the branch :class:`~genropy_asgi.spa.delivery_desk.
#: DeliveryDesk` serves. The call that files a page's table subscription at the
#: desk: it goes up at once and synchronously, so the index is already right
#: when the request that subscribed commits in the same breath.
DESK_SUBSCRIBE_TABLE_PATH = "/commander/delivery/subscribe_table"

#: The routing key of the end-of-request exchange: what this request produced
#: goes up, what waits for its page comes back.
DESK_EXCHANGE_PATH = "/commander/delivery/exchange"

#: The routing key of the end-of-request deposit: what the slot still holds
#: when no collect carried it away goes up alone, and nothing comes back.
DESK_DEPOSIT_PATH = "/commander/delivery/deposit"

#: The routing key one addressed write climbs the moment its verb is called:
#: it is filed at the desk at once, so a request that never collects loses
#: nothing, and the answer says whether the target exists at all.
DESK_ON_DATACHANGE_PATH = "/commander/delivery/on_datachange"

#: The address kind that names a page itself: the change is a SIGNAL and lands
#: as a deposit on that page's queue at the desk — no Bag write, no residue.
#: The STATE kinds — the ones that name a store — are ``STATE_KINDS``, declared
#: with the desk and imported here: one word, one module.
SIGNAL_KIND = "page"


class GenropyRequestSlot(RequestSlot):
    """The core's request slot plus the table-event deposits of a hosted site's request.

    ``dbevents`` are the deposits that go up the lane at the end of the request
    (inside ``collect_page``'s exchange, or on their own through
    ``deliver_slot_deposits``); ``own_dbevents`` are the ``local_only`` deposits
    of the hidden transaction, which never leave this process and reach the
    origin page's own collect alone. The addressed writes are NOT here: each one
    leaves at once, on its own CALL, the moment its verb is called.
    """

    def __init__(self) -> None:
        super().__init__()
        self.dbevents: list[dict[str, Any]] = []
        self.own_dbevents: list[dict[str, Any]] = []


class DeliveryOrders(RoutingClass):
    """The ``delivery`` branch of the worker's ``commander_orders``: what the desk pushes down.

    Args:
        spa_worker: the process the order acts on.
    """

    def __init__(self, spa_worker: Any) -> None:
        self.spa_worker = spa_worker

    @route()
    def subscribed_tables(self, tables: list[str] | None = None) -> dict[str, Any]:
        """Replace the source filter with the whole set the commander pushes."""
        with self.spa_worker.dispatch_lock:
            self.spa_worker.subscribed_tables = set(tables or ())
        return {}


class GenropyWorker(SpaWorker):
    """A ``SpaWorker`` whose hosted WSGI app is a ``GnrWsgiSite``."""

    def __init__(
        self,
        name: str,
        *,
        source: str,
        debug: bool = False,
        debugger: bool = False,
        group_engine: Any = None,
        **kwargs: Any,
    ) -> None:
        """Args:
        name: the worker's name (the one its handler minted).
        source: the GenroPy site — a site name or a site directory path.
            Ignored when ``group_engine`` arrives: the site is already built.
        debug: True builds the site in debug mode — the SQL time counters,
            ``pageModule`` in the page's own bootstrap, the developer's extras.
        debugger: True wraps the site in the Werkzeug debugger middleware, whose
            error page carries a traceback AND a console that evaluates Python
            inside the process. Its own switch since 2026-08-26, and off by
            default: it must never come on as a side effect of a flag somebody
            set to get the SQL counters. GenroPy keeps the two separate too —
            ``site.debug`` comes from the configuration, the werkzeug wrapper
            only from ``serveprod --debug``.
        group_engine: the ``GnrWsgiSite`` the group's template built, handed
            to a worker born by fork (fork contract §8, 2026-08-24). When it
            is None the worker was spawned and builds its own site.
        kwargs: forwarded to ``SpaWorker`` — the spawn grammar
            (``freeze_handler``, ``group``, the pools) plus the policies.
            ``user_idle_freeze_minutes`` is the one exception: core 0.36 moved
            it to the group, so it is taken out here and kept on this worker.
            Not named at all, it is read from the site's ``<cleanup>`` section
            (``connection_max_age`` seconds, 7200 where the site is silent):
            a caller's value always wins over the site's.
        """
        # The valve is the bridge's own since core 0.36: ``SpaWorker`` no longer
        # takes it, the group does. A caller's value is therefore kept HERE and
        # never forwarded — passing it on raises TypeError — while the site's
        # own ``<cleanup>`` answers when the caller named none. Which of the two
        # wins is unchanged: the caller's.
        caller_idle = kwargs.pop("user_idle_freeze_minutes", None)
        idle_from_site = caller_idle is None
        super().__init__(name, **kwargs)
        #: The source filter: every table some page anywhere subscribes. Fed
        #: ONLY by the ``/commander/delivery/subscribed_tables`` order the
        #: commander pushes on every transition of the set and at the
        #: presentation; ``notifyDbEvents`` filters the site's commits on it.
        self.subscribed_tables: set[str] = set()
        self.worker_dispatcher.commander_orders.add_branches(
            [{"name": "delivery", "instance": DeliveryOrders(self)}]
        )
        if not idle_from_site:
            self.user_idle_freeze_minutes = float(caller_idle)
        # The forked worker receives the site its template built; the spawned
        # one builds its own. Nothing below this line differs between them.
        self._gnr_site = group_engine
        if self._gnr_site is None:
            self._gnr_site = self._create_site(source, debug)
        if idle_from_site:
            cleanup = self._gnr_site.custom_config.getAttr("cleanup") or {}
            seconds = int(cleanup.get(IDLE_FREEZE_LEGACY_KEY) or IDLE_FREEZE_DEFAULT_SECONDS)
            self.user_idle_freeze_minutes = seconds / 60.0
        # The register client, materialized HERE on the init thread: the legacy
        # ``site.register`` property builds it lazily, unlocked, down to db work
        # (``checkPendingConnection``), which must not happen on the event loop
        # while the first request is already running on a pool thread.
        self._register_client = self._gnr_site.register
        self.wsgi_app = self._gnr_site
        if debugger:
            # Deferred import, transcribed from the pre-rebase host: importing
            # gnr.web at module top would drag the register client (and with it
            # the whole site machinery) into every import of this module.
            from gnr.web.serverwsgi import GnrDebuggedApplication

            self.wsgi_app = GnrDebuggedApplication(self._gnr_site, evalex=True, pin_security=False)
        # Settle the site's lazy per-process state HERE, single-threaded:
        # ``resources_dirs`` is published and only then reversed in place, and
        # the uncached service scan it drives would let a first concurrent
        # request iterate a torn list (genropy#984). ``storage('gnr')`` and
        # ``storage('dojo')`` are exactly what the first request resolves in
        # ``build_arg_dict``.
        #
        # `dojo` joined the list on 2026-08-26, measured by the twin proxy: a
        # worker born for one user was still instantiating that service inside
        # its first page — six register calls the legacy did not make there, and
        # 291 ms against 100. The legacy makes them too, at the startup of its
        # one long-lived process. Settling them here puts them where the legacy
        # has them: outside the request, in the birth.
        self._gnr_site.resources_dirs
        self._gnr_site.storage("gnr")
        self._gnr_site.storage("dojo")
        self._gnr_site.spa_worker = self

    def build_registry(self) -> RegisterRegistry:
        """The registry factory: legacy stores under legacy capture."""
        return GenropyRegistry()

    @property
    def gnr_site(self) -> Any:
        """The hosted ``GnrWsgiSite`` instance (unwrapped)."""
        return self._gnr_site

    @property
    def user_items(self) -> Any:
        """The user register under the name the legacy client reads (§7a)."""
        return self.user_register

    @property
    def connection_items(self) -> Any:
        """The connection register under the name the legacy client reads (§7a)."""
        return self.connection_register

    @property
    def page_items(self) -> Any:
        """The page register under the name the legacy client reads (§7a)."""
        return self.page_register

    def apply_forwarded(self, bag: Any, change: dict[str, Any]) -> None:
        """Apply a change born elsewhere to a LEGACY Bag (STATE delivery).

        The core writes with the new Bag's API (``set_item``/``_fired``); the
        bridge stores are legacy Bags, so the write is translated: ``setItem``
        with the attributes and the reason, ``pop(path, _reason=...)`` for a
        delete (the legacy signature carries it). A fired change rides the
        transient ``_fired`` attribute for the capture (the legacy ``setItem``
        has no ``_fired`` parameter — the collectors pop it from their local
        copy, see ``legacy_bag``), then the node is cleaned: the attribute
        removed and the static value reset — the same one-shot semantics
        ``set_item(_fired=True)`` has on the core Bag.
        """
        path = change["key"]["path"]
        reason = change["key"]["reason"]
        if change["delete"]:
            bag.pop(path, _reason=reason)
            return
        attributes = dict(change["attributes"] or {})
        attributes["_original_ts"] = change["change_ts"]
        fired = change["key"]["fired"]
        if fired:
            attributes["_fired"] = True
        bag.setItem(path, change["value"], _attributes=attributes, _reason=reason)
        if fired:
            node = bag.getNode(path)
            node.attr.pop("_fired", None)
            node.staticvalue = None

    # ------------------------------------------------------------------
    # The request slot: what one request produces, and its two exits
    # ------------------------------------------------------------------

    def build_request_slot(self) -> RequestSlot:
        """The slot of one request, with the table-event deposits of a hosted site's request."""
        return GenropyRequestSlot()

    def on_request_served(self) -> None:
        """The end of every served request, failed ones included: what the slot still holds goes up."""
        self.deliver_slot_deposits()

    def deliver_slot_deposits(self) -> None:
        """Deliver what the slot still holds, at the end of a request that never collected.

        Empties the slot's ``dbevents`` through the desk's own deposit op, which
        files them in the subscribers' queues and retires nothing: there is no
        page to answer. ``own_dbevents`` — the hidden transaction — are NOT
        delivered here: they belong to the origin page's own collect and never
        leave this process. Called on the pool thread, like ``collect_page``;
        after a collect the slot is empty, so it delivers nothing twice.

        A desk that refuses the deposit is logged and the deposits are dropped,
        never raised: this runs in the ``finally`` of the stitching, where an
        exception of its own would replace the site's — the lost deposits are
        the same class of loss as a worker dying between commit and delivery.
        """
        slot = self.request_slot
        if not slot.dbevents:
            return
        try:
            self.run_on_loop(self.call(DESK_DEPOSIT_PATH, {"dbevents": slot.dbevents}))
        except CommanderCallFailed:
            self._logger.exception(
                "Worker %s: %d deposits lost, the desk refused the end-of-request deposit",
                self.name,
                len(slot.dbevents),
            )
        slot.dbevents = []

    # ------------------------------------------------------------------
    # The site's verbs: subscriptions, the collect, the addressed writes
    # ------------------------------------------------------------------

    def setStoreSubscription(  # noqa: N802 - reserved protocol name
        self,
        identity: str,
        page_id: str,
        storename: str,
        prefix: str,
        active: bool = True,
    ) -> dict[str, Any]:
        """Open (or close) a page's window onto a store, by path prefix.

        Args:
            identity: the user the calling site speaks for.
            page_id: the page whose window moves.
            storename: ``'page'`` for the page's own store, ``'user'`` for the
                view onto its owner's.
            prefix: the path prefix the window covers.
            active: opening it, or closing it.

        Returns:
            The page register item.

        Raises:
            KeyError: no such page here.
            ValueError: any other storename — an impossible address.

        Moves the row's ``subscribed_paths``, which the capture reads at event
        time: the set is what a move packages and what the filter consults.
        """
        with self.dispatch_lock:
            page = self.page_register.get(page_id)
            if page is None:
                raise KeyError(f"setStoreSubscription: unknown page {page_id!r}")
            if storename == "page":
                with page["item_lock"]:
                    if active:
                        page["subscribed_paths"].add(prefix)
                    else:
                        page["subscribed_paths"].discard(prefix)
            elif storename == "user":
                if active:
                    self.registry.subscribe_store_path(page_id, prefix)
                elif page["user_view"] is not None:
                    page["store_subscriptions"].discard(prefix)
                    page["user_view"].unsubscribe_path(prefix)
            else:
                raise ValueError(f"setStoreSubscription: no store named {storename!r}")
            return page

    def collect_page(self, page_id: str) -> dict[str, Any]:
        """End the request: exchange with the desk, then drain everything for one page.

        Args:
            page_id: the page the delivery is for.

        Returns:
            ``{"datachanges": [...], "dbevents": [...]}`` — the row's queue and
            the queue the desk handed back, merged in ARRIVAL order (the
            ``arrival_ts`` each list was stamped with as it grew), with the
            ``user_view`` drain merged in on its own ``change_ts``; the deposits
            are their own species in their own key, never dressed as
            datachanges.

        Raises:
            KeyError: no such page here.
            CommanderCallFailed: the desk refused the exchange.

        Empties the request slot, the row's queue, the user view and — through
        the exchange — the page's queues at the desk. Each list is already in
        its own arrival order — the row is appended under its lock, the desk's
        queue on the commander's one loop, the two on the same wall clock — so
        a two-way merge reproduces the order the daemon's single list would
        have had, and nothing is sorted; the merged list is numbered as one
        list, then the row's index goes back to zero. Nothing is discarded for
        its age: what waits is delivered whatever its age, as the daemon did.
        The exchange happens on EVERY request, empty-handed included: retiring
        what waits is the reason it exists. The STATE writes it brings back are
        applied to the user's own Bag BEFORE the drain, so the page that retired
        them reads them in this very delivery and its siblings capture them on
        their own ``user_view``.
        """
        with self.dispatch_lock:
            if self.page_register.get(page_id) is None:
                raise KeyError(f"collect_page: unknown page {page_id!r}")
            user = self.registry.page_user(page_id)
        slot = self.request_slot
        reply = self.run_on_loop(
            self.call(
                DESK_EXCHANGE_PATH,
                {"page_id": page_id, "user": user, "dbevents": slot.dbevents},
            )
        )
        slot.dbevents = []
        with self.dispatch_lock:
            page = self.page_register.get(page_id)
            if page is None:
                raise KeyError(f"collect_page: unknown page {page_id!r}")
            user_item = self.user_register.get(user)
            with user_item["item_lock"]:
                store = user_item["store"]
                for change in from_tytx(reply["store_changes"], "json"):
                    self.apply_forwarded(store, change)
            with page["item_lock"]:
                desk_changes = from_tytx(reply["datachanges"], "json")
                datachanges = list(
                    heapq.merge(page["datachanges"], desk_changes, key=self._arrival_order)
                )
                page["datachanges"] = []
                page["datachanges_idx"] = 0
                if page["user_view"] is not None:
                    datachanges = list(
                        heapq.merge(
                            datachanges, page["user_view"].drain(), key=self._arrival_order
                        )
                    )
            for index, change in enumerate(datachanges, start=1):
                change["change_idx"] = index
            dbevents = reply["dbevents"] + slot.own_dbevents
            slot.own_dbevents = []
        return {"datachanges": datachanges, "dbevents": dbevents}

    def _arrival_order(self, change: dict[str, Any]) -> float:
        """The instant a change joined its queue: ``arrival_ts``, or the write's own clock.

        The row and the desk stamp ``arrival_ts``; the ``user_view`` collector
        does not (the user store is another round), so its changes take their
        ``change_ts`` — the same wall clock, read at the write.
        """
        arrival_ts = change.get("arrival_ts")
        return arrival_ts if arrival_ts is not None else change["change_ts"].timestamp()

    def _route_datachange(
        self,
        op: str,
        identity: str,
        kind: str,
        target: str | None,
        filters: str | None,
        message: dict[str, Any],
        act_on_row: Callable[[dict[str, Any]], None],
    ) -> dict[str, bool]:
        """Take one addressed write to its road: the target row here, or the desk.

        Args:
            op: the verb being routed, named in the errors and in the message.
            identity: the user the calling site speaks for.
            kind: what ``target`` names — a page (the SIGNAL address) or a store.
            target: the addressed page.
            filters: the broadcast address, whose delivery is the second pass's.
            message: what the op adds to the desk message — ``change`` and
                ``replace`` for a write, ``path`` for a drop.
            act_on_row: what the verb does on the row when the road is local;
                called with the row, under its ``item_lock``.

        Returns:
            ``{"local": ..., "filed": ...}`` — ``local`` True when the write
            stayed here; ``filed`` False when the desk holds nobody by that
            name and the write went nowhere, as the daemon's silent return on a
            missing item. The verbs carry both into their answer.

        Raises:
            NotImplementedError: a ``filters`` broadcast, or a STATE kind other
                than ``user_store`` — nothing local can serve them yet, and a
                silent success would be a write into nowhere.
            CommanderCallFailed: the desk refused the write.

        A page of the caller's OWN user, living here, is acted on at once, under
        ``dispatch_lock`` then the row's ``item_lock`` — the row cannot leave the
        register between the two — so the write is in the parcel before any
        freeze. Every other address leaves at once as ONE CALL to the desk, from
        this very thread: an unservable write fails alone, in the caller's own
        call, and a request that never collects loses nothing. Whether the
        target EXISTS is the desk's judgment: a worker knows its own rows only,
        and an unknown target is reported, never raised — a page closed a moment
        ago, or born in this very request and not yet announced, is not an
        error of the caller's.
        """
        if filters is not None:
            raise NotImplementedError(f"{op}: filtered addresses are not delivered by this pass")
        if kind in STATE_KINDS and kind != "user_store":
            raise NotImplementedError(f"{op}: kind {kind!r} is not delivered by this pass")
        with self.dispatch_lock:
            page = self.page_register.get(target) if kind == SIGNAL_KIND else None
            if page is not None and self.registry.page_user(target) == identity:
                with page["item_lock"]:
                    act_on_row(page)
                return {"local": True, "filed": True}
        answer = self.run_on_loop(
            self.call(
                DESK_ON_DATACHANGE_PATH,
                {"op": op, "kind": kind, "target": target, "filters": filters, **message},
            )
        )
        return {"local": False, "filed": bool(answer["filed"])}

    def set_datachange(
        self,
        identity: str,
        change: str,
        kind: str = SIGNAL_KIND,
        target: str | None = None,
        filters: str | None = None,
        replace: bool = False,
        **addressing: Any,
    ) -> dict[str, Any]:
        """Write a change toward an addressed target, bypassing its filter.

        Args:
            identity: the user the calling site speaks for.
            change: the TYTX-encoded change dict.
            kind: what ``target`` names — a page (the SIGNAL address) or a
                store.
            target: the addressed page.
            filters: the alternative address, a broadcast over the pages a
                filter selects.
            replace: coalesce with the pending change of the same key — same
                path, same reason, same fired — so a value written over and
                over reaches the browser once.
            addressing: the caller's own ``page_id``, the pull cycle of the
                call and never the target of the write.

        Returns:
            The address the write took, as it was resolved; ``local`` is True
            when it stayed on a row here, ``filed`` False when nobody holds the
            target and the write went nowhere.

        Raises:
            NotImplementedError, CommanderCallFailed: see ``_route_datachange``.

        On the local road the change goes on the target row through the same
        append the store subscriber uses, so the row keeps one list and one
        index.
        """
        road = self._route_datachange(
            "set_datachange",
            identity,
            kind,
            target,
            filters,
            {"replace": replace, "change": change},
            lambda page: self.registry.append_page_datachange(
                page, from_tytx(change, "json"), replace=replace
            ),
        )
        return {
            "kind": kind,
            "target": target,
            "filters": filters,
            "replace": replace,
            **road,
        }

    def reset_datachanges(
        self,
        identity: str,
        target: str | None = None,
        filters: str | None = None,
        **addressing: Any,
    ) -> dict[str, Any]:
        """Empty the pending changes of the addressed page without reading them.

        Args:
            identity: the user the calling site speaks for.
            target: the addressed page.
            filters: the alternative address.
            addressing: the caller's own ``page_id``.

        Returns:
            The address the reset took; ``local`` is True when it emptied a row
            here, ``filed`` False when nobody holds the target.

        Raises:
            NotImplementedError, CommanderCallFailed: see ``_route_datachange``.

        On the local road the row's list and index go back to empty; on the
        desk's, the queue it keeps for that page.
        """
        road = self._route_datachange(
            "reset_datachanges",
            identity,
            SIGNAL_KIND,
            target,
            filters,
            {},
            lambda page: page.update(datachanges=[], datachanges_idx=0),
        )
        return {"target": target, "filters": filters, **road}

    def drop_datachanges(
        self,
        identity: str,
        path: str,
        target: str | None = None,
        filters: str | None = None,
        **addressing: Any,
    ) -> dict[str, Any]:
        """Discard the pending changes under one path of the addressed page.

        Args:
            identity: the user the calling site speaks for.
            path: the prefix whose pending changes go.
            target: the addressed page.
            filters: the alternative address.
            addressing: the caller's own ``page_id``.

        Returns:
            The address the drop took and the path it named; ``local`` is True
            when it pruned a row here, ``filed`` False when nobody holds the
            target.

        Raises:
            NotImplementedError, CommanderCallFailed: see ``_route_datachange``.

        The prefix is matched on segment boundaries, on the row here or in the
        desk's queue for that page.
        """

        def prune(page: dict[str, Any]) -> None:
            page["datachanges"][:] = [
                pending
                for pending in page["datachanges"]
                if not (
                    pending["key"]["path"] == path
                    or pending["key"]["path"].startswith(f"{path}.")
                )
            ]

        road = self._route_datachange(
            "drop_datachanges", identity, SIGNAL_KIND, target, filters, {"path": path}, prune
        )
        return {"target": target, "filters": filters, "path": path, **road}

    # ------------------------------------------------------------------
    # The table events: their own ops, the desk's index, their own species
    # ------------------------------------------------------------------

    def subscribeTable(  # noqa: N802 - reserved protocol name
        self,
        identity: str,
        table: str,
        page_id: str,
        subscribe: bool = True,
        subscribeMode: str | None = None,  # noqa: N803 - reserved protocol name
    ) -> dict[str, Any]:
        """Subscribe (or unsubscribe) the calling page to a table's events.

        Args:
            identity: the user the calling site speaks for.
            table: the table whose events the page wants.
            page_id: the caller's own page — the subscriber is whoever asks, so
                there is no target to address.
            subscribe: opening the subscription, or closing it.
            subscribeMode: vestigial, accepted and ignored exactly as the daemon
                does: callers still pass it, and refusing it would break them at
                mount time.

        Returns:
            The subscription as it was taken.

        Raises:
            KeyError: no such page here.

        Moves the row's ``table_subscriptions`` set — what a move packages —
        and then files the interest at the desk, which is the only index there
        is. The call is synchronous: when this request goes on to commit, the
        index it just changed is already right, so a site that subscribes a
        table and commits it in the same request finds the interest filed. The
        source filter of this process is not touched here: the commander
        pushes it.
        """
        with self.dispatch_lock:
            page = self.page_register.get(page_id)
            if page is None:
                raise KeyError(f"subscribeTable: unknown page {page_id!r}")
            if subscribe:
                page["table_subscriptions"].add(table)
            else:
                page["table_subscriptions"].discard(table)
        self.run_on_loop(
            self.call(
                DESK_SUBSCRIBE_TABLE_PATH,
                {"page_id": page_id, "table": table, "subscribe": subscribe},
            )
        )
        return {"page_id": page_id, "table": table, "subscribe": subscribe}

    def notifyDbEvents(  # noqa: N802 - reserved protocol name
        self,
        identity: str,
        dbevents: dict[str, Any],
        reason: str | None = None,
        page_id: str | None = None,
        local_only: bool = False,
        **addressing: Any,
    ) -> dict[str, Any]:
        """Announce a commit's table events to the pages that subscribed them.

        Args:
            identity: the user the calling site speaks for.
            dbevents: ``{table: batch}`` as the commit produced it.
            reason: what the commit was, carried through to the subscribers.
            page_id: the origin page — the caller's own — travelling as
                ``from_page_id`` so a subscriber can tell its own commit from
                somebody else's.
            local_only: the hidden transaction, whose events belong to the page
                that made them and to nobody else: the deposits stay on the
                slot for the origin page's own collect and never reach the wire.
            addressing: what the desk would read of the address; nothing reads
                it while every deposit is announced by its table alone.

        Returns:
            The tables actually announced.

        Lays the deposits on the request slot, which has two exits: the exchange
        inside ``collect_page`` when the page collects, and ``deliver_slot_deposits``
        at the end of the request otherwise. Filtered at the source: a table no
        page anywhere subscribes is not announced at all — a thousand events
        nobody wants die here rather than on the wire — and neither is a table
        whose batch is empty. The deposits are shaped once, so every subscriber
        reads the very same object and the origin's own ``ts``.
        """
        deposits = [
            self.dbevent_deposit(table, batch, page_id, reason)
            for table, batch in (dbevents or {}).items()
            if batch and (local_only or table in self.subscribed_tables)
        ]
        slot = self.request_slot
        if local_only:
            slot.own_dbevents.extend(deposits)
        else:
            slot.dbevents.extend(deposits)
        return {"tables": [deposit["table"] for deposit in deposits]}

    def dbevent_deposit(
        self,
        table: str,
        batch: Any,
        from_page_id: str | None,
        reason: str | None,
    ) -> dict[str, Any]:
        """The deposit one table's batch becomes on the slot and in the desk's queues.

        Args:
            table: the table the batch belongs to.
            batch: the events as the commit produced them.
            from_page_id: the origin page.
            reason: what the commit was.

        Returns:
            The shaped deposit, JSON by construction — ``ts`` an epoch float,
            the batch what the caller handed over — so it rides the rail as it
            is.
        """
        return {
            "table": table,
            "batch": batch,
            "from_page_id": from_page_id,
            "reason": reason,
            "ts": time.time(),
        }

    def _install_carried_store(self, user: str, store: Any, resident: bool) -> None:
        """The core's swap, then every page watching the row's Bag follows the carried one.

        A fresh view with the same prefixes, re-fed with everything the old one
        still held — so no window goes deaf and no captured change is lost in
        the swap. The resident case and the empty parcel change nothing, as in
        the core. The caller holds the lock.
        """
        super()._install_carried_store(user, store, resident)
        if store is None or resident:
            return
        entry = self.user_register.get(user)
        for connection_id in entry["connections"]:
            for page_id in self.connection_register.get(connection_id)["pages"]:
                page = self.page_register.get(page_id)
                view = page["user_view"]
                if view is None:
                    continue
                view.detach()
                fresh = self.registry.new_collector(store, paths=set(page["store_subscriptions"]))
                for change in view.changes:
                    fresh.append(change)
                page["user_view"] = fresh

    def census(self) -> dict[str, Any]:
        """The core census, plus the source filter this process commits against."""
        census = super().census()
        with self.dispatch_lock:
            census["subscribed_tables"] = sorted(self.subscribed_tables)
        return census

    @property
    def connections_folder(self) -> str:
        """The site's per-connection disk root (``data/_connections``)."""
        return self._gnr_site.allConnectionsFolder

    @property
    def pool_member(self) -> bool:
        """Whether this worker serves in a pool — a spawned child on a real wire.

        A worker with no wire attached is a bare test construction: it holds
        every row it ever made, so the per-table commit gate may answer from
        its own subscription cache. A child on the wire holds only its share.
        """
        return self.stream is not None

    def drop_page(self, identity: str, page_id: str, cascade: bool = True) -> None:
        """Drop a page with the legacy ``cascade`` flag, then its disk with the row.

        The flag stays on the bridge and is absorbed here (decision D7,
        2026-08-20). The DEFAULT the site's own close paths pass is the legacy
        page semantics: ``cascade=False`` drops the page ALONE, leaving an
        emptied connection row alive — a closed tab must not take its
        browser's connection with it, its cookie still routes (Must not
        break: site-facing semantics). The core drop has no cascade-less
        form — its climb is unconditional — so this branch composes it from
        the same pieces: the registry drop and the announcement.
        ``cascade=True`` is the core drop itself: the page, and the
        connection and user its departure empties. Either way the disk
        follows the rows: the whole connection folder when the connection
        fell, the page's subfolder otherwise.
        """
        with self.dispatch_lock:
            page = self.page_register.get(page_id)
            if page is None:
                return
            connection_id = page["connection_id"]
            if cascade:
                super().drop_page(identity, page_id)
            else:
                user = self.registry.page_user(page_id)
                self.registry.drop_page(page_id, cascade=False)
                self.add_worker_event("drop_page", user=user, page_id=page_id)
            if self.connection_register.get(connection_id) is None:
                shutil.rmtree(
                    os.path.join(self.connections_folder, connection_id), ignore_errors=True
                )
            else:
                shutil.rmtree(
                    os.path.join(self.connections_folder, connection_id, page_id),
                    ignore_errors=True,
                )

    def drop_connection(self, identity: str, connection_id: str) -> None:
        """The core drop, then the connection's disk folder goes with the row.

        The cascaded pages' subfolders live under the connection folder
        removed here, so nothing is left behind.
        """
        super().drop_connection(identity, connection_id)
        shutil.rmtree(os.path.join(self.connections_folder, connection_id), ignore_errors=True)

    def drop_user(self, user: str) -> None:
        """The core drop, then every connection folder of the user goes too."""
        with self.dispatch_lock:
            connection_ids = [
                connection_id
                for connection_id in self.connection_register.keys()
                if (item := self.connection_register.get(connection_id))
                and item["user"] == user
            ]
            super().drop_user(user)
        for connection_id in connection_ids:
            shutil.rmtree(os.path.join(self.connections_folder, connection_id), ignore_errors=True)

    def exit_process(self) -> None:
        """Stop the site first, then the core teardown (wire, pools)."""
        self._gnr_site.on_site_stop()
        super().exit_process()

    def _create_site(self, source: str, debug: bool) -> Any:
        """The spawned worker's site, built through the group's own factory.

        One construction for both births (fork contract §8, 2026-08-24): the
        forked worker receives a site the template built with this very
        factory. ``build_site`` and not ``build_group_engine`` — closing the
        db connection is what a template owes its children, and a worker
        building for itself has none to close.
        """
        return GenropySiteEngineFactory(source=source, debug=debug).build_site()
