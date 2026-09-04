# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Contract: the local data plane — subscriptions, deposit, drain.

The site-facing signatures are pinned here and DO NOT move (``setStoreSubscription``,
``collect_page``, ``set_datachange``, ``reset_datachanges``, ``drop_datachanges``):
``kind``, ``target`` and ``filters`` are in them already — local is one branch of an
addressing decision, not its absence. Every addressed write travels to the
commander's desk and comes back through the end-of-request exchange; what stays
purely local is the page's own capture — its row queue and its ``user_view``,
the page listening to itself. Migrated from the core (genro-asgi
``tests/orchestration/test_contract_phase3_data_plane.py`` at ``f3c7962``) with
#59, on the bridge's live lane: a GenropyWorker hosting the test site.
"""

from __future__ import annotations

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
def worker(lane):
    lane.worker.new_page("u1", page_id="p1", connection_id="s1")
    lane.deliver_worker_events()
    return lane.worker


# ----------------------------------------------------------------------
# setStoreSubscription: the page declares what it wants to hear about
# ----------------------------------------------------------------------


def test_the_page_queue_stays_empty_until_the_page_subscribes(worker):
    page = worker.page_register.get("p1")
    page["store"]["form.name"] = "Ada"
    assert page["datachanges"] == []
    assert page["subscribed_paths"] == set()


def test_the_page_subscription_opens_and_closes_its_own_store(worker):
    page = worker.page_register.get("p1")
    worker.setStoreSubscription("u1", page_id="p1", storename="page", prefix="form")
    page["store"]["form.name"] = "Ada"
    assert [c["key"]["path"] for c in page["datachanges"]] == ["form.name"]
    page["datachanges"] = []
    worker.setStoreSubscription("u1", page_id="p1", storename="page", prefix="form", active=False)
    assert page["subscribed_paths"] == set()
    page["store"]["form.name"] = "Grace"
    assert page["datachanges"] == []


def test_the_user_subscription_opens_the_view_and_widens_it(worker):
    page = worker.page_register.get("p1")
    user_store = worker.user_register.get("u1")["store"]
    worker.setStoreSubscription("u1", page_id="p1", storename="user", prefix="gnr.chat.msg")
    view = page["user_view"]
    assert page["store_subscriptions"] == {"gnr.chat.msg"}
    user_store["gnr.chat.msg.m1"] = "ciao"
    assert [c["key"]["path"] for c in view.drain()] == ["gnr.chat.msg", "gnr.chat.msg.m1"]
    worker.setStoreSubscription("u1", page_id="p1", storename="user", prefix="gnr.batch")
    assert page["user_view"] is view
    assert view.paths == {"gnr.chat.msg", "gnr.batch"}


def test_closing_a_user_subscription_a_page_never_took_is_a_no_op(worker):
    worker.setStoreSubscription("u1", page_id="p1", storename="user", prefix="prefs", active=False)
    assert worker.page_register.get("p1")["user_view"] is None


def test_an_unknown_storename_is_an_error(worker):
    with pytest.raises(ValueError, match="connection"):
        worker.setStoreSubscription("u1", page_id="p1", storename="connection", prefix="x")


def test_a_subscription_for_an_unknown_page_is_an_error(worker):
    with pytest.raises(KeyError, match="ghost"):
        worker.setStoreSubscription("u1", page_id="ghost", storename="page", prefix="x")


# ----------------------------------------------------------------------
# collect_page: one drain point, two species, merged in arrival order
# ----------------------------------------------------------------------


def test_collect_page_merges_both_collectors_by_ts(lane, worker):
    lane.verb("setStoreSubscription", "u1", page_id="p1", storename="page", prefix="form")
    lane.verb("setStoreSubscription", "u1", page_id="p1", storename="user", prefix="prefs")
    page = worker.page_register.get("p1")
    page["store"]["form.name"] = "Ada"
    worker.user_register.get("u1")["store"]["prefs.theme"] = "dark"
    page["store"]["form.age"] = 36
    collected = lane.verb("collect_page", "p1")
    assert [c["key"]["path"] for c in collected["datachanges"]] == [
        "form.name",
        "prefs",
        "prefs.theme",
        "form.age",
    ]
    assert collected["dbevents"] == []
    assert lane.verb("collect_page", "p1")["datachanges"] == []


def test_collect_page_drains_the_dbevents_species_apart(lane, worker):
    """The mailbox on the row is gone: the deposits come back from the desk."""
    lane.verb("subscribeTable", "u1", table="adm.user", page_id="p1")
    lane.wait_filter_synced()
    lane.verb("notifyDbEvents", "u1", dbevents={"adm.user": ["ins:1"]}, page_id="p1")
    collected = lane.verb("collect_page", "p1")
    assert [d["table"] for d in collected["dbevents"]] == ["adm.user"]
    assert collected["datachanges"] == []
    assert lane.verb("collect_page", "p1")["dbevents"] == []


def test_collect_page_of_an_unknown_page_is_an_error(worker):
    with pytest.raises(KeyError, match="nope"):
        worker.collect_page("nope")


# ----------------------------------------------------------------------
# set_datachange, local form: the explicit deposit lands whatever the filter says
# ----------------------------------------------------------------------


def test_the_explicit_deposit_ignores_the_page_filter(lane, worker):
    """An explicit write is not a capture: it lands whatever the page subscribed."""
    lane.verb("set_datachange", "u1", change=foreign_change("untold.x", 1), target="p1")
    collected = lane.verb("collect_page", "p1")
    assert [c["key"]["path"] for c in collected["datachanges"]] == ["untold.x"]


def test_the_signature_carries_the_addressing_it_will_grow_into(worker):
    """``kind``, ``target`` and ``filters`` are already in the signature — the
    local branch is a routing decision, not a smaller verb."""
    answer = worker.set_datachange(
        "u1", change=foreign_change("untold.x", 1), target="p1", filters=None, replace=False
    )
    assert answer["target"] == "p1"
    assert answer["filters"] is None
    assert answer["replace"] is False
    assert "kind" in answer
    assert answer["local"] is True


def test_replace_coalesces_the_pending_change_of_the_same_key(lane, worker):
    """The daemon's own dedup: written twice, delivered once."""
    for value in (1, 2):
        lane.verb(
            "set_datachange", "u1", change=foreign_change("untold.x", value), target="p1",
            replace=True,
        )
    changes = lane.verb("collect_page", "p1")["datachanges"]
    assert [c["key"]["path"] for c in changes] == ["untold.x"]
    assert changes[0]["value"] == 2


def test_reset_datachanges_empties_the_pending_without_reading_them(lane, worker):
    lane.verb("set_datachange", "u1", change=foreign_change("untold.x", 1), target="p1")
    lane.verb("reset_datachanges", "u1", target="p1")
    assert lane.verb("collect_page", "p1")["datachanges"] == []


def test_drop_datachanges_discards_only_the_path_it_names(lane, worker):
    lane.verb("set_datachange", "u1", change=foreign_change("form.name", "Ada"), target="p1")
    lane.verb("set_datachange", "u1", change=foreign_change("other.kept", "stays"), target="p1")
    lane.verb("drop_datachanges", "u1", path="form", target="p1")
    collected = lane.verb("collect_page", "p1")
    paths = [c["key"]["path"] for c in collected["datachanges"]]
    assert "form.name" not in paths
    assert "other.kept" in paths
