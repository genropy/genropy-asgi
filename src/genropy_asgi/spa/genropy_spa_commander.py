# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropySpaCommander: the core's vertex with the desk of a hosted GenroPy site.

The core commander knows populations, placements, the global store and the
freezer; what a GenroPy site adds at the vertex — who subscribes which table,
what waits for which page — left the core with genro-asgi #59 and lives here,
attached through the seams the core names for a consumer:

- ``commander_dispatcher.add_branches``: the :class:`~genropy_asgi.spa.
  delivery_desk.DeliveryDesk` is the ``delivery`` branch, so a worker's CALLs
  on ``/commander/delivery/{subscribe_table,exchange,deposit,on_datachange}``
  resolve to its routes, the paths the core had;
- ``envelope_handler`` (a property): :class:`GenropyCommanderEnvelopeHandler`
  is the last layer of the envelope chain — its ``on_new_page`` reads the
  ``table_subscriptions`` a page's birth (or wake) announces and hands them to
  ``record_page_table_subscriptions``, so the desk's index is a projection of
  the page rows, rebuilt from every announcement;
- ``on_worker_presented``: a newborn worker is sent the source filter (the
  desk's whole table set) on a task of its own, never holding up the envelope;
- ``drop_page`` / ``drop_connection`` / ``drop_user``: the core demolition,
  then the desk forgets the departed pages' queues and subscriptions and the
  departed user's store queue.

``broadcast_subscribed_tables`` is what the desk calls on every transition of
the global table set (a table gaining its first subscriber, or losing its
last): one push per living worker, awaited by nobody. The push is the order
``/commander/delivery/subscribed_tables`` on the worker's dispatcher — the
``delivery`` branch the worker attaches to its ``commander_orders`` — with
``{"tables": [...]}``; a worker whose wire is gone or that does not answer in
``SUBSCRIBED_TABLES_PUSH_TIMEOUT_SECONDS`` is logged at debug level and left
alone: the next transition carries the whole set again.

``get_pool_census`` adds the desk's numbers under ``delivery_desk``.

This module imports no ``gnr.*``: it runs in the front process.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from genro_asgi.spa.orchestration import SpaCommander
from genro_asgi.spa.orchestration.envelope_handler import CommanderEnvelopeHandler

from .delivery_desk import DeliveryDesk

#: The order that carries the source filter down to a worker: the ``delivery``
#: branch of its ``commander_orders``.
SUBSCRIBED_TABLES_OP_PATH = "/commander/delivery/subscribed_tables"
#: How long one push waits for the worker's reply before it is given up on.
SUBSCRIBED_TABLES_PUSH_TIMEOUT_SECONDS = 5.0

__all__ = [
    "SUBSCRIBED_TABLES_OP_PATH",
    "SUBSCRIBED_TABLES_PUSH_TIMEOUT_SECONDS",
    "GenropyCommanderEnvelopeHandler",
    "GenropySpaCommander",
]


class GenropyCommanderEnvelopeHandler(CommanderEnvelopeHandler):
    """The vertex layer of the envelope chain, reading what a page's birth carries for the desk."""

    def on_new_page(self, worker_event: dict[str, Any]) -> None:
        """The core folds the page in; the desk files the tables its row announced."""
        super().on_new_page(worker_event)
        self.spa_commander.record_page_table_subscriptions(
            worker_event["page_id"], worker_event["table_subscriptions"]
        )


class GenropySpaCommander(SpaCommander):
    """The core commander with the desk of a hosted GenroPy site (see the module docstring)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        #: Who subscribes what, and what waits for whom: the ``delivery`` branch.
        #: Born before the core's constructor runs: whatever that constructor
        #: folds through the drop verbs finds the desk in place.
        self.delivery_desk = DeliveryDesk(self)
        super().__init__(*args, **kwargs)
        self.commander_dispatcher.add_branches(
            [{"name": "delivery", "instance": self.delivery_desk}]
        )
        self._subscribed_tables_push_tasks: set[asyncio.Task[None]] = set()
        self._logger = logging.getLogger(__name__)

    @property
    def envelope_handler(self) -> CommanderEnvelopeHandler:
        """The bridge's last layer: the core's fold, then the desk reads the announcement."""
        return GenropyCommanderEnvelopeHandler(self)

    def record_page_table_subscriptions(self, page_id: str, tables: list[str]) -> None:
        """File the tables a page's birth or wake announced: the desk's index follows the rows.

        Args:
            page_id: the page the announcement is about.
            tables: the ``table_subscriptions`` its register row carries —
                empty at birth, the replayed set at the wake.

        Acts on the desk's ``page_subscriptions``; a table gaining its first
        subscriber here is announced to every worker, once.
        """
        self.delivery_desk.record_page_table_subscriptions(page_id, tables)

    def on_worker_presented(self, worker_handler: Any) -> None:
        """A newborn worker saw no transition: its presentation fetches it the whole set."""
        self.push_subscribed_tables(worker_handler)

    def broadcast_subscribed_tables(self) -> None:
        """Push the desk's current subscribed-table set to every living worker.

        Called on every transition of the global set — a table gaining its first
        subscriber, or losing its last. It creates one task per worker and awaits
        none of them: the announcement must not hold up the op that caused it.
        """
        for group_handler in self.group_map.values():
            for worker_handler in group_handler.living_workers:
                self.push_subscribed_tables(worker_handler)

    def push_subscribed_tables(self, worker_handler: Any) -> None:
        """Send one process the current source filter, without waiting for it."""
        task = asyncio.create_task(self._push_subscribed_tables(worker_handler))
        self._subscribed_tables_push_tasks.add(task)
        task.add_done_callback(self._subscribed_tables_push_tasks.discard)

    async def _push_subscribed_tables(self, worker_handler: Any) -> None:
        """Read the desk's set at send time and put it on the process's wire.

        A process whose wire is gone is logged at debug level and left alone: a
        dead worker has no commits left to filter. One that does not answer
        within ``SUBSCRIBED_TABLES_PUSH_TIMEOUT_SECONDS`` is logged the same way:
        the next transition carries the whole set again.
        """
        tables = self.delivery_desk.subscribed_tables
        try:
            await worker_handler.connector.call(
                SUBSCRIBED_TABLES_OP_PATH,
                {"tables": tables},
                timeout=SUBSCRIBED_TABLES_PUSH_TIMEOUT_SECONDS,
            )
        except ConnectionError:
            self._logger.debug(
                "Worker %s: the subscribed-tables push found no wire", worker_handler.name
            )
        except TimeoutError:
            self._logger.debug(
                "Worker %s: the subscribed-tables push got no answer", worker_handler.name
            )

    def drop_page(self, page_id: str) -> None:
        """The core forgets the page; the desk forgets its queues and its subscriptions."""
        super().drop_page(page_id)
        self.delivery_desk.drop_page(page_id)

    def drop_connection(self, cid: str) -> None:
        """The core forgets the connection's pages; the desk forgets each of them.

        The pages are read before the core empties them out of
        ``page_connection_map``: afterwards nothing says whose they were.
        """
        page_ids = [page for page, owner in self.page_connection_map.items() if owner == cid]
        super().drop_connection(cid)
        for page_id in page_ids:
            self.delivery_desk.drop_page(page_id)

    def drop_user(self, user: str) -> bool:
        """The core forgets the identity whole; the desk forgets his store queue.

        His pages went with their connections: the core's demolition passes
        through ``drop_connection`` above.
        """
        had_state = super().drop_user(user)
        self.delivery_desk.drop_user(user)
        return had_state

    async def get_pool_census(self) -> dict[str, Any]:
        """The core census, plus the desk's numbers under ``delivery_desk``."""
        census = await super().get_pool_census()
        census["delivery_desk"] = self.delivery_desk.census
        return census
