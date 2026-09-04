# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""The source filter is pushed: every worker learns the set, whoever moved it.

A worker filters the commits of its own site against ``subscribed_tables``. That
set is global — it belongs to the desk — and no reply carries it: the commander
pushes it to EVERY living worker on every transition of it (a table gaining its
first subscriber, or losing its last), and to a newborn worker at its first
presentation. A worker that subscribed nothing itself therefore filters with the
same set as the one that did. Migrated from the core (genro-asgi
``tests/orchestration/test_contract_subscribed_tables_broadcast.py`` at ``f3c7962``)
with #59: two GenropyWorkers under one commander.
"""

from __future__ import annotations

import pytest

from tests.lane import wait_until

TABLE = "customer"


@pytest.fixture
def two_pages(two_lanes):
    """The two lanes with one page each, on the same commander and group."""
    lane, other = two_lanes
    lane.worker.add_connection("c1", sticky_cid="spa-c1")
    lane.worker.add_page("p1", "c1")
    lane.deliver_worker_events()
    other.worker.add_connection("c2", sticky_cid="spa-c2")
    other.worker.add_page("p2", "c2")
    other.deliver_worker_events()
    return lane, other


def subscribe(lane, page_id, table, subscribe=True):
    """Place the subscription the way the site places it."""
    return lane.verb("subscribeTable", "alice", table=table, page_id=page_id, subscribe=subscribe)


def test_a_subscription_on_one_worker_reaches_the_other(two_pages):
    lane, other = two_pages
    subscribe(lane, "p1", TABLE)
    wait_until(lambda: other.worker.subscribed_tables == {TABLE})
    other.open_request()
    other.verb("notifyDbEvents", "alice", dbevents={TABLE: ["ins:1"]}, page_id="p2")
    other.verb("collect_page", "p2")
    lane.open_request()
    delivery = lane.verb("collect_page", "p1")
    assert [deposit["table"] for deposit in delivery["dbevents"]] == [TABLE]
    assert delivery["dbevents"][0]["from_page_id"] == "p2"


def test_a_second_subscriber_of_the_same_table_pushes_nothing(two_pages, monkeypatch):
    lane, other = two_pages
    subscribe(lane, "p1", TABLE)
    wait_until(lambda: other.worker.subscribed_tables == {TABLE})
    pushes = []
    monkeypatch.setattr(
        lane.commander, "push_subscribed_tables", lambda handler: pushes.append(handler.name)
    )
    subscribe(other, "p2", TABLE)
    assert pushes == []
    assert lane.desk.subscribed_tables == [TABLE]


def test_the_last_subscriber_leaving_empties_every_worker(two_pages):
    lane, other = two_pages
    subscribe(lane, "p1", TABLE)
    wait_until(lambda: other.worker.subscribed_tables == {TABLE})
    subscribe(lane, "p1", TABLE, subscribe=False)
    wait_until(lambda: lane.worker.subscribed_tables == set())
    wait_until(lambda: other.worker.subscribed_tables == set())


def test_dropping_the_only_subscribing_page_empties_every_worker(two_pages):
    lane, other = two_pages
    subscribe(lane, "p1", TABLE)
    wait_until(lambda: other.worker.subscribed_tables == {TABLE})
    lane.on_loop(lane.desk.drop_page, "p1")
    wait_until(lambda: lane.worker.subscribed_tables == set())
    wait_until(lambda: other.worker.subscribed_tables == set())


def test_the_replayed_subscriptions_of_a_woken_page_are_announced(two_pages):
    lane, other = two_pages
    lane.on_loop(lane.commander.record_page_table_subscriptions, "p9", ["orders"])
    wait_until(lambda: lane.worker.subscribed_tables == {"orders"})
    wait_until(lambda: other.worker.subscribed_tables == {"orders"})
    lane.on_loop(lane.commander.drop_page, "p9")


def test_a_newborn_worker_gets_the_set_at_its_first_presentation(two_pages):
    from tests.lane import start_site_lane

    lane, _other = two_pages
    subscribe(lane, "p1", TABLE)
    wait_until(lambda: lane.desk.subscribed_tables == [TABLE])
    newborn = start_site_lane(lane.source, sibling=lane, worker_name="pool_0003")
    try:
        wait_until(lambda: newborn.worker.subscribed_tables == {TABLE})
    finally:
        newborn.stop()
