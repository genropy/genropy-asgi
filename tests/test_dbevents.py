# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Contract: table events — own ops, own species, one index at the desk.

The ops keep the daemon's signatures (``subscribeTable``, ``notifyDbEvents``) and
those DO NOT move. The index lives at the commander's desk, ``subscribeTable``
files the interest there with a synchronous lane call, the deposits accumulate on
the request slot and travel to the desk at the end of the request, and a page
reads them back through its own exchange. Migrated from the core (genro-asgi
``tests/orchestration/test_contract_phase4_dbevents.py`` at ``f3c7962``) with #59.
"""

from __future__ import annotations

import pytest

from tests.lane import wait_until


@pytest.fixture
def worker(lane):
    """The lane's worker with alice's two pages on it, told to the vertex."""
    lane.worker.new_page("alice", page_id="p0", connection_id="s1")
    lane.worker.new_page("alice", page_id="p1", connection_id="s1")
    lane.deliver_worker_events()
    return lane.worker


def deposits_of(lane, page_id):
    """What one page reads when it exchanges: its deposits, retired from the desk."""
    return lane.verb("collect_page", page_id)["dbevents"]


# ----------------------------------------------------------------------
# The subscription: the row here, the index at the desk
# ----------------------------------------------------------------------


def test_a_subscription_lands_on_the_index_and_on_the_row(lane, worker):
    result = lane.verb("subscribeTable", "alice", table="glbl.user", page_id="p1")
    lane.wait_filter_synced()
    assert result == {"page_id": "p1", "table": "glbl.user", "subscribe": True}
    assert lane.desk.page_subscriptions.pages_for("glbl.user") == {"p1"}
    assert worker.page_register.get("p1")["table_subscriptions"] == {"glbl.user"}
    # The reply carries no table list: the source filter arrives on the CALL the
    # commander pushes, within the flight of that call.
    wait_until(lambda: worker.subscribed_tables == {"glbl.user"})


def test_a_subscription_for_an_unknown_page_is_an_error(worker):
    with pytest.raises(KeyError, match="ghost"):
        worker.subscribeTable("alice", table="glbl.user", page_id="ghost")


def test_subscribe_mode_is_accepted_and_ignored(lane, worker):
    """The vestigial parameter still travels from the callers: it must not refuse."""
    result = lane.verb(
        "subscribeTable", "alice", table="glbl.user", page_id="p1", subscribeMode="fired"
    )
    assert result == {"page_id": "p1", "table": "glbl.user", "subscribe": True}
    assert lane.desk.page_subscriptions.pages_for("glbl.user") == {"p1"}


def test_an_unsubscribe_clears_the_row_and_the_index(lane, worker):
    lane.verb("subscribeTable", "alice", table="glbl.user", page_id="p1")
    lane.wait_filter_synced()
    lane.verb("subscribeTable", "alice", table="glbl.user", page_id="p1", subscribe=False)
    lane.wait_filter_synced()
    assert worker.page_register.get("p1")["table_subscriptions"] == set()
    assert lane.desk.page_subscriptions.pages_for("glbl.user") == set()


def test_a_dropped_page_leaves_no_subscription_behind(lane, worker):
    """The worker forgets the row; the desk forgets the page when the fold reaches it."""
    lane.verb("subscribeTable", "alice", table="glbl.user", page_id="p1")
    lane.wait_filter_synced()
    lane.verb("drop_page", "alice", "p1")
    lane.wait_filter_synced()
    assert lane.desk.page_subscriptions.pages_for("glbl.user") == set()
    assert worker.page_register.get("p1") is None


# ----------------------------------------------------------------------
# notifyDbEvents: shaped once, filtered at the source, delivered by the desk
# ----------------------------------------------------------------------


def test_a_commit_reaches_the_local_subscribers(lane, worker):
    lane.verb("subscribeTable", "alice", table="glbl.user", page_id="p1")
    lane.wait_filter_synced()
    result = lane.verb(
        "notifyDbEvents", "alice", dbevents={"glbl.user": ["ins:1"]}, reason="commit",
        page_id="p0",
    )
    assert result == {"tables": ["glbl.user"]}
    deposits = deposits_of(lane, "p1")
    assert [(d["table"], d["batch"], d["from_page_id"], d["reason"]) for d in deposits] == [
        ("glbl.user", ["ins:1"], "p0", "commit")
    ]
    assert "ts" in deposits[0]


def test_two_subscribing_pages_read_the_same_shaped_deposit(lane, worker):
    worker.new_page("alice", page_id="p2", connection_id="s1")
    lane.deliver_worker_events()
    lane.verb("subscribeTable", "alice", table="glbl.user", page_id="p1")
    lane.wait_filter_synced()
    lane.verb("subscribeTable", "alice", table="glbl.user", page_id="p2")
    lane.wait_filter_synced()
    lane.verb("notifyDbEvents", "alice", dbevents={"glbl.user": ["ins:1"]}, page_id="p0")
    first = deposits_of(lane, "p1")
    second = deposits_of(lane, "p2")
    assert first == second
    assert first[0]["ts"] == second[0]["ts"]


def test_a_commit_nobody_subscribed_deposits_nothing(lane, worker):
    """Filtered at the source: a table outside the cache never even reaches the wire."""
    result = lane.verb("notifyDbEvents", "alice", dbevents={"glbl.user": ["ins:1"]}, page_id="p1")
    assert result == {"tables": []}
    assert deposits_of(lane, "p0") == []
    assert deposits_of(lane, "p1") == []


def test_an_empty_batch_is_not_announced_at_all(lane, worker):
    lane.verb("subscribeTable", "alice", table="glbl.user", page_id="p1")
    lane.wait_filter_synced()
    result = lane.verb("notifyDbEvents", "alice", dbevents={"glbl.user": []}, page_id="p1")
    assert result == {"tables": []}
    assert deposits_of(lane, "p1") == []


def test_local_only_deposits_on_the_origin_page_alone(lane, worker):
    """The hidden transaction: its events belong to the page that made them."""
    lane.verb("subscribeTable", "alice", table="glbl.user", page_id="p1")
    lane.wait_filter_synced()
    result = lane.verb(
        "notifyDbEvents", "alice", dbevents={"glbl.user": ["ins:1"]}, page_id="p0",
        local_only=True,
    )
    assert result == {"tables": ["glbl.user"]}
    assert [d["batch"] for d in deposits_of(lane, "p0")] == [["ins:1"]]
    assert deposits_of(lane, "p1") == []


# ----------------------------------------------------------------------
# The species never mix
# ----------------------------------------------------------------------


def test_the_deposit_drains_on_its_own_key_and_never_as_a_datachange(lane, worker):
    lane.verb("subscribeTable", "alice", table="glbl.user", page_id="p1")
    lane.wait_filter_synced()
    lane.verb("setStoreSubscription", "alice", page_id="p1", storename="page", prefix="form")
    lane.verb("notifyDbEvents", "alice", dbevents={"glbl.user": ["ins:1"]}, page_id="p0")
    worker.page_register.get("p1")["store"]["form.name"] = "Ada"
    collected = lane.verb("collect_page", "p1")
    assert [d["table"] for d in collected["dbevents"]] == ["glbl.user"]
    assert [c["key"]["path"] for c in collected["datachanges"]] == ["form.name"]
    assert lane.verb("collect_page", "p1")["dbevents"] == []
