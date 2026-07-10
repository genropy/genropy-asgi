# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Unit tests for the global-store rail (issue #1) — no daemon, no site.

The legacy ``globalStore()`` Bag is wired to the framework's global-store rail:
every leaf write ships up as a ``store_set``/``store_del`` with a FULL-PATH key
and a TYTX-encoded SCALAR value; commander pushes (``/update_global``,
``/store_snapshot``) materialize back into the Bag without re-dispatching.

Same style as test_register_client_units: bare instances (``__new__``) wired to
stubs, run with GenroPy installed but WITHOUT a register daemon.
"""

import datetime
import importlib.util
from types import SimpleNamespace

import pytest

_HAS_GNR = importlib.util.find_spec("gnr") is not None

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")


class RailWorker:
    """Captures the store ops the register ships on the rail."""

    def __init__(self):
        self.calls = []

    def dispatch(self, op, args, kwargs, events=None):
        self.calls.append((op, args))


def make_client(worker=None):
    from genropy_asgi.siteregister.siteregister_client import GenropyRegisterClient

    client = GenropyRegisterClient.__new__(GenropyRegisterClient)
    app = SimpleNamespace(worker=worker)
    client.__dict__["site"] = SimpleNamespace(spa_application=app, currentRequest=None)
    return client


# ------------------------------------------------------------------
# Write path: legacy Bag mutations ship full-path TYTX scalars
# ------------------------------------------------------------------


def test_leaf_write_ships_full_path_scalar_and_autocreated_parent_ships_nothing():
    worker = RailWorker()
    client = make_client(worker)
    client.global_bag.setItem("CACHE_TS.invoices", 1.5)
    assert worker.calls == [("store_set", ("CACHE_TS.invoices", "1.5::R"))]


def test_update_ships_the_same_full_path_not_a_doubled_label():
    # The Bag update trigger's pathlist already ends with the node label: the
    # rail must not ship CACHE_TS.invoices.invoices.
    worker = RailWorker()
    client = make_client(worker)
    client.global_bag.setItem("CACHE_TS.invoices", 1.5)
    worker.calls.clear()
    client.global_bag.setItem("CACHE_TS.invoices", 2.5)
    assert worker.calls == [("store_set", ("CACHE_TS.invoices", "2.5::R"))]


def test_top_level_scalar_ships_typed():
    worker = RailWorker()
    client = make_client(worker)
    client.global_bag.setItem("flag", True)
    assert worker.calls == [("store_set", ("flag", "True::B"))]


def test_sibling_keys_never_touch_each_other():
    # The full-path mapping: writing one sibling ships one key only (the
    # top-level mapping would ship the whole CACHE_TS subtree and lose the
    # other worker's concurrent invalidation).
    worker = RailWorker()
    client = make_client(worker)
    client.global_bag.setItem("CACHE_TS.invoices", 1.0)
    client.global_bag.setItem("CACHE_TS.customers", 2.0)
    keys = [args[0] for op, args in worker.calls]
    assert keys == ["CACHE_TS.invoices", "CACHE_TS.customers"]


def test_wholesale_bag_set_ships_one_write_per_leaf():
    from gnr.core.gnrbag import Bag

    worker = RailWorker()
    client = make_client(worker)
    sub = Bag()
    sub.setItem("x", 1)
    sub.setItem("y.z", "deep")
    client.global_bag.setItem("sub", sub)
    assert sorted(worker.calls) == [
        ("store_set", ("sub.x", "1::L")),
        ("store_set", ("sub.y.z", "deep::T")),
    ]


def test_delete_leaf_ships_store_del():
    worker = RailWorker()
    client = make_client(worker)
    client.global_bag.setItem("CACHE_TS.invoices", 1.5)
    worker.calls.clear()
    client.global_bag.delItem("CACHE_TS.invoices")
    assert worker.calls == [("store_del", ("CACHE_TS.invoices",))]


def test_delete_subtree_ships_del_per_leaf():
    # The delete-per-prefix convention, day one: dropping CACHE_TS drops its keys.
    worker = RailWorker()
    client = make_client(worker)
    client.global_bag.setItem("CACHE_TS.invoices", 1.0)
    client.global_bag.setItem("CACHE_TS.customers", 2.0)
    worker.calls.clear()
    client.global_bag.delItem("CACHE_TS")
    assert sorted(worker.calls) == [
        ("store_del", ("CACHE_TS.customers",)),
        ("store_del", ("CACHE_TS.invoices",)),
    ]


def test_subtree_replace_dels_the_leaves_that_are_gone():
    from gnr.core.gnrbag import Bag

    worker = RailWorker()
    client = make_client(worker)
    old = Bag()
    old.setItem("a", 1)
    old.setItem("b", 2)
    client.global_bag.setItem("sub", old)
    worker.calls.clear()
    new = Bag()
    new.setItem("a", 10)
    client.global_bag.setItem("sub", new)
    assert ("store_set", ("sub.a", "10::L")) in worker.calls
    assert ("store_del", ("sub.b",)) in worker.calls


def test_string_with_typed_text_marker_survives_the_wire():
    worker = RailWorker()
    client_a = make_client(worker)
    client_a.global_bag.setItem("tricky", "42::L")
    op, (key, wire) = worker.calls[0]
    client_b = make_client(RailWorker())
    client_b.apply_global_write("store_set", key, wire)
    assert client_b.global_bag.getItem("tricky") == "42::L"


def test_naive_datetime_survives_the_wire_and_stays_naive():
    # gnrwebapp writes datetime.now() (naive) in CACHE_TS.* and compares with <:
    # an aware value coming back would raise TypeError in the legacy cache read.
    worker = RailWorker()
    client_a = make_client(worker)
    stamp = datetime.datetime(2026, 7, 10, 8, 30, 0)
    client_a.global_bag.setItem("CACHE_TS.k", stamp)
    op, (key, wire) = worker.calls[0]
    client_b = make_client(RailWorker())
    client_b.apply_global_write("store_set", key, wire)
    back = client_b.global_bag.getItem("CACHE_TS.k")
    assert back == stamp
    assert back.tzinfo is None


def test_missing_worker_never_breaks_the_legacy_write():
    client = make_client(worker=None)
    client.global_bag.setItem("flag", True)  # no rail: best-effort, no raise
    assert client.global_bag.getItem("flag") is True


# ------------------------------------------------------------------
# Read path: pushes materialize into the Bag without re-dispatch
# ------------------------------------------------------------------


def test_apply_global_write_materializes_without_redispatch():
    worker = RailWorker()
    client = make_client(worker)
    client.apply_global_write("store_set", "CACHE_TS.x", "3.5::R")
    assert client.global_bag.getItem("CACHE_TS.x") == 3.5
    assert worker.calls == []


def test_apply_store_del_removes_the_leaf_and_missing_key_is_silent():
    worker = RailWorker()
    client = make_client(worker)
    client.apply_global_write("store_set", "CACHE_TS.x", "1::L")
    client.apply_global_write("store_del", "CACHE_TS.x")
    assert client.global_bag.getItem("CACHE_TS.x") is None
    client.apply_global_write("store_del", "never.there")  # no raise
    assert worker.calls == []


def test_snapshot_replaces_the_bag_without_redispatch():
    worker = RailWorker()
    client = make_client(worker)
    client.global_bag.setItem("stale", "old")
    worker.calls.clear()
    client.load_global_snapshot({"CACHE_TS.a": "1.5::R", "flag": "True::B"})
    assert client.global_bag.getItem("stale") is None
    assert client.global_bag.getItem("CACHE_TS.a") == 1.5
    assert client.global_bag.getItem("flag") is True
    assert worker.calls == []


# ------------------------------------------------------------------
# The channel seam on the pool child
# ------------------------------------------------------------------


async def test_update_global_push_lands_on_replica_and_bag():
    from genro_asgi.applications.spa_application import GlobalStore

    from genropy_asgi.spa.genropy_worker_application import GenropyWorkerApplication

    worker = RailWorker()
    register = make_client(worker)
    app = GenropyWorkerApplication.__new__(GenropyWorkerApplication)
    app.global_store = GlobalStore()
    app._gnr_site = SimpleNamespace(register=register)
    envelope = {
        "path": "/update_global",
        "data": {"op": "store_set", "key": "CACHE_TS.k", "value": "7.5::R"},
    }
    await app.handle_channel_message(envelope)
    assert app.global_store.get("CACHE_TS.k") == "7.5::R"
    assert register.global_bag.getItem("CACHE_TS.k") == 7.5
    assert worker.calls == []


async def test_store_snapshot_push_replaces_replica_and_bag():
    from genro_asgi.applications.spa_application import GlobalStore

    from genropy_asgi.spa.genropy_worker_application import GenropyWorkerApplication

    register = make_client(RailWorker())
    register.global_bag.setItem("stale", "old")
    app = GenropyWorkerApplication.__new__(GenropyWorkerApplication)
    app.global_store = GlobalStore()
    app._gnr_site = SimpleNamespace(register=register)
    await app.handle_channel_message(
        {"path": "/store_snapshot", "data": {"flag": "True::B"}}
    )
    assert app.global_store.get("flag") == "True::B"
    assert register.global_bag.getItem("flag") is True
    assert register.global_bag.getItem("stale") is None
