# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""The subscription index: both maps consistent after any sequence.

Every assertion is made on the two maps together, because the point of the
class is that no primitive can leave one of them ahead of the other. The
empty case is asserted on the fan-out reader — a dbevent on an unsubscribed
table must cost a lookup that misses, not a walk.
"""

import threading

import pytest

from genropy_asgi.spa.subscription_index import SubscriptionIndex


@pytest.fixture
def index():
    return SubscriptionIndex()


def test_subscribe_feeds_both_maps(index):
    index.subscribe("p1", "sys.user")
    assert index.table_pages == {"sys.user": {"p1"}}
    assert index.page_tables == {"p1": {"sys.user"}}


def test_subscribe_is_idempotent(index):
    index.subscribe("p1", "sys.user")
    index.subscribe("p1", "sys.user")
    assert index.pages_for("sys.user") == {"p1"}
    assert index.tables_for("p1") == {"sys.user"}


def test_many_pages_one_table(index):
    for page_id in ("p1", "p2", "p3"):
        index.subscribe(page_id, "sys.user")
    assert index.pages_for("sys.user") == {"p1", "p2", "p3"}
    assert index.page_tables == {p: {"sys.user"} for p in ("p1", "p2", "p3")}


def test_one_page_many_tables(index):
    for table in ("sys.user", "sys.group", "adm.log"):
        index.subscribe("p1", table)
    assert index.tables_for("p1") == {"sys.user", "sys.group", "adm.log"}
    assert set(index.table_pages) == {"sys.user", "sys.group", "adm.log"}


def test_unsubscribe_clears_both_and_leaves_no_empty_set(index):
    index.subscribe("p1", "sys.user")
    index.unsubscribe("p1", "sys.user")
    assert index.table_pages == {}
    assert index.page_tables == {}


def test_unsubscribe_keeps_the_siblings(index):
    index.subscribe("p1", "sys.user")
    index.subscribe("p2", "sys.user")
    index.subscribe("p1", "sys.group")
    index.unsubscribe("p1", "sys.user")
    assert index.pages_for("sys.user") == {"p2"}
    assert index.tables_for("p1") == {"sys.group"}


def test_unsubscribe_of_unknown_pair_is_silent(index):
    index.subscribe("p1", "sys.user")
    index.unsubscribe("p2", "sys.user")
    index.unsubscribe("p1", "adm.log")
    index.unsubscribe("ghost", "nowhere")
    assert index.table_pages == {"sys.user": {"p1"}}
    assert index.page_tables == {"p1": {"sys.user"}}


def test_drop_page_clears_every_table_set_it_was_in(index):
    for table in ("sys.user", "sys.group", "adm.log"):
        index.subscribe("p1", table)
        index.subscribe("p2", table)
    index.drop_page("p1")
    assert index.page_tables == {"p2": {"sys.user", "sys.group", "adm.log"}}
    assert index.table_pages == {t: {"p2"} for t in ("sys.user", "sys.group", "adm.log")}


def test_drop_last_page_empties_the_index(index):
    index.subscribe("p1", "sys.user")
    index.subscribe("p1", "sys.group")
    index.drop_page("p1")
    assert index.table_pages == {}
    assert index.page_tables == {}


def test_drop_unknown_page_is_silent(index):
    index.subscribe("p1", "sys.user")
    index.drop_page("ghost")
    assert index.pages_for("sys.user") == {"p1"}


def test_empty_case_costs_a_missing_lookup(index):
    assert index.pages_for("sys.user") == set()
    assert index.tables_for("p1") == set()
    assert index.table_pages == {}
    assert index.page_tables == {}


def test_readers_hand_back_copies(index):
    index.subscribe("p1", "sys.user")
    pages = index.pages_for("sys.user")
    tables = index.tables_for("p1")
    pages.add("intruder")
    tables.add("intruder")
    index.subscribe("p2", "sys.user")
    assert index.pages_for("sys.user") == {"p1", "p2"}
    assert index.tables_for("p1") == {"sys.user"}


def test_any_primitive_sequence_keeps_the_maps_mirrored(index):
    sequence = [
        ("subscribe", "p1", "sys.user"),
        ("subscribe", "p2", "sys.user"),
        ("subscribe", "p1", "sys.group"),
        ("unsubscribe", "p2", "sys.user"),
        ("subscribe", "p3", "adm.log"),
        ("subscribe", "p1", "adm.log"),
        ("drop_page", "p1"),
        ("subscribe", "p2", "sys.group"),
        ("unsubscribe", "p3", "adm.log"),
    ]
    for step in sequence:
        if step[0] == "drop_page":
            index.drop_page(step[1])
        else:
            getattr(index, step[0])(step[1], step[2])
        forward = {(t, p) for t, pages in index.table_pages.items() for p in pages}
        backward = {(t, p) for p, tables in index.page_tables.items() for t in tables}
        assert forward == backward
        assert all(pages for pages in index.table_pages.values())
        assert all(tables for tables in index.page_tables.values())
    assert index.table_pages == {"sys.group": {"p2"}}
    assert index.page_tables == {"p2": {"sys.group"}}


def test_injected_lock_wraps_every_primitive():
    lock = threading.Lock()
    index = SubscriptionIndex(lock=lock)
    index.subscribe("p1", "sys.user")
    assert not lock.locked()
    with lock:
        assert index.lock is lock
    assert index.pages_for("sys.user") == {"p1"}
    index.drop_page("p1")
    assert index.page_tables == {}


def test_a_page_of_many_tables_and_a_table_of_many_pages_coexist(index):
    index.subscribe("p1", "sys.user")
    index.subscribe("p1", "sys.group")
    index.subscribe("p2", "sys.user")
    index.unsubscribe("p1", "sys.group")
    assert index.pages_for("sys.user") == {"p1", "p2"}
    assert index.tables_for("p1") == {"sys.user"}
    assert "sys.group" not in index.table_pages
