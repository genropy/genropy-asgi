# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""DeliveryDesk: the vertex's desk — who subscribes what, and what waits for whom.

The ``delivery`` branch of the commander's dispatcher: its four ``@route``
methods are the CALLs a worker places on ``/commander/delivery/<op>``, resolved
by name by genro-routes. This is the genropy half of the vertex: it left the
core with genro-asgi #59 and :class:`~genropy_asgi.spa.genropy_spa_commander.
GenropySpaCommander` attaches it in the core's place, under the same paths.

The commander alone holds the subscription index and the pending queues, and
every one of them is fed and drained by CALLs a worker places on the lane.
Three species, never mixed: a page's **datachanges** (explicit writes
addressed at it), a page's **dbevents** (the deposits of a commit on a table
it subscribed), and a user's **store changes** (STATE writes addressed at his
own store, applied to his Bag by whichever of his pages retires them —
usersticky puts them all in one process, and the siblings capture the write
locally on their own ``user_view``).

**Outside the pickled surface, on purpose.** The queues are ephemeral: what
is waiting for a user who goes into the freezer is lost with the websockets
that would have carried it, and nothing here is dumped or restored.

**The subscription is written before the call answers**, so the index here is
right the moment the subscriber is told so. The workers' own source filter is
a copy, refreshed by a CALL the commander pushes on every transition of the
set, so a worker filters with a set at most one CALL's flight out of date:
the accepted risk, measured against the tens of seconds that separate a
page's subscription from its first commit.

**The exchange files first and answers after**, so the caller's own events
come back in the same round; a sibling page's events wait in its own queue
for its own next exchange, since nothing is ever pushed from here.

**Nothing waiting here expires.** A queue lives as long as its page: what a
page has not collected yet is delivered at its next exchange whatever its
age, as the daemon's single list did, and ``drop_page`` is what empties it.
Every queued item is stamped ``arrival_ts``, the wall-clock instant it was
filed here — the same clock the workers stamp their own rows with — so a
collect can merge this queue with the row's in the order one list would
have had.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from genro_routes import RoutingClass, route
from genro_tytx import from_tytx, to_tytx

from .subscription_index import SubscriptionIndex

#: What ``kind`` names when an addressed write is a STATE one: the target is a
#: store, and the write is a real Bag write wherever it lands. Declared here,
#: at the vertex, and imported by the worker: the two halves of the bridge
#: share one module for the one word they must agree on.
STATE_KINDS = frozenset({"page_store", "user_store", "connection_store"})

__all__ = ["STATE_KINDS", "DeliveryDesk"]


class DeliveryDesk(RoutingClass):
    """The vertex's desk (see the module docstring).

    Args:
        spa_commander: the vertex this desk belongs to — a
            :class:`~genropy_asgi.spa.genropy_spa_commander.GenropySpaCommander`,
            whose ``broadcast_subscribed_tables`` announces every transition of
            the global table set and whose two population maps say whether an
            addressed target exists.
    """

    def __init__(self, spa_commander: Any) -> None:
        self.spa_commander = spa_commander
        #: Which pages want which table, both directions, one mutator each.
        self.page_subscriptions = SubscriptionIndex()
        #: The pending changes of each page, in arrival order.
        self.page_datachange_map: dict[str, list[dict[str, Any]]] = {}
        #: The pending table-event deposits of each page, in arrival order.
        self.page_dbevent_map: dict[str, list[dict[str, Any]]] = {}
        #: The pending STATE writes addressed at each user's own store.
        self.user_store_change_map: dict[str, list[dict[str, Any]]] = {}
        self._logger = logging.getLogger(__name__)

    @property
    def subscribed_tables(self) -> list[str]:
        """Every table holding at least one subscription — the workers' source filter."""
        return sorted(self.page_subscriptions.table_pages)

    @property
    def census(self) -> dict[str, Any]:
        """The desk in numbers: the table set and the length of every queue. JSON-safe."""
        return {
            "subscribed_tables": self.subscribed_tables,
            "page_dbevent_map": {
                page_id: len(queue) for page_id, queue in self.page_dbevent_map.items()
            },
            "page_datachange_map": {
                page_id: len(queue) for page_id, queue in self.page_datachange_map.items()
            },
            "user_store_change_map": {
                user: len(queue) for user, queue in self.user_store_change_map.items()
            },
        }

    def record_page_table_subscriptions(self, page_id: str, tables: list[str]) -> None:
        """File a page's announced subscriptions: the index rebuilt from the row.

        Args:
            page_id: the page the announcement is about.
            tables: the ``table_subscriptions`` its register row carries —
                empty at birth, the replayed set at the wake.

        Acts on ``page_subscriptions``, and announces the new set to the workers
        once when at least one table gained its first subscriber.
        """
        created = [self.page_subscriptions.subscribe(page_id, table) for table in tables]
        if any(created):
            self._announce_subscribed_tables()

    @route()
    def subscribe_table(
        self, page_id: str, table: str, subscribe: bool = True
    ) -> dict[str, Any]:
        """File (or unfile) a page's subscription to a table's events.

        Args:
            page_id: the subscribing page — whoever asks, so there is no target.
            table: the table whose events it wants.
            subscribe: opening the subscription, or closing it.

        Returns:
            The subscription as it was taken. No table list: the workers' source
            filter travels on its own CALL, not on this reply.

        Acts on ``page_subscriptions`` BEFORE it answers, and announces the new
        set to the workers when this was a transition of it.
        """
        if subscribe:
            moved = self.page_subscriptions.subscribe(page_id, table)
        else:
            moved = self.page_subscriptions.unsubscribe(page_id, table)
        if moved:
            self._announce_subscribed_tables()
        return {"page_id": page_id, "table": table, "subscribe": subscribe}

    @route()
    def exchange(
        self,
        page_id: str,
        user: str,
        dbevents: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Take a request's deposits in, and hand back what waits for its page.

        Args:
            page_id: the exchanging page — whose queues are retired.
            user: its owner, whose store queue is retired with them.
            dbevents: the deposits the request produced, as the worker shaped
                them.

        Returns:
            The two page species and the user's store changes. The changes travel
            TYTX-encoded: their ``change_ts`` is a datetime, which JSON has no
            word for.

        Acts on all three queue maps: the deposits are filed first, so what the
        caller itself produced for itself is in the answer. The addressed writes
        arrive by their own op, ``on_datachange``, the moment their verb is
        called — they do not ride this exchange.
        """
        for deposit in dbevents or ():
            self.file_dbevent(deposit)
        return {
            "datachanges": to_tytx(self.drain_page_datachanges(page_id), "json"),
            "dbevents": self.drain_page_dbevents(page_id),
            "store_changes": to_tytx(self.drain_user_store_changes(user), "json"),
        }

    @route()
    def deposit(self, dbevents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """File the deposits of a request that reached no ``collect_page``.

        Args:
            dbevents: the deposits the request produced, as the worker shaped
                them.

        Returns:
            Nothing: there is no page to answer.

        Acts on ``page_dbevent_map`` through ``file_dbevent`` and retires
        nothing — a ``rootPage`` webhook, or a request that failed after its
        commit, has no page of its own whose queues would be drained.
        """
        for deposit in dbevents or ():
            self.file_dbevent(deposit)
        return {}

    @route()
    def on_datachange(self, **message: Any) -> dict[str, Any]:
        """File one addressed write the moment its own verb is called.

        Args:
            message: the header the verb shaped — ``op``, ``kind``, ``target``,
                ``filters``, ``replace``, plus the TYTX-encoded ``change`` of a
                ``set_datachange`` or the ``path`` of a ``drop_datachanges``.

        Returns:
            ``{"filed": True}`` when the target exists at the vertex, and
            ``{"filed": False}`` when nobody holds it — the verb raises on that
            answer, so no queue is born for a page that never existed.

        Acts on the queue maps through ``file_datachange``. The desk is the
        authority on a target's existence: a worker knows its own rows only, and
        an address it does not hold is not thereby unknown.
        """
        target = message["target"]
        if message["kind"] in STATE_KINDS:
            known = target in self.spa_commander.user_map
        else:
            known = target in self.spa_commander.page_connection_map
        if not known:
            return {"filed": False}
        self.file_datachange(message)
        return {"filed": True}

    def file_datachange(self, message: dict[str, Any]) -> None:
        """Put one addressed write in the queue of whoever it is addressed at.

        Args:
            message: the header — ``op``, ``kind``, ``target``, ``filters`` —
                plus what the op adds: the TYTX-encoded ``change`` of a
                ``set_datachange``, the ``path`` of a ``drop_datachanges``.

        Raises:
            NotImplementedError: a filtered address — the page surface here
                answers no field yet, exactly as on the worker before it.

        Acts on ``page_datachange_map`` for a SIGNAL address and on
        ``user_store_change_map`` for a STATE one. ``replace`` coalesces with the
        pending change of the same key — path, reason, fired — so a value
        written over and over reaches the browser once. ``reset_datachanges``
        empties that queue and ``drop_datachanges`` takes one prefix out of it,
        the semantics the page collector had before the queue moved here. A
        filed change is stamped ``arrival_ts`` — the instant of THIS append,
        after a ``replace`` has removed its predecessor, as the daemon's pop and
        append put the newcomer last.
        """
        if message.get("filters") is not None:
            raise NotImplementedError(f"desk: the filtered address {message['filters']!r}")
        if message["kind"] in STATE_KINDS:
            queue = self.user_store_change_map.setdefault(message["target"], [])
        else:
            queue = self.page_datachange_map.setdefault(message["target"], [])
        if message.get("op") == "reset_datachanges":
            queue.clear()
            return
        if message.get("op") == "drop_datachanges":
            queue[:] = [
                pending
                for pending in queue
                if not self._under(pending["key"]["path"], message["path"])
            ]
            return
        change = from_tytx(message["change"], "json")
        if message.get("replace"):
            queue[:] = [pending for pending in queue if pending["key"] != change["key"]]
        change["arrival_ts"] = time.time()
        queue.append(change)

    def file_dbevent(self, deposit: dict[str, Any]) -> None:
        """Put one deposit in the queue of every page subscribing its table.

        Args:
            deposit: the deposit as the worker shaped it — ``table``, ``batch``,
                ``from_page_id``, ``reason``, ``ts``; ``arrival_ts`` is stamped
                here, once, the same instant for every subscriber's queue.

        Acts on ``page_dbevent_map``. A table nobody subscribes anywhere costs
        one dict lookup that misses and the deposit dies here: a dbevent is a
        signal, and there is no queue for a listener that does not exist.
        """
        deposit["arrival_ts"] = time.time()
        for page_id in self.page_subscriptions.pages_for(deposit["table"]):
            self.page_dbevent_map.setdefault(page_id, []).append(deposit)

    def drain_page_datachanges(self, page_id: str) -> list[dict[str, Any]]:
        """Retire a page's pending changes, all of them, in arrival order."""
        return self.page_datachange_map.pop(page_id, [])

    def drain_page_dbevents(self, page_id: str) -> list[dict[str, Any]]:
        """Retire a page's pending deposits, all of them, in arrival order."""
        return self.page_dbevent_map.pop(page_id, [])

    def drain_user_store_changes(self, user: str) -> list[dict[str, Any]]:
        """Retire a user's pending store writes, all of them, in arrival order."""
        return self.user_store_change_map.pop(user, [])

    def drop_page(self, page_id: str) -> None:
        """Forget a page here: its two queues and its subscriptions, one breath.

        Args:
            page_id: the page that is gone.

        Acts on both page queue maps and on ``page_subscriptions``: nothing is
        delivered to a page that is not there any more, and nothing keeps its
        tables alive in the source filter.
        """
        self.page_datachange_map.pop(page_id, None)
        self.page_dbevent_map.pop(page_id, None)
        if self.page_subscriptions.drop_page(page_id):
            self._announce_subscribed_tables()

    def drop_user(self, user: str) -> None:
        """Forget what was waiting for a user's own store; his pages go on their own."""
        self.user_store_change_map.pop(user, None)

    def _announce_subscribed_tables(self) -> None:
        """Tell the vertex the global set moved: every living worker gets it pushed."""
        self.spa_commander.broadcast_subscribed_tables()

    def _under(self, path: str, prefix: str) -> bool:
        """Whether a pending change's path falls under a dropped prefix."""
        return path == prefix or path.startswith(f"{prefix}.")
