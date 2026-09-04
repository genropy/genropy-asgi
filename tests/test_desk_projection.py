# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Contract: the desk's index is a projection of the page rows.

A page's subscriptions live on its row; the vertex rebuilds its index from what
every ``new_page`` announcement carries — empty at birth, the replayed set at
the wake — and forgets a page the moment the fold drops it. Migrated from the
core (genro-asgi ``tests/orchestration/test_contract_phase13_desk_projection.py``
at ``f3c7962``) with #59.
"""

from __future__ import annotations

from genro_asgi.spa import GUEST_PREFIX


def subscribed_guest(lane, table: str = "mytable") -> str:
    """A guest with one page whose subscription went through the lane.

    The vertex row placement would have created is seeded by hand: the lane
    has no reception in front of it.
    """
    lane.worker.add_connection("a1b2", sticky_cid="spa-a1b2")
    lane.worker.add_page("page-0", "a1b2")
    guest = f"{GUEST_PREFIX}a1b2"
    lane.commander.user_map[guest] = lane.commander._new_row()
    lane.verb("subscribeTable", guest, table, "page-0")
    lane.wait_filter_synced()
    return guest


def test_freezing_a_user_clears_his_pages_at_the_desk(lane):
    guest = subscribed_guest(lane)
    assert "page-0" in lane.commander.page_connection_map
    lane.desk.file_dbevent(
        {"table": "mytable", "batch": [], "from_page_id": "px", "reason": None, "ts": 0.0}
    )
    assert lane.run(lane.worker.freeze_user(guest)) is True
    lane.deliver_worker_events()
    assert "page-0" not in lane.desk.page_subscriptions.page_tables
    assert "page-0" not in lane.desk.page_datachange_map
    assert "page-0" not in lane.desk.page_dbevent_map
    assert "page-0" not in lane.commander.page_connection_map


def test_adoption_rebuilds_the_desk_index_from_the_replayed_rows(lane):
    guest = subscribed_guest(lane)
    assert lane.run(lane.worker.freeze_user(guest)) is True
    lane.deliver_worker_events()
    assert lane.desk.subscribed_tables == []
    lane.run(lane.worker.adopt_connection(guest, "a1b2"))
    lane.deliver_worker_events()
    assert lane.desk.subscribed_tables == ["mytable"]
    lane.wait_filter_synced()
    lane.open_request()
    lane.verb("notifyDbEvents", guest, {"mytable": [{"dbevent": "I", "pkey": "r1"}]})
    delivery = lane.verb("collect_page", "page-0")
    assert [deposit["table"] for deposit in delivery["dbevents"]] == ["mytable"]


def test_the_new_page_announcement_carries_the_rows_subscriptions(lane):
    # The births are read BEFORE the subscription: every round-trip on the wire
    # carries the pending announcements away, and the pushed source filter is one.
    lane.worker.add_connection("a1b2", sticky_cid="spa-a1b2")
    lane.worker.add_page("page-0", "a1b2")
    births = [event for event in lane.worker.worker_events if event["op"] == "new_page"]
    assert [event["table_subscriptions"] for event in births] == [[]]
    guest = f"{GUEST_PREFIX}a1b2"
    lane.commander.user_map[guest] = lane.commander._new_row()
    lane.verb("subscribeTable", guest, "mytable", "page-0")
    assert lane.run(lane.worker.freeze_user(guest)) is True
    lane.deliver_worker_events()
    lane.wait_filter_synced()
    lane.run(lane.worker.adopt_connection(guest, "a1b2"))
    wakes = [event for event in lane.worker.worker_events if event["op"] == "new_page"]
    assert [event["table_subscriptions"] for event in wakes] == [["mytable"]]
    lane.deliver_worker_events()
    assert lane.desk.page_subscriptions.page_tables["page-0"] == {"mytable"}


def test_the_drop_cascade_reaches_the_desk_and_is_pinned(lane):
    commander, desk = lane.commander, lane.desk
    commander.connection_user_map["c-1"] = "mario"
    commander.user_map["mario"] = commander._new_row()
    commander.page_connection_map["p-1"] = "c-1"
    lane.on_loop(desk.subscribe_table, "p-1", "mytable")
    desk.file_dbevent(
        {"table": "mytable", "batch": [], "from_page_id": "px", "reason": None, "ts": 0.0}
    )
    assert desk.page_dbevent_map["p-1"]
    lane.on_loop(commander.drop_connection, "c-1")
    assert "p-1" not in desk.page_subscriptions.page_tables
    assert "p-1" not in desk.page_dbevent_map
    assert "p-1" not in commander.page_connection_map
    desk.user_store_change_map["mario"] = [{"key": {}, "value": 1}]
    lane.on_loop(commander.drop_user, "mario")
    assert "mario" not in desk.user_store_change_map
    lane.wait_filter_synced()
