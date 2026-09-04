# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Contract: what a verb cannot deliver is refused at the call, and reaches no desk.

A STATE kind this pass does not deliver, or a filtered address, raises in the
caller's own call; nothing refused is filed anywhere, and the writes around it
land as if it never happened. Migrated from the core (genro-asgi
``tests/orchestration/test_contract_phase14_verb_refusal.py`` at ``f3c7962``) with #59.
"""

from __future__ import annotations

import logging

import pytest
from genro_bag import Bag
from genro_bag.datachange import DataChangeCollector
from genro_tytx import to_tytx


def foreign_change(path: str, value):
    """A change born elsewhere, TYTX-encoded the way the site hands it over."""
    source = Bag()
    producer = DataChangeCollector(source)
    source[path] = value
    return to_tytx(producer.drain()[-1], "json")


@pytest.fixture
def page_lane(lane):
    lane.worker.new_page("u1", page_id="p1", connection_id="s1")
    lane.deliver_worker_events()
    lane.open_request()
    return lane


def test_a_state_kind_this_pass_does_not_deliver_is_refused_at_the_call(page_lane):
    for kind in ("page_store", "connection_store"):
        with pytest.raises(NotImplementedError, match=kind):
            page_lane.verb(
                "set_datachange", "u1", change=foreign_change("x.y", 1), kind=kind, target="p1"
            )
    slots = page_lane.verb("collect_page", "p1")
    assert slots["datachanges"] == []


def test_a_filtered_address_fails_alone(page_lane):
    page_lane.verb("set_datachange", "u1", change=foreign_change("before.x", 1), target="p1")
    with pytest.raises(NotImplementedError, match="filtered"):
        page_lane.verb(
            "set_datachange", "u1", change=foreign_change("bad.x", 2), filters="user:alice"
        )
    page_lane.verb("set_datachange", "u1", change=foreign_change("after.x", 3), target="p1")
    delivery = page_lane.verb("collect_page", "p1")
    assert [c["key"]["path"] for c in delivery["datachanges"]] == ["before.x", "after.x"]


def test_nothing_refused_ever_reaches_the_desk(page_lane):
    with pytest.raises(NotImplementedError):
        page_lane.verb(
            "set_datachange", "u1", change=foreign_change("bad.x", 2), filters="user:alice"
        )
    unknown = page_lane.verb(
        "set_datachange", "u1", change=foreign_change("bad.y", 3), target="ghost"
    )
    assert unknown["filed"] is False and unknown["local"] is False
    assert page_lane.verb("reset_datachanges", "u1")["filed"] is False
    delivery = page_lane.verb("collect_page", "p1")
    assert delivery["datachanges"] == []
    assert page_lane.desk.page_datachange_map == {}
    assert page_lane.desk.user_store_change_map == {}


def test_a_user_store_write_nobody_holds_is_said_out_loud(page_lane, caplog):
    # The site's legacy surface returns None whatever happened: the one loss it
    # cannot notice is logged by the worker, with the user and the path.
    with caplog.at_level(logging.WARNING, logger="genro_asgi.spa.orchestration.spa_worker"):
        answer = page_lane.verb(
            "set_datachange",
            "u1",
            change=foreign_change("prefs.theme", "dark"),
            kind="user_store",
            target="nobody.here",
        )
    assert answer["filed"] is False and answer["local"] is False
    records = [r for r in caplog.records if "went nowhere" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    assert "nobody.here" in records[0].getMessage()
    assert "prefs.theme" in records[0].getMessage()
