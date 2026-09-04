# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Contract: the worker's side — request slot, exchange, merged collect.

Events born during a request accumulate on a slot of THAT request; at the end
of the request — always, even with nothing to send — one exchange on the lane
delivers them to the commander and retires the page's pendings plus the user's
STATE store writes. The site-facing verb signatures DO NOT MOVE. Migrated from
the core (genro-asgi ``tests/orchestration/test_contract_phase9_request_exchange.py``
at ``f3c7962``) with #59.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from genro_bag import Bag
from genro_bag.datachange import DataChangeCollector
from genro_tytx import to_tytx

from genropy_asgi.spa.genropy_worker import GenropyRequestSlot
from tests.lane import wait_until

USER = "alice"
PAGE = "p1"
SIBLING = "p0"
TABLE = "glbl.user"


@pytest.fixture
def worker(lane):
    """The lane's worker with alice's two pages on it, told to the vertex."""
    lane.worker.new_page(USER, page_id=SIBLING, connection_id="s1")
    lane.worker.new_page(USER, page_id=PAGE, connection_id="s1")
    # The desk judges a target's existence at the vertex: the births go up on the
    # worker's own channel, and the fold reads them before the announcement is answered.
    lane.deliver_worker_events()
    return lane.worker


def foreign_change(path: str, value):
    """A change born elsewhere, TYTX-encoded the way the site hands it over."""
    source = Bag()
    producer = DataChangeCollector(source)
    source[path] = value
    return to_tytx(producer.drain()[-1], "json")


# ----------------------------------------------------------------------
# The slot and the source filter
# ----------------------------------------------------------------------


def test_events_of_a_request_accumulate_on_that_requests_own_slot(lane, worker):
    lane.verb("subscribeTable", USER, table=TABLE, page_id=PAGE)
    lane.wait_filter_synced()
    lane.open_request()
    lane.verb(
        "notifyDbEvents", USER, dbevents={TABLE: ["ins:1"]}, reason="commit", page_id=SIBLING
    )
    slot = worker.request_slot
    assert isinstance(slot, GenropyRequestSlot)
    assert [(d["table"], d["batch"], d["from_page_id"], d["reason"]) for d in slot.dbevents] == [
        (TABLE, ["ins:1"], SIBLING, "commit")
    ]
    assert isinstance(slot.dbevents[0]["ts"], float)
    with ThreadPoolExecutor(max_workers=1) as other_request:
        other = other_request.submit(worker.open_request_slot).result(30)
    assert isinstance(other, GenropyRequestSlot)
    assert other.dbevents == []


def test_events_for_tables_outside_the_cache_die_in_the_worker(lane, worker):
    lane.verb("subscribeTable", USER, table=TABLE, page_id=PAGE)
    wait_until(lambda: worker.subscribed_tables == {TABLE})
    answer = lane.verb("notifyDbEvents", USER, dbevents={"nobody.wants": ["ins:1"]}, page_id=PAGE)
    assert answer == {"tables": []}
    assert worker.request_slot.dbevents == []
    assert lane.desk.page_dbevent_map == {}


def test_local_only_events_reach_only_the_own_collect_and_never_the_wire(lane, worker):
    lane.verb("subscribeTable", USER, table=TABLE, page_id=SIBLING)
    lane.wait_filter_synced()
    lane.verb("notifyDbEvents", USER, dbevents={TABLE: ["ins:1"]}, page_id=PAGE, local_only=True)
    collected = lane.verb("collect_page", PAGE)
    assert [d["batch"] for d in collected["dbevents"]] == [["ins:1"]]
    # Nothing was filed at the desk, so the subscriber never hears of it.
    assert lane.desk.page_dbevent_map == {}
    assert lane.verb("collect_page", SIBLING)["dbevents"] == []


# ----------------------------------------------------------------------
# The exchange at the end of the request
# ----------------------------------------------------------------------


def test_the_exchange_happens_on_every_request_even_empty_handed(lane, worker):
    lane.desk.file_datachange(
        {
            "op": "set_datachange",
            "kind": "page",
            "target": PAGE,
            "filters": None,
            "change": to_tytx(
                {"key": {"path": "a", "reason": "r", "fired": False}, "value": 1,
                 "attributes": None, "delete": False, "change_ts": datetime.now(UTC)},
                "json",
            ),
        }
    )
    assert PAGE in lane.desk.page_datachange_map
    collected = lane.verb("collect_page", PAGE)
    assert collected["dbevents"] == []
    assert [change["value"] for change in collected["datachanges"]] == [1]
    assert PAGE not in lane.desk.page_datachange_map


def test_own_generated_events_come_back_in_the_same_requests_collect(lane, worker):
    lane.verb("subscribeTable", USER, table=TABLE, page_id=PAGE)
    lane.wait_filter_synced()
    lane.verb("notifyDbEvents", USER, dbevents={TABLE: ["ins:1"]}, page_id=PAGE)
    collected = lane.verb("collect_page", PAGE)
    assert [d["batch"] for d in collected["dbevents"]] == [["ins:1"]]


def test_collect_merges_own_collectors_with_the_retired_pendings(lane, worker):
    lane.verb("setStoreSubscription", USER, page_id=PAGE, storename="page", prefix="form")
    lane.verb("setStoreSubscription", USER, page_id=PAGE, storename="user", prefix="prefs")
    lane.verb("subscribeTable", USER, table=TABLE, page_id=PAGE)
    lane.wait_filter_synced()
    worker.page_register.get(PAGE)["store"]["form.name"] = "Ada"
    worker.user_register.get(USER)["store"]["prefs.theme"] = "dark"
    # A change that crosses the wire keeps its change_ts to the millisecond (TYTX
    # rounds there), so the gap is what makes the intended order unambiguous.
    time.sleep(0.005)
    lane.verb("set_datachange", USER, change=foreign_change("untold.x", 1), target=PAGE)
    lane.verb("notifyDbEvents", USER, dbevents={TABLE: ["ins:1"]}, page_id=PAGE)
    collected = lane.verb("collect_page", PAGE)
    paths = [c["key"]["path"] for c in collected["datachanges"]]
    assert paths == ["form.name", "prefs", "prefs.theme", "untold.x"]
    assert [c["change_ts"] for c in collected["datachanges"]] == sorted(
        c["change_ts"] for c in collected["datachanges"]
    )
    assert [d["batch"] for d in collected["dbevents"]] == [["ins:1"]]


# ----------------------------------------------------------------------
# Addressed writes: one road, through the desk
# ----------------------------------------------------------------------


def test_set_datachange_to_a_page_of_the_caller_lands_on_its_row(lane, worker):
    answer = lane.verb(
        "set_datachange",
        USER,
        change=foreign_change("untold.x", 1),
        kind="page",
        target=PAGE,
        filters=None,
        replace=False,
    )
    assert answer == {
        "kind": "page",
        "target": PAGE,
        "filters": None,
        "replace": False,
        "local": True,
        "filed": True,
    }
    assert [c["key"]["path"] for c in worker.page_register.get(PAGE)["datachanges"]] == [
        "untold.x"
    ]
    collected = lane.verb("collect_page", PAGE)
    assert [c["key"]["path"] for c in collected["datachanges"]] == ["untold.x"]
    assert lane.desk.page_datachange_map == {}


def test_a_user_store_write_is_applied_before_the_collect_of_the_retriever(lane, worker):
    lane.verb("setStoreSubscription", USER, page_id=PAGE, storename="user", prefix="prefs")
    lane.verb("setStoreSubscription", USER, page_id=SIBLING, storename="user", prefix="prefs")
    lane.verb("collect_page", PAGE)
    lane.verb("collect_page", SIBLING)
    lane.verb(
        "set_datachange",
        USER,
        change=foreign_change("prefs.theme", "dark"),
        kind="user_store",
        target=USER,
    )
    collected = lane.verb("collect_page", PAGE)
    written = [c for c in collected["datachanges"] if c["key"]["path"] == "prefs.theme"]
    assert [c["value"] for c in written] == ["dark"]
    assert worker.user_register.get(USER)["store"]["prefs.theme"] == "dark"
    assert "_original_ts" in worker.user_register.get(USER)["store"].getAttr("prefs.theme")
    # The sibling captured the very same Bag write on its own user_view.
    sibling = lane.verb("collect_page", SIBLING)
    assert [c["key"]["path"] for c in sibling["datachanges"] if c["value"] == "dark"] == [
        "prefs.theme"
    ]


def test_the_dead_helpers_are_gone(lane, worker):
    assert not hasattr(worker, "deposit_dbevent")
    assert not hasattr(worker, "fan_out_local")
    assert not hasattr(worker, "subscriptions")
    assert not hasattr(worker, "_addressed_row")
    lane.verb("subscribeTable", USER, table=TABLE, page_id=PAGE)
    lane.wait_filter_synced()
    lane.verb("notifyDbEvents", USER, dbevents={TABLE: ["ins:1"]}, page_id=PAGE)
    lane.verb("collect_page", PAGE)
    # The row has no mailbox: a deposit lives on the slot and at the desk, never on the row.
    assert "dbevents" not in worker.page_register.get(PAGE)
