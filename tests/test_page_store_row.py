# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Contract: the page store queue lives on the register row (GenropyPageRow).

The page row carries ``datachanges`` and ``datachanges_idx``; the capture
``GenropyRegistry.subscribe_page_store`` attaches to the row's legacy Bag fills
them with ``serverChange`` changes for the paths under ``subscribed_paths``.
The queue as a row field travels with the row: a collector object outside the
parcel was lost on freeze and transfer. Migrated from the core
(genro-asgi ``tests/orchestration/test_contract_page_store_row.py`` and
``tests/test_register_registry.py`` at ``f3c7962``) with #59: the row is the
bridge's now. The scenarios that need the worker's verbs and the vertex's desk
(``collect_page``, the addressed writes) live in the lane-driven suites.
"""

from __future__ import annotations

import importlib.util

import pytest

_HAS_GNR = importlib.util.find_spec("gnr") is not None

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")


@pytest.fixture
def registry():
    from genropy_asgi.spa.genropy_register import GenropyRegistry

    made = GenropyRegistry()
    made.new_page("p1", user="u1", connection_id="s1")
    return made


@pytest.fixture
def page(registry):
    row = registry.page_items.get("p1")
    row["subscribed_paths"].add("form")
    return row


def test_a_write_under_a_subscribed_prefix_is_queued_as_a_serverchange(page):
    page["store"]["form.name"] = "Ada"

    assert len(page["datachanges"]) == 1
    change = page["datachanges"][0]
    assert change["key"] == {"path": "form.name", "reason": "serverChange", "fired": False}
    assert change["value"] == "Ada"
    assert change["delete"] is False
    assert change["change_idx"] == 1

    page["store"]["form.age"] = 36

    assert [c["key"]["path"] for c in page["datachanges"]] == ["form.name", "form.age"]
    assert page["datachanges"][-1]["change_idx"] == 2
    assert page["datachanges_idx"] == 2


def test_the_autocreated_parents_of_a_write_are_not_changes(page):
    page["subscribed_paths"].add("a")
    page["store"].setItem("a.b.c", 1)

    assert [c["key"]["path"] for c in page["datachanges"]] == ["a.b.c"]


def test_a_prefix_matches_on_segment_boundaries(page):
    page["store"]["form2.name"] = "Ada"

    assert page["datachanges"] == []


def test_a_write_outside_every_prefix_is_not_queued(page):
    page["store"]["other.name"] = "Ada"

    assert page["datachanges"] == []


def test_after_detach_page_a_write_queues_nothing(registry, page):
    registry.detach_page(page)

    page["store"]["form.name"] = "Ada"

    assert page["datachanges"] == []


def test_a_page_born_with_a_queue_in_its_fields_keeps_it(registry):
    pending = [{"key": {"path": "form.name", "reason": "serverChange", "fired": False}}]

    woken = registry.new_page(
        "p2", user="u1", connection_id="s1", datachanges=pending, datachanges_idx=7
    )

    assert woken["datachanges"] == pending
    assert woken["datachanges_idx"] == 7


def test_the_row_is_born_with_the_sites_fields_and_no_dbevents(registry):
    from genropy_asgi.spa.genropy_register import GenropyPageRow

    row = registry.page_items.get("p1")
    assert type(row) is GenropyPageRow
    assert row["datachanges"] == [] and row["datachanges_idx"] == 0
    assert row["user_view"] is None
    assert row["subscribed_paths"] == set()
    assert row["store_subscriptions"] == set()
    assert row["table_subscriptions"] == set()
    assert "dbevents" not in row


def test_the_parcel_leaves_the_view_behind_and_replays_the_three_sets(registry):
    from genropy_asgi.spa.genropy_register import GenropyPageRow

    assert "user_view" in GenropyPageRow.fields_left_behind
    assert "connection_id" in GenropyPageRow.fields_left_behind
    assert GenropyPageRow.fields_replayed == (
        "subscribed_paths",
        "store_subscriptions",
        "table_subscriptions",
    )
    woken = registry.new_page("p2", user="u1", connection_id="s1")
    woken.replay_fields(
        registry,
        {"table_subscriptions": ["t"], "subscribed_paths": ["a"], "store_subscriptions": ["pref"]},
    )
    assert woken["table_subscriptions"] == {"t"}
    assert woken["subscribed_paths"] == {"a"}
    assert woken["store_subscriptions"] == {"pref"}
    assert woken["user_view"] is not None
    assert woken.announcement_fields() == {"table_subscriptions": ["t"]}


def test_drop_connection_detaches_the_capture_of_its_pages(registry, page):
    registry.subscribe_store_path("p1", "prefs")
    page["subscribed_paths"].add("x")
    view = page["user_view"]
    registry.drop_connection("s1")
    page["store"]["x"] = 1
    assert page["datachanges"] == []
    assert view.changes == []


def test_subscribe_store_path_unknown_page_raises_key_error(registry):
    with pytest.raises(KeyError, match="nope"):
        registry.subscribe_store_path("nope", "prefs")


def test_append_page_datachange_stamps_consecutive_indexes(registry):
    """The one append of a change to a page row numbers what it appends."""
    page = registry.page_items.get("p1")

    for path in ("a", "b"):
        registry.append_page_datachange(page, {"key": {"path": path}, "value": 1})

    assert [change["change_idx"] for change in page["datachanges"]] == [1, 2]
    assert page["datachanges_idx"] == 2


def test_append_page_datachange_with_replace_keeps_one_pending_per_key(registry):
    """``replace`` drops the pending change of the same key before appending."""
    page = registry.page_items.get("p1")

    registry.append_page_datachange(page, {"key": {"path": "a"}, "value": 1}, replace=True)
    registry.append_page_datachange(page, {"key": {"path": "a"}, "value": 2}, replace=True)

    assert [change["value"] for change in page["datachanges"]] == [2]
    assert page["datachanges"][0]["change_idx"] == 2


# ----------------------------------------------------------------------
# On the live lane: the collect and the addressed writes reach the row
# ----------------------------------------------------------------------


def foreign_change(path: str, value):
    """A change born elsewhere, TYTX-encoded the way the site hands it over."""
    from genro_bag import Bag
    from genro_bag.datachange import DataChangeCollector
    from genro_tytx import to_tytx

    source = Bag()
    producer = DataChangeCollector(source)
    source[path] = value
    return to_tytx(producer.drain()[-1], "json")


def test_collect_page_returns_the_queue_and_leaves_the_row_empty(lane):
    worker = lane.worker
    worker.new_page("u1", page_id="p1", connection_id="s1")
    lane.verb("setStoreSubscription", "u1", page_id="p1", storename="page", prefix="form")
    row = worker.page_register.get("p1")
    row["store"]["form.name"] = "Ada"
    collected = lane.verb("collect_page", "p1")
    assert [c["key"]["path"] for c in collected["datachanges"]] == ["form.name"]
    assert [c["key"]["reason"] for c in collected["datachanges"]] == ["serverChange"]
    assert row["datachanges"] == []
    assert row["datachanges_idx"] == 0


def test_two_requests_partition_the_queue_between_them(lane):
    from concurrent.futures import ThreadPoolExecutor

    worker = lane.worker
    worker.new_page("u1", page_id="p1", connection_id="s1")
    lane.verb("setStoreSubscription", "u1", page_id="p1", storename="page", prefix="srv")
    row = worker.page_register.get("p1")

    def in_own_request():
        worker.open_request_slot()
        return worker.collect_page("p1")

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="request-b") as other:
        row["store"]["srv.a"] = 1
        row["store"]["srv.b"] = 2
        first = other.submit(in_own_request).result(30)
        assert [c["key"]["path"] for c in first["datachanges"]] == ["srv.a", "srv.b"]
        row["store"]["srv.c"] = 3
        second = lane.verb("collect_page", "p1")
        assert [c["key"]["path"] for c in second["datachanges"]] == ["srv.c"]


def test_a_prefix_subscribed_after_birth_captures_the_next_write(lane):
    worker = lane.worker
    worker.new_page("u1", page_id="p1", connection_id="s1")
    row = worker.page_register.get("p1")
    row["store"]["late.name"] = "before"
    assert row["datachanges"] == []
    lane.verb("setStoreSubscription", "u1", page_id="p1", storename="page", prefix="late")
    row["store"]["late.name"] = "after"
    assert [c["key"]["path"] for c in row["datachanges"]] == ["late.name"]


@pytest.fixture
def two_pages(lane):
    """Two pages of the same user on the lane's worker."""
    lane.worker.new_page("u1", page_id="p1", connection_id="s1")
    lane.worker.new_page("u1", page_id="p2", connection_id="s1")
    return lane


def test_a_same_user_local_address_is_appended_to_the_target_row_at_once(two_pages):
    row = two_pages.worker.page_register.get("p2")
    answer = two_pages.verb("set_datachange", "u1", change=foreign_change("srv.x", 1), target="p2")
    assert answer["local"] is True
    assert [c["key"]["path"] for c in row["datachanges"]] == ["srv.x"]
    assert row["datachanges"][0]["change_idx"] == 1
    assert row["datachanges"][0]["key"]["reason"] is None
    assert two_pages.desk.page_datachange_map == {}


def test_collect_page_delivers_the_addressed_change_and_empties_the_row(two_pages):
    two_pages.verb("set_datachange", "u1", change=foreign_change("srv.x", 1), target="p2")
    collected = two_pages.verb("collect_page", "p2")
    assert [c["key"]["path"] for c in collected["datachanges"]] == ["srv.x"]
    assert two_pages.worker.page_register.get("p2")["datachanges"] == []


def test_a_server_write_and_an_addressed_write_share_one_index(two_pages):
    two_pages.verb("setStoreSubscription", "u1", page_id="p2", storename="page", prefix="srv")
    row = two_pages.worker.page_register.get("p2")
    row["store"]["srv.a"] = 1
    two_pages.verb("set_datachange", "u1", change=foreign_change("srv.b", 2), target="p2")
    assert [c["key"]["path"] for c in row["datachanges"]] == ["srv.a", "srv.b"]
    assert [c["change_idx"] for c in row["datachanges"]] == [1, 2]
    assert row["datachanges_idx"] == 2


def test_replace_coalesces_the_pending_change_of_the_same_key(two_pages):
    row = two_pages.worker.page_register.get("p2")
    for value in (1, 2):
        two_pages.verb(
            "set_datachange", "u1", change=foreign_change("srv.x", value), target="p2", replace=True
        )
    assert len(row["datachanges"]) == 1
    assert row["datachanges"][0]["value"] == 2


def test_reset_datachanges_empties_the_local_row_and_its_index(two_pages):
    row = two_pages.worker.page_register.get("p2")
    two_pages.verb("set_datachange", "u1", change=foreign_change("srv.x", 1), target="p2")
    two_pages.verb("reset_datachanges", "u1", target="p2")
    assert row["datachanges"] == []
    assert row["datachanges_idx"] == 0


def test_drop_datachanges_takes_one_prefix_out_of_the_local_row(two_pages):
    row = two_pages.worker.page_register.get("p2")
    for path in ("srv.a", "srv.a.x", "srv.ab"):
        two_pages.verb("set_datachange", "u1", change=foreign_change(path, 1), target="p2")
    two_pages.verb("drop_datachanges", "u1", "srv.a", target="p2")
    assert [c["key"]["path"] for c in row["datachanges"]] == ["srv.ab"]


def test_a_page_of_another_user_leaves_at_once_for_the_desk(two_pages):
    worker = two_pages.worker
    worker.new_page("u2", page_id="p9", connection_id="s9")
    two_pages.deliver_worker_events()
    row = worker.page_register.get("p9")
    two_pages.verb("set_datachange", "u1", change=foreign_change("srv.x", 1), target="p9")
    assert row["datachanges"] == []
    assert [c["key"]["path"] for c in two_pages.desk.page_datachange_map["p9"]] == ["srv.x"]
    assert not hasattr(worker.request_slot, "datachanges")


def test_a_request_that_never_collects_loses_no_addressed_write(two_pages):
    worker = two_pages.worker
    worker.new_page("u2", page_id="p9", connection_id="s9")
    two_pages.deliver_worker_events()
    two_pages.verb("set_datachange", "u1", change=foreign_change("srv.x", 1), target="p9")
    two_pages.open_request()
    assert [c["key"]["path"] for c in two_pages.desk.page_datachange_map["p9"]] == ["srv.x"]


def test_the_desks_change_is_stamped_by_the_row_that_retires_it(two_pages):
    worker = two_pages.worker
    worker.new_page("u2", page_id="p9", connection_id="s9")
    two_pages.deliver_worker_events()
    two_pages.verb("set_datachange", "u1", change=foreign_change("srv.x", 1), target="p9")
    collected = two_pages.verb("collect_page", "p9")
    assert [c["key"]["path"] for c in collected["datachanges"]] == ["srv.x"]
    assert collected["datachanges"][0]["change_idx"] >= 1
    row = worker.page_register.get("p9")
    assert row["datachanges"] == [] and row["datachanges_idx"] == 0
