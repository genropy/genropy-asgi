# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Contract: the commander's delivery desk, and the seams it hangs on.

The vertex alone holds the subscription index (table -> page ids) and the
pending queues — per page (two species: datachanges and dbevents, never mixed)
and per user (STATE writes to his store). Everything is fed and drained by
CALLs on the lane. Queues live OUTSIDE the pickled surface: events are ephemeral.

Migrated from the core (genro-asgi ``tests/orchestration/
test_contract_phase8_delivery_desk.py`` at ``f3c7962``) with #59: the desk is
the bridge's now, attached to a ``GenropySpaCommander`` under
``/commander/delivery``. The verbs the site calls — ``subscribeTable`` and the
end-of-request exchange — are the WORKER's half and have their own suites; here
the desk CALLs are placed as the verbs place them, on a real lane over a real
UDS: a core ``SpaWorker`` on one end (no site is needed to reach the desk), a
real ``WorkerHandler`` under the bridge's commander on the other.

The last tests pin the three seams the bridge's commander lives on: the
envelope layer that reads a page's birth, the drop verbs that reach the desk,
and the presentation that fetches a newborn worker the source filter.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from genro_routes import RoutingClass, route
from genro_tytx import from_tytx, to_tytx

from genro_asgi.channel.frame import FrameStream
from genro_asgi.spa.orchestration import FreezeHandler, GroupHandler, SpaWorker
from genro_asgi.spa.orchestration.worker_handler import WorkerHandler

from genropy_asgi.spa.genropy_spa_commander import GenropySpaCommander

WORKER_NAME = "standard_0001"
SUBSCRIBE_PATH = "/commander/delivery/subscribe_table"
EXCHANGE_PATH = "/commander/delivery/exchange"
ON_DATACHANGE_PATH = "/commander/delivery/on_datachange"
USER = "mario"
PAGE = "page_one"
SIBLING = "page_two"


async def wait_for(condition, timeout: float = 10.0) -> None:
    """Poll until the condition holds, or give up loudly at the deadline."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("the machine never reached the awaited state")
        await asyncio.sleep(0.01)


class FilterOrders(RoutingClass):
    """The worker's ``delivery`` orders as a recorder: what the vertex pushed, in order."""

    def __init__(self) -> None:
        self.pushed: list[list[str]] = []

    @route()
    def subscribed_tables(self, tables: list[str]) -> dict[str, Any]:
        self.pushed.append(list(tables))
        return {}


class DeskLane:
    """A core worker and its real handler on one UDS, the bridge's desk above it.

    ``subscribe``, ``on_datachange`` and ``exchange`` put the CALLs on the wire
    the way the worker's verbs place them, and decode the answers. The worker
    records the source filter the vertex pushes it under ``filter_orders``.
    """

    def __init__(self, commander, group, freeze_handler, worker_name=WORKER_NAME) -> None:
        self.commander = commander
        self.group = group
        self.worker_name = worker_name
        self.worker_handler = WorkerHandler(group, worker_name, **group.worker_settings)
        group.worker_handler_map[worker_name] = self.worker_handler
        self.worker = SpaWorker(
            worker_name, freeze_handler=freeze_handler, deposit_lock_retry_interval=0.01
        )
        self.filter_orders = FilterOrders()
        self.worker.worker_dispatcher.commander_orders.add_branches(
            [{"name": "delivery", "instance": self.filter_orders}]
        )
        self._reader_task = None

    @property
    def desk(self):
        return self.commander.delivery_desk

    async def open(self) -> None:
        connector = self.worker_handler.connector
        await connector.start()
        reader, writer = await asyncio.open_unix_connection(str(connector.socket_path))
        self.worker.attach_stream(FrameStream(reader, writer))
        await self.worker.send_presentation({})
        self._reader_task = asyncio.create_task(self.worker.receive_frames())
        await connector.wait_connected()
        self.worker_handler.state = "running"
        self.worker.open_request_slot()

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        self.worker.exit_process()
        await self.worker_handler.connector.stop()
        self.group.worker_handler_map.pop(self.worker_name, None)

    async def subscribe(self, page_id: str, table: str, subscribe: bool = True) -> Any:
        return await self.worker.call(
            SUBSCRIBE_PATH, {"page_id": page_id, "table": table, "subscribe": subscribe}
        )

    async def on_datachange(self, *messages: dict[str, Any]) -> list[Any]:
        return [await self.worker.call(ON_DATACHANGE_PATH, message) for message in messages]

    async def exchange(
        self,
        page_id: str = PAGE,
        user: str = USER,
        dbevents: list[dict[str, Any]] | None = None,
    ) -> Any:
        answer = await self.worker.call(
            EXCHANGE_PATH, {"page_id": page_id, "user": user, "dbevents": dbevents}
        )
        return {
            "datachanges": from_tytx(answer["datachanges"], "json"),
            "dbevents": answer["dbevents"],
            "store_changes": from_tytx(answer["store_changes"], "json"),
        }


@pytest.fixture
def short_root():
    """A temporary root short enough for a socket path; it dies with the test."""
    root = Path(tempfile.mkdtemp(prefix="gnrdesk_"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def make_commander_and_group(short_root):
    commander = GenropySpaCommander(short_root / "frozen_users")
    group = GroupHandler(
        commander,
        "standard",
        memory_concession_bytes=8 * 1024 * 1024 * 1024,
        instance_dir=short_root / "i",
        frozen_users_path=short_root / "frozen_users",
        entry_module="never.launched",
    )
    return commander, group


@pytest.fixture
async def lane(short_root, tmp_path):
    commander, group = make_commander_and_group(short_root)
    # The desk is the authority on a target's existence, and these tests place
    # the desk CALLs themselves: the two pages and their user are folded in by
    # hand, as the envelope chain would have folded them.
    commander.page_connection_map[PAGE] = "cid-a"
    commander.page_connection_map[SIBLING] = "cid-a"
    commander.user_map[USER] = {**commander._new_row(), "group": "standard"}
    built = DeskLane(commander, group, FreezeHandler(tmp_path / "frozen_users"))
    await built.open()
    yield built
    await built.close()


def a_change(path: str, value: Any, reason: str | None = None, age: float = 0.0) -> dict[str, Any]:
    """One change dict in the shape the collectors produce it."""
    return {
        "key": {"path": path, "reason": reason, "fired": False},
        "value": value,
        "attributes": None,
        "delete": False,
        "change_ts": datetime.now(UTC) - timedelta(seconds=age),
        "change_idx": 0,
    }


def addressed(
    target: str, change: dict[str, Any], kind: str = "page", replace: bool = False
) -> dict[str, Any]:
    """The header the worker wraps a change in, with the parcel it never opens."""
    return {
        "kind": kind,
        "target": target,
        "filters": None,
        "change": to_tytx(change, "json"),
        "replace": replace,
    }


def a_deposit(table: str, reason: str | None = None, age: float = 0.0) -> dict[str, Any]:
    """One table-event deposit in the shape ``dbevent_deposit`` gives it."""
    return {
        "table": table,
        "batch": [{"pkey": "1"}],
        "from_page_id": PAGE,
        "reason": reason,
        "ts": time.time() - age,
    }


# ----------------------------------------------------------------------
# The index: fed by the immediate subscription call
# ----------------------------------------------------------------------


async def test_a_subscription_call_updates_the_index_before_it_answers(lane):
    answer = await asyncio.wait_for(lane.subscribe(PAGE, "invoices"), 5.0)
    assert lane.desk.page_subscriptions.pages_for("invoices") == {PAGE}
    assert answer["page_id"] == PAGE and answer["subscribe"] is True


async def test_an_unsubscribe_call_removes_the_entry_and_stops_future_delivery(lane):
    await asyncio.wait_for(lane.subscribe(PAGE, "invoices"), 5.0)
    answer = await asyncio.wait_for(lane.subscribe(PAGE, "invoices", subscribe=False), 5.0)
    assert answer["subscribe"] is False
    assert lane.desk.page_subscriptions.pages_for("invoices") == set()
    exchanged = await asyncio.wait_for(lane.exchange(dbevents=[a_deposit("invoices")]), 5.0)
    assert exchanged["dbevents"] == []
    assert lane.desk.page_dbevent_map == {}


# ----------------------------------------------------------------------
# The exchange: events in, pendings out, one round
# ----------------------------------------------------------------------


async def test_the_exchange_returns_the_callers_own_events_in_the_same_round(lane):
    await asyncio.wait_for(lane.subscribe(PAGE, "invoices"), 5.0)
    await asyncio.wait_for(lane.on_datachange(addressed(PAGE, a_change("form.name", "Mario"))), 5.0)
    exchanged = await asyncio.wait_for(
        lane.exchange(dbevents=[a_deposit("invoices", reason="commit")]), 5.0
    )
    assert [deposit["table"] for deposit in exchanged["dbevents"]] == ["invoices"]
    assert exchanged["dbevents"][0]["reason"] == "commit"
    assert [change["value"] for change in exchanged["datachanges"]] == ["Mario"]
    # Retired: the queues are empty, nothing comes back twice.
    assert await asyncio.wait_for(lane.exchange(), 5.0) == {
        "datachanges": [],
        "dbevents": [],
        "store_changes": [],
    }


async def test_events_for_another_pages_queue_wait_for_that_pages_own_exchange(lane):
    await asyncio.wait_for(lane.subscribe(SIBLING, "invoices"), 5.0)
    await asyncio.wait_for(
        lane.on_datachange(addressed(SIBLING, a_change("form.name", "Mario"))), 5.0
    )
    mine = await asyncio.wait_for(lane.exchange(dbevents=[a_deposit("invoices")]), 5.0)
    assert mine["datachanges"] == [] and mine["dbevents"] == []
    assert lane.desk.page_dbevent_map[SIBLING] and lane.desk.page_datachange_map[SIBLING]
    theirs = await asyncio.wait_for(lane.exchange(page_id=SIBLING), 5.0)
    assert [change["value"] for change in theirs["datachanges"]] == ["Mario"]
    assert [deposit["table"] for deposit in theirs["dbevents"]] == ["invoices"]
    assert SIBLING not in lane.desk.page_dbevent_map
    assert SIBLING not in lane.desk.page_datachange_map


async def test_an_event_for_a_table_nobody_subscribes_dies_at_the_desk(lane):
    await asyncio.wait_for(lane.subscribe(PAGE, "invoices"), 5.0)
    exchanged = await asyncio.wait_for(lane.exchange(dbevents=[a_deposit("orders")]), 5.0)
    assert exchanged["dbevents"] == []
    assert lane.desk.page_dbevent_map == {}


async def test_replace_coalesces_inside_the_target_queue(lane):
    await asyncio.wait_for(
        lane.on_datachange(
            addressed(SIBLING, a_change("form.name", "first")),
            addressed(SIBLING, a_change("form.name", "second"), replace=True),
            addressed(SIBLING, a_change("form.other", "kept"), replace=True),
        ),
        5.0,
    )
    theirs = await asyncio.wait_for(lane.exchange(page_id=SIBLING), 5.0)
    assert [(c["key"]["path"], c["value"]) for c in theirs["datachanges"]] == [
        ("form.name", "second"),
        ("form.other", "kept"),
    ]


# ----------------------------------------------------------------------
# Hygiene: nothing waiting expires, and the fold
# ----------------------------------------------------------------------


async def test_nothing_waiting_at_the_desk_expires(lane):
    stale = 3600.0
    await asyncio.wait_for(lane.subscribe(PAGE, "invoices"), 5.0)
    before = time.time()
    await asyncio.wait_for(
        lane.on_datachange(
            addressed(PAGE, a_change("form.old", "old", age=stale)),
            addressed(PAGE, a_change("form.new", "new")),
            addressed(USER, a_change("prefs.old", "old", age=stale), kind="user_store"),
            addressed(USER, a_change("prefs.new", "new"), kind="user_store"),
        ),
        5.0,
    )
    exchanged = await asyncio.wait_for(
        lane.exchange(
            dbevents=[a_deposit("invoices", reason="old", age=stale), a_deposit("invoices")]
        ),
        5.0,
    )
    assert [c["value"] for c in exchanged["datachanges"]] == ["old", "new"]
    assert [c["value"] for c in exchanged["store_changes"]] == ["old", "new"]
    assert [deposit["reason"] for deposit in exchanged["dbevents"]] == ["old", None]
    arrivals = [c["arrival_ts"] for c in exchanged["datachanges"]]
    assert arrivals == sorted(arrivals) and arrivals[0] >= before
    assert all(deposit["arrival_ts"] >= before for deposit in exchanged["dbevents"])


async def test_a_dropped_page_takes_its_queue_with_it(lane):
    await asyncio.wait_for(lane.subscribe(PAGE, "invoices"), 5.0)
    await asyncio.wait_for(lane.on_datachange(addressed(PAGE, a_change("form.name", "Mario"))), 5.0)
    await asyncio.wait_for(lane.exchange(page_id=SIBLING, dbevents=[a_deposit("invoices")]), 5.0)
    assert lane.desk.page_datachange_map[PAGE] and lane.desk.page_dbevent_map[PAGE]
    lane.commander.drop_page(PAGE)
    assert PAGE not in lane.desk.page_datachange_map
    assert PAGE not in lane.desk.page_dbevent_map
    assert lane.desk.page_subscriptions.tables_for(PAGE) == set()
    assert lane.desk.subscribed_tables == []


# ----------------------------------------------------------------------
# The seams: the envelope layer, the drop verbs, the presentation
# ----------------------------------------------------------------------


async def test_the_new_page_announcement_files_the_rows_tables_at_the_desk(lane):
    # The bridge's envelope layer reads what the core's fold ignores: the
    # table_subscriptions a page's birth or wake carries.
    await lane.worker.announce_worker_events(
        [
            {
                "op": "new_page",
                "worker": lane.worker_name,
                "page_id": "p9",
                "connection_id": "c9",
                "table_subscriptions": ["orders"],
            }
        ]
    )
    assert lane.commander.page_connection_map["p9"] == "c9"
    assert lane.desk.page_subscriptions.page_tables["p9"] == {"orders"}
    assert lane.desk.subscribed_tables == ["orders"]


def test_the_drop_cascade_reaches_the_desk_and_is_pinned(short_root):
    # drop_connection and drop_user clear the departed pages' queues and index
    # entries at the desk; the assertion must fail if either cleanup is removed.
    commander, _group = make_commander_and_group(short_root)
    desk = commander.delivery_desk
    commander.connection_user_map["c-1"] = "mario"
    commander.user_map["mario"] = commander._new_row()
    commander.page_connection_map["p-1"] = "c-1"
    desk.subscribe_table("p-1", "mytable")
    desk.file_dbevent(
        {"table": "mytable", "batch": [], "from_page_id": "px", "reason": None, "ts": 0.0}
    )
    assert desk.page_dbevent_map["p-1"]
    commander.drop_connection("c-1")
    assert "p-1" not in desk.page_subscriptions.page_tables
    assert "p-1" not in desk.page_dbevent_map
    assert "p-1" not in commander.page_connection_map
    desk.user_store_change_map["mario"] = [{"key": {}, "value": 1}]
    commander.drop_user("mario")
    assert "mario" not in desk.user_store_change_map


async def test_a_newborn_worker_gets_the_set_at_its_presentation_and_at_every_transition(
    short_root, tmp_path
):
    commander, group = make_commander_and_group(short_root)
    lane = DeskLane(commander, group, FreezeHandler(tmp_path / "frozen_users"))
    await lane.open()
    try:
        # The presentation fetched the newborn the whole set: empty, so far.
        await wait_for(lambda: lane.filter_orders.pushed == [[]])
        # A transition of the global set is pushed; a second subscriber is not.
        await asyncio.wait_for(lane.subscribe(PAGE, "invoices"), 5.0)
        await wait_for(lambda: lane.filter_orders.pushed == [[], ["invoices"]])
        await asyncio.wait_for(lane.subscribe(SIBLING, "invoices"), 5.0)
        await asyncio.wait_for(lane.subscribe(PAGE, "invoices", subscribe=False), 5.0)
        await asyncio.sleep(0.05)
        assert lane.filter_orders.pushed == [[], ["invoices"]]
        # The last subscriber leaving is a transition again.
        await asyncio.wait_for(lane.subscribe(SIBLING, "invoices", subscribe=False), 5.0)
        await wait_for(lambda: lane.filter_orders.pushed == [[], ["invoices"], []])
    finally:
        await lane.close()


async def test_the_census_carries_the_desks_numbers(lane):
    await asyncio.wait_for(lane.subscribe(PAGE, "invoices"), 5.0)
    await asyncio.wait_for(lane.exchange(page_id=SIBLING, dbevents=[a_deposit("invoices")]), 5.0)
    census = await lane.commander.get_pool_census()
    assert census["delivery_desk"]["subscribed_tables"] == ["invoices"]
    assert census["delivery_desk"]["page_dbevent_map"] == {PAGE: 1}
