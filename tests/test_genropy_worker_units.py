# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Unit tests for GenropyWorker, GenropyRegistry and the legacy-Bag capture.

The collector and registry tests need GenroPy (the legacy Bag) but no site;
the worker construction tests build a real ``GnrWsgiSite`` and skip cleanly
when the ``test_invoice_pg`` site cannot be built. State is always built
through the public API — registry lifecycle calls and real Bag writes —
never by wiring internal structures by hand.
"""

import datetime
import importlib.util
import os
import pickle
import tempfile
import uuid

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
    from genro_asgi.spa.orchestration import FreezeHandler

    from genropy_asgi.spa.genropy_worker import GenropyWorker

    deposit = tempfile.mkdtemp(prefix="gnr_frozen_")
    try:
        instance = GenropyWorker(
            "pool_0001", source=_SITE, debug=False, freeze_handler=FreezeHandler(deposit)
        )
    except Exception as exc:  # site missing or broken: skip, don't fail
        pytest.skip(f"cannot build the {_SITE} site: {exc}")
    yield instance
    instance.exit_process()


def test_worker_hosts_the_site_behind_the_wsgi_seam(worker):
    from genropy_asgi.spa.genropy_worker import GenropyRegistry

    assert worker.wsgi_app is worker.gnr_site  # debug=False: unwrapped
    assert worker.gnr_site.spa_worker is worker
    assert isinstance(worker.registry, GenropyRegistry)
    assert worker.gnr_site._local_mode is True


def test_the_sites_cleanup_age_becomes_the_idle_valve(worker):
    # no valve was named at construction: the site's <cleanup> decides — the
    # legacy connection_max_age seconds become the freeze-valve minutes, with
    # the daemon-parity default (7200s -> 120min) where the site is silent.
    # page_max_age/guest_max_age have no equivalent on this base by decision
    # (gate 2026-08-21): a silent tab's row lives until the site drops it or
    # its user freezes.
    from genropy_asgi.spa.genropy_worker import (
        IDLE_FREEZE_DEFAULT_SECONDS,
        IDLE_FREEZE_LEGACY_KEY,
    )

    cleanup = worker.gnr_site.custom_config.getAttr("cleanup") or {}
    seconds = int(cleanup.get(IDLE_FREEZE_LEGACY_KEY) or IDLE_FREEZE_DEFAULT_SECONDS)
    assert worker.user_idle_freeze_minutes == seconds / 60.0


def test_a_callers_idle_valve_wins_over_the_site(worker):
    from genro_asgi.spa.orchestration import FreezeHandler

    from genropy_asgi.spa.genropy_worker import GenropyWorker

    deposit = tempfile.mkdtemp(prefix="gnr_frozen_")
    explicit = GenropyWorker(
        "pool_0002",
        source=_SITE,
        debug=False,
        freeze_handler=FreezeHandler(deposit),
        user_idle_freeze_minutes=5.0,
    )
    try:
        assert explicit.user_idle_freeze_minutes == 5.0
    finally:
        explicit.exit_process()


def test_the_register_client_is_ours_and_wired_at_construction(worker):
    # The provider gate (genropy #1070) resolved gnr.web.daemon to this
    # package, and the worker captured the client on the init thread: the
    # site's lazy ``register`` property does db-touching work that must not
    # run on the event loop, so by the time the worker exists the client is
    # already built and points back at the worker.
    import gnr.web.daemon.siteregister_client as served

    import genropy_asgi.siteregister.siteregister_client as ours

    client = worker.gnr_site.register
    # The served module is OUR file under the legacy name (the provider gate
    # re-executes it there, so class identity is per-name: compare the file).
    assert served.__file__ == ours.__file__
    assert type(client).__name__ == "GenropyRegisterClient"
    assert client.spa_worker is worker


def test_a_bare_worker_is_not_a_pool_member(worker):
    # no wire attached: every row it ever made is its own, so the commit gate
    # may answer from the worker's subscription cache
    assert worker.pool_member is False


# ------------------------------------------------------------------
# Disk cleanup on the drop verbs (the successor of test_expiry_and_disk)
# ------------------------------------------------------------------


def fresh_ids():
    tag = uuid.uuid4().hex[:8]
    return f"user_{tag}", f"cid_{tag}", f"page_{tag}"


def make_dirs(worker, cid, page_id=None):
    path = os.path.join(worker.connections_folder, cid, *([page_id] if page_id else []))
    os.makedirs(path, exist_ok=True)
    return os.path.join(worker.connections_folder, cid)


def test_drop_page_takes_the_emptied_connections_folder(worker):
    user, cid, page_id = fresh_ids()
    worker.new_page(user, page_id=page_id, session_id=cid)
    folder = make_dirs(worker, cid, page_id)
    worker.drop_page(user, page_id, cascade=False)  # the legacy flag, absorbed
    assert worker.page_items.get(page_id) is None
    assert worker.connection_items.get(cid) is None  # last page: the core cascades
    assert not os.path.exists(folder)


def test_a_surviving_sibling_keeps_the_connection_folder(worker):
    user, cid, page_id = fresh_ids()
    sibling = f"{page_id}_b"
    worker.new_page(user, page_id=page_id, session_id=cid)
    worker.new_page(user, page_id=sibling, session_id=cid)
    folder = make_dirs(worker, cid, page_id)
    make_dirs(worker, cid, sibling)
    worker.drop_page(user, page_id)
    assert not os.path.exists(os.path.join(folder, page_id))
    assert os.path.exists(os.path.join(folder, sibling))
    worker.drop_connection(user, cid)
    assert not os.path.exists(folder)


def test_drop_user_takes_every_connection_folder(worker):
    user, cid, page_id = fresh_ids()
    other_cid = f"{cid}_b"
    worker.new_page(user, page_id=page_id, session_id=cid)
    worker.new_page(user, page_id=f"{page_id}_b", session_id=other_cid)
    folder = make_dirs(worker, cid, page_id)
    other_folder = make_dirs(worker, other_cid)
    worker.drop_user(user)
    assert worker.user_items.get(user) is None
    assert not os.path.exists(folder)
    assert not os.path.exists(other_folder)
