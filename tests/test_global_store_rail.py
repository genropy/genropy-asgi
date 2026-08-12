# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Tests for the global-store rail (issue #1) on the core transport.

Two layers, matching the rail's own two halves:

- SHIP/MATERIALIZE units on a bare client wired to a stub worker: leaf writes
  ship full-path TYTX scalars on ``store_set``/``store_del``, pushes
  materialize back without re-dispatch. No site needed.
- The REAL SINGLE — ``UserStickyCommander(workers=0, local_worker=True)``
  holding a GenropyWorker on a ``LocalChannel`` — for the write-through in
  both directions and the LEASE (D4): the ``with globalStore()`` block holds
  the commander's master, its writes travel once on the release,
  all-or-nothing. The lease's sync form blocks its thread on the worker's
  loop, so the with-blocks run in ``asyncio.to_thread`` — exactly the WSGI
  thread they run on in production.

The whole module skips when GenroPy (or, for the single, the site) is missing.
"""

import asyncio
import datetime
import importlib.util
from types import SimpleNamespace

import pytest

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")

SETTLE_TIMEOUT = 5.0


async def until(predicate, timeout=SETTLE_TIMEOUT):
    """Await a condition without blocking the loop."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise TimeoutError("condition never became true")
        await asyncio.sleep(0.01)


class RailWorker:
    """Captures the store ops the register ships on the rail."""

    def __init__(self):
        self.calls = []

    def store_set(self, identity, path, value=None):
        self.calls.append(("store_set", (path, value)))

    def store_del(self, identity, path):
        self.calls.append(("store_del", (path,)))


def make_client(worker=None):
    from genropy_asgi.siteregister.siteregister_client import GenropyRegisterClient

    client = GenropyRegisterClient.__new__(GenropyRegisterClient)
    client.__dict__["site"] = SimpleNamespace(spa_worker=worker)
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


def test_unacquirable_lease_maps_to_gnr_daemon_locked():
    from genropy_asgi.siteregister.exceptions import GnrDaemonLocked

    class DeadLockWorker(RailWorker):
        def global_store_lock(self):
            raise RuntimeError("channel down")

    client = make_client(DeadLockWorker())
    with pytest.raises(GnrDaemonLocked):
        client.globalStore().__enter__()


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
# The real single: write-through both directions, and the lease
# ------------------------------------------------------------------


@pytest.fixture
async def single():
    """A commander in the single role holding a real GenropyWorker in-process."""
    from genro_asgi.spa.commander import UserStickyCommander

    commander = UserStickyCommander(
        workers=0,
        local_worker=True,
        worker_class="genropy_asgi.spa.genropy_worker:GenropyWorker",
        worker_kwargs={"source": _SITE, "debug": False},
    )
    try:
        await commander.start()
    except Exception as exc:  # site missing or broken: skip, don't fail
        pytest.skip(f"cannot start the {_SITE} single: {exc}")
    try:
        yield commander
    finally:
        await commander.stop()


async def test_legacy_write_reaches_master_and_survives_the_echo(single):
    register = single.worker.gnr_site.register
    register.global_bag.setItem("gnr.plain", 7)
    # the master is a blind courier: it holds the text the ascent shipped
    await until(lambda: single.global_master.bag["gnr.plain"] == "7::L")
    # the descending hop crosses to_tytx/from_tytx, whose suffix grammar is the
    # shared historical one: the echo arrives DECODED on the replica...
    await until(lambda: single.worker.global_store["gnr.plain"] == 7)
    # ...and never bounced back up: the legacy Bag still reads the value
    assert register.global_bag.getItem("gnr.plain") == 7


async def test_descending_change_materializes_into_the_legacy_bag(single):
    register = single.worker.gnr_site.register
    # a write born elsewhere: straight on the worker's store op, not on the Bag
    single.worker.store_set(None, "gnr.remote", value="9::L")
    await until(lambda: register.global_bag.getItem("gnr.remote") == 9)


async def test_two_sequential_lease_blocks_see_each_others_writes(single):
    register = single.worker.gnr_site.register

    def first_block():
        with register.globalStore() as store:
            store.setItem("gnr.leased", 5)

    def second_block():
        with register.globalStore() as store:
            return store.getItem("gnr.leased")

    await asyncio.to_thread(first_block)
    # the release crossed a tytx hop, so the master holds the decoded value
    await until(lambda: single.global_master.bag["gnr.leased"] == 5)
    assert await asyncio.to_thread(second_block) == 5


async def test_a_raising_lease_body_applies_nothing(single):
    register = single.worker.gnr_site.register

    def failing_block():
        with register.globalStore() as store:
            store.setItem("gnr.doomed", 1)
            raise RuntimeError("body failed")

    with pytest.raises(RuntimeError):
        await asyncio.to_thread(failing_block)
    # the master never saw the write; the local Bag write stays local until
    # the next grant re-materializes the master over it
    assert single.global_master.bag["gnr.doomed"] is None

    def peek_block():
        with register.globalStore() as store:
            return store.getItem("gnr.doomed")

    assert await asyncio.to_thread(peek_block) is None


async def test_a_plain_write_outside_the_lease_still_propagates(single):
    register = single.worker.gnr_site.register
    register.global_bag.setItem("gnr.lockless", True)
    await until(lambda: single.global_master.bag["gnr.lockless"] == "True::B")
