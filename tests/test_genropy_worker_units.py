# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Unit tests for GenropyWorker, GenropyRegistry and the legacy-Bag capture.

The collector and registry tests need GenroPy (the legacy Bag) but no site;
the worker construction tests build a real ``GnrWsgiSite`` and skip cleanly
when the ``test_invoice_pg`` site cannot be built. State is always built
through the public API — registry lifecycle calls and real Bag writes —
never by wiring internal structures by hand.
"""

import asyncio
import datetime
import importlib.util
import pickle

import pytest

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")


def make_bag():
    from gnr.core.gnrbag import Bag

    return Bag()


def make_collector(bag, paths=None):
    from genropy_asgi.spa.legacy_bag import LegacyBagCollector

    return LegacyBagCollector(bag, paths=paths)


# ------------------------------------------------------------------
# LegacyBagCollector: capture and consumption
# ------------------------------------------------------------------


def test_capture_and_drain_order():
    bag = make_bag()
    bag["a.b"] = 1  # pre-existing structure: no collector yet
    collector = make_collector(bag)
    bag["a.b"] = 2
    bag["a.c"] = 3
    bag["a.b"] = 4
    changes = collector.drain()
    paths = [c["key"]["path"] for c in changes]
    assert paths == ["a.b", "a.c", "a.b"]
    assert [c["change_idx"] for c in changes] == sorted(c["change_idx"] for c in changes)
    assert changes[0]["value"] == 2 and changes[0]["delete"] is False
    assert changes[0]["change_ts"].tzinfo is not None
    assert changes[0]["key"]["fired"] is False
    assert collector.pending == 0


def test_insert_and_delete_rebuild_the_full_path():
    bag = make_bag()
    collector = make_collector(bag)
    bag["a.b"] = 1  # autocreate of 'a', then insert of 'a.b'
    paths = [c["key"]["path"] for c in collector.drain()]
    assert paths == ["a", "a.b"]
    del bag["a.b"]
    (change,) = collector.drain()
    assert change["key"]["path"] == "a.b"
    assert change["delete"] is True
    assert change["value"] is None


def test_changes_peek_leaves_pending_intact():
    bag = make_bag()
    collector = make_collector(bag)
    bag["x"] = 1
    peeked = collector.drain(reset=False)
    assert len(peeked) == 1
    assert collector.pending == 1
    assert collector.changes == peeked


def test_append_with_replace_coalesces_on_equal_key():
    bag = make_bag()
    collector = make_collector(bag)
    bag["x"] = 1
    first = collector.changes[0]
    forwarded = {
        "key": dict(first["key"]),
        "value": 99,
        "attributes": None,
        "delete": False,
        "change_ts": datetime.datetime.now(datetime.UTC),
        "change_idx": 0,
    }
    collector.append(forwarded, replace=True)
    assert collector.pending == 1
    (survivor,) = collector.drain()
    assert survivor["value"] == 99
    assert survivor["change_idx"] > first["change_idx"]  # fresh idx, tail position
    assert forwarded["change_idx"] == 0  # the caller's dict is never mutated


def test_append_without_replace_keeps_both():
    bag = make_bag()
    collector = make_collector(bag)
    bag["x"] = 1
    collector.append(dict(collector.changes[0]))
    assert collector.pending == 2


def test_prefix_matches_on_segment_boundaries():
    bag = make_bag()
    bag["a.b.c"] = 0
    bag["a.bc"] = 0
    collector = make_collector(bag, paths={"a.b"})
    bag["a.bc"] = 1  # 'a.bc' is NOT under 'a.b'
    assert collector.pending == 0
    bag["a.b.c"] = 2
    bag["a.b"] = 3  # the prefix itself is captured too
    paths = [c["key"]["path"] for c in collector.drain()]
    assert paths == ["a.b.c", "a.b"]


def test_prefix_widening_and_narrowing():
    bag = make_bag()
    bag["a.x"] = 0
    bag["b.y"] = 0
    collector = make_collector(bag, paths={"a"})
    bag["b.y"] = 1
    assert collector.pending == 0
    collector.subscribe_path("b")
    bag["b.y"] = 2
    assert collector.pending == 1
    collector.unsubscribe_path("a")
    collector.unsubscribe_path("b")
    bag["a.x"] = 3  # empty set captures nothing (not the paths=None state)
    assert collector.pending == 1


def test_subscribe_path_on_capture_everything_starts_restricting():
    bag = make_bag()
    bag["a.x"] = 0
    bag["b.y"] = 0
    collector = make_collector(bag)  # paths=None: everything
    collector.subscribe_path("a")
    bag["b.y"] = 1
    assert collector.pending == 0
    bag["a.x"] = 2
    assert collector.pending == 1


def test_drop_discards_only_the_prefix():
    bag = make_bag()
    bag["a.x"] = 0
    bag["ab"] = 0
    collector = make_collector(bag)
    bag["a.x"] = 1
    bag["ab"] = 2
    collector.drop("a")
    (survivor,) = collector.drain()
    assert survivor["key"]["path"] == "ab"


def test_detach_stops_capture_and_keeps_pending():
    bag = make_bag()
    bag["x"] = 0
    collector = make_collector(bag)
    bag["x"] = 1
    collector.detach()
    bag["x"] = 2
    assert collector.pending == 1
    assert collector.drain()[0]["value"] == 1


# ------------------------------------------------------------------
# The ::BAG wire type
# ------------------------------------------------------------------


def test_bag_wire_type_round_trip():
    from genro_tytx import from_tytx, to_tytx

    import genropy_asgi.spa.legacy_bag  # noqa: F401  (registration happens at import)

    inner = make_bag()
    inner["num"] = 42
    inner["when"] = datetime.datetime(2026, 8, 12, 10, 30, 0)
    inner["label"] = "hello"
    wire = to_tytx({"value": inner})
    assert "::BAG" in wire
    back = from_tytx(wire)
    out = back["value"]
    assert type(out) is type(inner)  # a legacy Bag, never a genro_bag.Bag
    assert out["num"] == 42
    # The legacy wire serializes a naive datetime as aware local time and
    # parses it back aware — the historical ::BAG behaviour, reproduced
    # verbatim. The wall clock survives; consumers needing naive values
    # normalize at their own boundary (as the global rail already does).
    when = out["when"]
    assert when.tzinfo is not None
    assert when.replace(tzinfo=None) == datetime.datetime(2026, 8, 12, 10, 30, 0)
    assert out["label"] == "hello"


def test_the_two_bag_types_keep_their_own_suffixes():
    from genro_bag import Bag as CoreBag
    from genro_tytx import to_tytx

    import genropy_asgi.spa.legacy_bag  # noqa: F401

    core = CoreBag()
    core["k"] = 1
    assert "::X" in to_tytx({"value": core})
    legacy = make_bag()
    legacy["k"] = 1
    assert "::BAG" in to_tytx({"value": legacy})


# ------------------------------------------------------------------
# GenropyRegistry: legacy stores through the core lifecycle
# ------------------------------------------------------------------


def make_registry():
    from genropy_asgi.spa.genropy_worker import GenropyRegistry

    return GenropyRegistry()


def test_registry_rows_hold_legacy_stores_under_legacy_capture():
    from gnr.core.gnrbag import Bag
    from genropy_asgi.spa.legacy_bag import LegacyBagCollector

    registry = make_registry()
    page = registry.new_page("p1", user="bob", session_id="cid1")
    assert type(page["store"]) is Bag
    assert isinstance(page["collector"], LegacyBagCollector)
    assert type(registry.user_items.get("bob")["store"]) is Bag


def test_registry_store_survives_pickle_whole():
    registry = make_registry()
    registry.new_page("p1", user="bob", session_id="cid1")
    registry.subscribe_store_path("p1", "pref")  # a live user_view is attached
    store = registry.user_items.get("bob")["store"]
    store["pref.color"] = "red"
    clone = pickle.loads(pickle.dumps(store))
    assert type(clone) is type(store)
    assert clone["pref.color"] == "red"


def test_login_reattaches_the_user_view_and_redeposits_pending():
    from genropy_asgi.spa.legacy_bag import LegacyBagCollector

    registry = make_registry()
    registry.new_connection("cid1", user="bob")
    registry.new_page("p1", user="bob", session_id="cid1")
    registry.subscribe_store_path("p1", "pref")
    store = registry.user_items.get("bob")["store"]
    store["pref.color"] = "red"
    view = registry.page_items.get("p1")["user_view"]
    pending_before = view.pending
    assert pending_before > 0

    registry.change_connection_user("cid1", user="alice")

    fresh = registry.page_items.get("p1")["user_view"]
    assert fresh is not view
    assert isinstance(fresh, LegacyBagCollector)
    assert fresh.pending == pending_before  # re-deposited, never drained
    alice_store = registry.user_items.get("alice")["store"]
    assert fresh.bag is alice_store
    assert "bob" not in registry.user_items  # last connection left with the login
    # The fresh view captures on the NEW owner's store: the write lands as the
    # legacy pair autocreate('pref') + insert('pref.size').
    alice_store["pref.size"] = 10
    new_paths = [c["key"]["path"] for c in fresh.drain()[pending_before:]]
    assert new_paths == ["pref", "pref.size"]


# ------------------------------------------------------------------
# GenropyWorker: a real site behind the core wsgi_app seam
# ------------------------------------------------------------------


@pytest.fixture(scope="module")
def worker():
    """One real GnrWsgiSite hosted by a GenropyWorker; skip if the site is missing."""
    from genropy_asgi.spa.genropy_worker import GenropyWorker

    try:
        instance = GenropyWorker("W:test", source=_SITE, debug=False)
    except Exception as exc:  # site missing or broken: skip, don't fail
        pytest.skip(f"cannot build the {_SITE} site: {exc}")
    yield instance
    asyncio.run(instance.shutdown())


def test_worker_hosts_the_site_behind_the_wsgi_seam(worker):
    from genropy_asgi.spa.genropy_worker import GenropyRegistry

    assert worker.wsgi_app is worker.gnr_site  # debug=False: unwrapped
    assert worker.gnr_site.spa_worker is worker
    assert isinstance(worker.registry, GenropyRegistry)
    assert worker.gnr_site._local_mode is True
