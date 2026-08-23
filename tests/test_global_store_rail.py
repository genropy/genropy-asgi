# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Tests for the global-store rail (issue #1) on the core transport.

Two layers, matching the rail's own two halves:

- SHIP units on a bare client wired to a stub worker: leaf writes ship
  full-path TYTX scalars on ``store_set``/``store_del``, a snapshot
  materialization ships NOTHING back up (and validates before it touches the
  live Bag), and a lease that fails — on the grant or on the write-back — is
  always released. No site needed.
- The REAL LANE (``tests/lane.py``) — a GenropyWorker with its real handler
  and a real commander desk — for the owner design of the reads (2026-08-21):
  the store lives on the commander ALONE, a legacy leaf write ships up on the
  rail, and a lock-less ``globalStore().getItem(path)`` PAYS one ``store_get``
  and answers the master at the moment it was asked — there is no replica and
  no descending push to be stale. The LEASE (D4) is unchanged: the ``with
  globalStore()`` block holds the commander's master, its writes travel once
  on the release, all-or-nothing. The with-blocks run on the pytest thread —
  exactly the WSGI thread they run on in production — while the lane's loop
  serves the desk on its own thread.

The whole module skips when GenroPy (or, for the single, the site) is missing.
"""

import asyncio
import datetime
import time
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
# The real lane: the rail up, the paid read down, and the lease
# ------------------------------------------------------------------


def settle(predicate, timeout=SETTLE_TIMEOUT):
    """Poll a condition from the pytest thread; the lane's loop runs elsewhere."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("condition never became true")
        time.sleep(0.01)


@pytest.fixture(scope="module")
def lane():
    from tests.lane import start_site_lane

    try:
        instance = start_site_lane(_SITE)
    except Exception as exc:  # site missing or broken: skip, don't fail
        pytest.skip(f"cannot start the {_SITE} lane: {exc}")
    yield instance
    instance.stop()


@pytest.fixture()
def register(lane):
    return lane.worker.gnr_site.register


@pytest.fixture()
def master(lane):
    """The only copy there is: the commander's own global register."""
    return lane.commander.global_register


def test_legacy_write_reaches_master_and_the_paid_read_answers_it(register, master):
    register.global_bag.setItem("gnr.plain", 7)
    # the master is a blind courier: it holds the text the ascent shipped
    settle(lambda: master["gnr.plain"] == "7::L")
    # the lock-less read pays its store_get and decodes the shared suffix
    assert register.globalStore().getItem("gnr.plain") == 7


def test_the_read_answers_the_master_at_ask_time(register, master):
    # nothing was ever written from THIS process: there is no local copy that
    # could answer, so the value can only have crossed the lane right now
    master["gnr.fresh"] = "11::L"
    assert register.globalStore().getItem("gnr.fresh") == 11
    master["gnr.fresh"] = "12::L"  # the master moved: the next read sees it
    assert register.globalStore().getItem("gnr.fresh") == 12


def test_a_missing_path_answers_the_default(register):
    store = register.globalStore()
    assert store.getItem("gnr.never_written") is None
    assert store.getItem("gnr.never_written", default=0) == 0


def test_a_subtree_comes_back_as_a_legacy_bag(register, master):
    register.global_bag.setItem("gnr.tree.a", 1)
    register.global_bag.setItem("gnr.tree.b", 2)
    settle(lambda: master["gnr.tree.b"] == "2::L")
    from gnr.core.gnrbag import Bag

    tree = register.globalStore().getItem("gnr.tree")
    assert type(tree) is Bag
    assert tree["a"] == 1 and tree["b"] == 2


def test_a_delete_removes_the_leaf_for_every_reader(register, master):
    register.global_bag.setItem("gnr.transient", 1)
    settle(lambda: master["gnr.transient"] == "1::L")
    register.global_bag.delItem("gnr.transient")
    settle(lambda: master["gnr.transient"] is None)
    assert register.globalStore().getItem("gnr.transient") is None


def test_a_naive_datetime_survives_the_rail_and_stays_naive(register, master):
    # gnrwebapp writes datetime.now() (naive) in CACHE_TS.* and compares with <:
    # an aware value coming back would raise TypeError in the legacy cache read.
    stamp = datetime.datetime(2026, 7, 10, 8, 30, 0)
    register.global_bag.setItem("CACHE_TS.stamp", stamp)
    settle(lambda: master["CACHE_TS.stamp"] is not None)
    back = register.globalStore().getItem("CACHE_TS.stamp")
    assert back == stamp
    assert back.tzinfo is None


def test_two_sequential_lease_blocks_see_each_others_writes(register, master):
    with register.globalStore() as store:
        store.setItem("gnr.leased", 5)
    # the release crosses a tytx hop of its own, which the collected text already
    # pays for: the master ends holding the same wire text a lock-less write
    # leaves there, never one decode ahead of it
    settle(lambda: master["gnr.leased"] == "5::L")
    with register.globalStore() as store:
        assert store.getItem("gnr.leased") == 5


def test_a_typed_looking_string_survives_the_immediate_rail(register, master):
    # No lease: the leaf write ships once-encoded ('42::L' -> '42::L::T'), the
    # master holds that literal text (blind courier), and the paid read's single
    # decoding hop hands every reader back the string — never the int 42.
    register.global_bag.setItem("gnr.tricky", "42::L")
    settle(lambda: master["gnr.tricky"] == "42::L::T")
    assert register.globalStore().getItem("gnr.tricky") == "42::L"


def test_a_typed_looking_string_survives_the_lease_rail(register, master):
    with register.globalStore() as store:
        store.setItem("gnr.leased_tricky", "42::L")
    settle(lambda: master["gnr.leased_tricky"] == "42::L::T")
    assert register.globalStore().getItem("gnr.leased_tricky") == "42::L"


def test_a_leased_delete_reaches_the_master(register, master):
    with register.globalStore() as store:
        store.setItem("gnr.leased_doomed", 1)
    settle(lambda: master["gnr.leased_doomed"] == "1::L")
    with register.globalStore() as store:
        store.delItem("gnr.leased_doomed")
    settle(lambda: master["gnr.leased_doomed"] is None)


def test_a_raising_lease_body_applies_nothing_and_frees_the_master(register, master):
    with pytest.raises(RuntimeError):
        with register.globalStore() as store:
            store.setItem("gnr.doomed", 1)
            raise RuntimeError("body failed")
    # the master never saw the write...
    assert master["gnr.doomed"] is None
    # ...and the grant was released: the lock has no TTL and its waiters no
    # timeout, so a kept grant would park this block forever
    with register.globalStore() as store:
        assert store.getItem("gnr.doomed") is None


def test_a_plain_write_outside_the_lease_still_propagates(register, master):
    register.global_bag.setItem("gnr.lockless", True)
    settle(lambda: master["gnr.lockless"] == "True::B")
