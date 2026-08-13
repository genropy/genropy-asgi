# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Tests for the global-store rail (issue #1) on the core transport.

Two layers, matching the rail's own two halves:

- SHIP units on a bare client wired to a stub worker: leaf writes ship
  full-path TYTX scalars on ``store_set``/``store_del``, a snapshot
  materialization ships NOTHING back up (and validates before it touches the
  live Bag), and a lease that fails — on the grant or on the write-back — is
  always released. No site needed.
- The REAL SINGLE — ``UserStickyCommander(workers=0, local_worker=True)``
  holding a GenropyWorker on a ``LocalChannel`` — for the write-through in
  both directions (the DESCENT included: the pushes materialize through the
  worker's own frame handler, which is where the live entries are) and for
  the LEASE (D4): the ``with globalStore()`` block holds the commander's
  master, its writes travel once on the release, all-or-nothing. The lease's
  sync form blocks its thread on the worker's loop, so the with-blocks run in
  ``asyncio.to_thread`` — exactly the WSGI thread they run on in production.

The whole module skips when GenroPy (or, for the single, the site) is missing.
"""

import asyncio
import datetime
import importlib.util
from types import SimpleNamespace

import pytest
from genro_bag import Bag as CoreBag

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


class WorkingCopy:
    """Accepts ``set``/``delete`` like the core lease's working copy; records the ops."""

    def __init__(self):
        self.ops = []

    def set(self, path, value):
        self.ops.append(("set", path, value))

    def delete(self, path):
        self.ops.append(("delete", path))


class Lease:
    """Stub of the core ``GlobalStoreLease``: grants *master*, carries a working
    ``copy``, and records every ``__exit__`` type so a test can assert the release."""

    def __init__(self, master=None, copy=None):
        self.master = CoreBag() if master is None else master
        self.copy = WorkingCopy() if copy is None else copy
        self.exits = []

    def __enter__(self):
        return self.master

    def __exit__(self, exc_type, exc, tb):
        self.exits.append(exc_type)


class LeaseWorker(RailWorker):
    """A stub worker whose ``global_store_lock`` grants one prepared lease."""

    def __init__(self, lease):
        super().__init__()
        self.lease = lease

    def global_store_lock(self):
        return self.lease


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


def test_a_typed_looking_string_ships_suffixed():
    # A bare asTypedText would leave '42::L' unsuffixed, and the one decoding hop
    # of the descent would hand the int 42 to the legacy reader.
    worker = RailWorker()
    client = make_client(worker)
    client.global_bag.setItem("tricky", "42::L")
    assert worker.calls == [("store_set", ("tricky", "42::L::T"))]


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


def test_a_failed_grant_materialization_releases_the_lease():
    # The grant is already in force when the master content is poured into the
    # legacy Bag: a raise there must not keep the master — the core lock has no
    # TTL and its waiters park with no timeout, so the next block would never run.
    class BrokenMaster:
        def walk(self):
            raise RuntimeError("corrupt master")

    worker = LeaseWorker(Lease(master=BrokenMaster()))
    client = make_client(worker)
    with pytest.raises(RuntimeError):
        client.globalStore().__enter__()
    assert worker.lease.exits == [RuntimeError]  # released, and applying nothing


def test_a_write_the_working_copy_rejects_releases_the_lease():
    # Same rule at the other end: the collected writes are applied to the lease's
    # working copy on the way out, and one the copy refuses must not leave the
    # master locked — nor half the block applied.
    class RejectingCopy:
        def set(self, path, value):
            raise ValueError(f"the working copy rejects {path!r}")

        def delete(self, path):
            raise ValueError(f"the working copy rejects {path!r}")

    worker = LeaseWorker(Lease(copy=RejectingCopy()))
    client = make_client(worker)
    with pytest.raises(ValueError):
        with client.globalStore() as store:
            store.setItem("gnr.rejected", 1)
    assert worker.lease.exits == [ValueError]  # released with nothing applied


# ------------------------------------------------------------------
# Descent: snapshot materialization on the stub client
# ------------------------------------------------------------------


def test_a_snapshot_materialization_ships_nothing_back_up():
    # The snapshot is a descent (the replica seed): rebuilding the legacy Bag
    # from it runs under the applying flag, so not one store op may echo up.
    worker = RailWorker()
    client = make_client(worker)
    client.global_bag.setItem("gnr.stale", 1)  # pre-existing content, shipped
    worker.calls.clear()
    client._materialize_global_snapshot({"gnr.a": 1, "gnr.b": "x"})
    assert worker.calls == []
    assert client.global_bag.getItem("gnr.a") == 1
    assert client.global_bag.getItem("gnr.b") == "x"


def test_a_good_snapshot_clears_and_fills():
    worker = RailWorker()
    client = make_client(worker)
    client.global_bag.setItem("gnr.stale", 1)
    client._materialize_global_snapshot({"gnr.fresh": 2})
    assert client.global_bag.getItem("gnr.stale") is None  # cleared
    assert client.global_bag.getItem("gnr.fresh") == 2  # filled


def test_a_rejected_snapshot_leaf_leaves_the_bag_untouched():
    # Validate-then-apply: a leaf the legacy setItem rejects raises on the
    # SCRATCH Bag, before the live one is cleared. The '#3' leaf is SYNTHETIC
    # and unproducible by production: snapshots are built in this same process
    # by _replica_global_leaves, which yields plain dotted paths only. What is
    # guarded is the validate-then-apply MECHANISM itself — a rejected leaf
    # must leave the live bag full, never emptied or partially refilled.
    from gnr.core.gnrbag import BagException

    worker = RailWorker()
    client = make_client(worker)
    client.global_bag.setItem("gnr.kept", 3)
    worker.calls.clear()
    with pytest.raises(BagException):
        # '#3' is the index form the legacy Bag grammar refuses on an empty Bag
        client._materialize_global_snapshot({"gnr.good": 1, "#3": 2})
    assert client.global_bag.getItem("gnr.kept") == 3  # untouched, not emptied
    assert client.global_bag.getItem("gnr.good") is None  # and no partial fill
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
    # the site's own register client is the one the frame path materializes into:
    # the worker captured it at construction and never re-reads the lazy property
    assert register is single.worker.gnr_site.register


async def test_a_descending_change_never_bounces_back_up(single):
    register = single.worker.gnr_site.register
    # the value travels UNSUFFIXED, so an echo would be visible on the master:
    # a re-shipped 'plain' would land there as 'plain::T'
    single.worker.store_set(None, "gnr.echo", value="plain")
    await until(lambda: register.global_bag.getItem("gnr.echo") == "plain")
    await asyncio.sleep(0.1)
    assert single.global_master.bag["gnr.echo"] == "plain"


async def test_a_descending_delete_removes_the_leaf(single):
    register = single.worker.gnr_site.register
    single.worker.store_set(None, "gnr.transient", value="1::L")
    await until(lambda: register.global_bag.getItem("gnr.transient") == 1)
    single.worker.store_del(None, "gnr.transient")
    await until(lambda: register.global_bag.getItem("gnr.transient") is None)


async def test_a_snapshot_rebuilds_the_legacy_bag_from_the_master(single):
    # the frame a worker gets when its replica is seeded: the whole store at once
    register = single.worker.gnr_site.register
    register.global_bag.setItem("gnr.kept", 3)
    register.global_bag.setItem("gnr.gone", 1)
    await until(lambda: single.global_master.bag["gnr.gone"] == "1::L")
    single.global_master.delete("gnr.gone")  # the master moves on without it
    await single.bootstrap_replica(single.worker.name)
    await until(lambda: register.global_bag.getItem("gnr.gone") is None)
    assert register.global_bag.getItem("gnr.kept") == 3


async def test_a_naive_datetime_survives_the_rail_and_stays_naive(single):
    # gnrwebapp writes datetime.now() (naive) in CACHE_TS.* and compares with <:
    # an aware value coming back would raise TypeError in the legacy cache read.
    register = single.worker.gnr_site.register
    stamp = datetime.datetime(2026, 7, 10, 8, 30, 0)
    register.global_bag.setItem("CACHE_TS.stamp", stamp)
    # the replica landing is the round trip: the legacy materialization runs in
    # the same frame handler, right after it
    await until(lambda: single.worker.global_store["CACHE_TS.stamp"] is not None)
    back = register.global_bag.getItem("CACHE_TS.stamp")
    assert back == stamp
    assert back.tzinfo is None


async def test_two_sequential_lease_blocks_see_each_others_writes(single):
    register = single.worker.gnr_site.register

    def first_block():
        with register.globalStore() as store:
            store.setItem("gnr.leased", 5)

    def second_block():
        with register.globalStore() as store:
            return store.getItem("gnr.leased")

    await asyncio.to_thread(first_block)
    # the release crosses a tytx hop of its own, which the collected text already
    # pays for: the master ends holding the same wire text a lock-less write
    # leaves there, never one decode ahead of it
    await until(lambda: single.global_master.bag["gnr.leased"] == "5::L")
    assert await asyncio.to_thread(second_block) == 5


async def test_a_typed_looking_string_survives_the_immediate_rail(single):
    # No lease: the leaf write ships once-encoded ('42::L' -> '42::L::T'), the
    # master holds that literal text (blind courier), and the descent's single
    # decoding hop hands every reader back the string — never the int 42.
    register = single.worker.gnr_site.register
    register.global_bag.setItem("gnr.tricky", "42::L")
    await until(lambda: single.global_master.bag["gnr.tricky"] == "42::L::T")
    await until(lambda: single.worker.global_store["gnr.tricky"] == "42::L")
    assert register.global_bag.getItem("gnr.tricky") == "42::L"


async def test_a_typed_looking_string_survives_the_lease_rail(single):
    register = single.worker.gnr_site.register

    def block():
        with register.globalStore() as store:
            store.setItem("gnr.leased_tricky", "42::L")

    await asyncio.to_thread(block)
    await until(lambda: single.global_master.bag["gnr.leased_tricky"] == "42::L::T")
    # and the value comes back the string that was written, not the int 42
    await until(lambda: single.worker.global_store["gnr.leased_tricky"] == "42::L")
    assert register.global_bag.getItem("gnr.leased_tricky") == "42::L"


async def test_a_leased_delete_reaches_the_master(single):
    register = single.worker.gnr_site.register

    def write_block():
        with register.globalStore() as store:
            store.setItem("gnr.leased_doomed", 1)

    def delete_block():
        with register.globalStore() as store:
            store.delItem("gnr.leased_doomed")

    await asyncio.to_thread(write_block)
    await until(lambda: single.global_master.bag["gnr.leased_doomed"] == "1::L")
    await asyncio.to_thread(delete_block)
    await until(lambda: single.global_master.bag["gnr.leased_doomed"] is None)


async def test_a_raising_lease_body_applies_nothing_and_frees_the_master(single):
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

    # the lock has no TTL and its waiters no timeout: had the failure kept the
    # grant, this thread would park on it forever
    peeked = await asyncio.wait_for(asyncio.to_thread(peek_block), timeout=SETTLE_TIMEOUT)
    assert peeked is None


async def test_a_plain_write_outside_the_lease_still_propagates(single):
    register = single.worker.gnr_site.register
    register.global_bag.setItem("gnr.lockless", True)
    await until(lambda: single.global_master.bag["gnr.lockless"] == "True::B")
