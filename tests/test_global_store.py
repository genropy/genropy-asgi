# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""The global store as ONE DICTIONARY on the commander (genro-asgi #74), from the bridge.

The commander owns ``dict[str, Any]``: literal string keys, opaque values, one
FIFO lock. The legacy site speaks dotted paths, so the bridge's
``GlobalStoreAdapter`` maps the FIRST path segment onto the dictionary key and
interprets the rest of the path inside the legacy Bag stored under it. These
tests assert that contract from the site's side, on the REAL LANE
(``tests/lane.py``): a ``GenropyWorker`` hosting the site, its handler, and a
real commander with its dictionary — the only copy there is.

What is asserted on the master is the TYPED value (a legacy ``Bag``, a
``datetime``, an ``int``), never a wire string: the values travel TYTX and the
master holds what was decoded.

The with-blocks run on the pytest thread — the WSGI thread of production —
while the lane's loop serves the commander on its own thread. Two workers of
the same pool appear where a test needs sibling leaves written from different
processes.
"""

import concurrent.futures
import datetime
import importlib.util
import threading
from types import SimpleNamespace

import pytest

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")

#: How long a test waits for a thread it parked on purpose.
THREAD_TIMEOUT = 30.0


@pytest.fixture(scope="module")
def store_lane():
    from tests.lane import start_site_lane

    try:
        instance = start_site_lane(_SITE, worker_name="store_0001")
    except Exception as exc:  # site missing or broken: skip, don't fail
        pytest.skip(f"cannot start the {_SITE} lane: {exc}")
    yield instance
    instance.stop()


@pytest.fixture(scope="module")
def sibling_lane(store_lane):
    """A SECOND worker under the same commander: sibling leaves from two processes."""
    from tests.lane import start_site_lane

    instance = start_site_lane(_SITE, sibling=store_lane, worker_name="store_0002")
    yield instance
    instance.stop()


@pytest.fixture()
def master(store_lane):
    """The only copy there is: the commander's own dictionary, emptied per test."""
    store = store_lane.commander.global_register
    store.clear()
    return store


@pytest.fixture()
def register(store_lane, master):
    """The site's register — the surface the legacy calls."""
    return store_lane.worker.gnr_site.register


@pytest.fixture()
def sibling_register(sibling_lane, master):
    """The register of the second worker, on the same dictionary."""
    return sibling_lane.worker.gnr_site.register


def legacy_bag():
    from gnr.core.gnrbag import Bag

    return Bag()


# ------------------------------------------------------------------
# Outside a turn: the first segment is the key
# ------------------------------------------------------------------


def test_a_scalar_write_is_one_key_of_the_dictionary(register, master):
    register.globalStore().setItem("RESTART_TS", 7)
    assert master["RESTART_TS"] == 7
    assert register.globalStore().getItem("RESTART_TS") == 7


def test_a_subpath_write_builds_the_legacy_bag_under_the_first_segment(register, master):
    from gnr.core.gnrbag import Bag

    register.globalStore().setItem("CACHE_TS.invoices", 3)
    assert isinstance(master["CACHE_TS"], Bag)
    assert master["CACHE_TS"].getItem("invoices") == 3
    assert register.globalStore().getItem("CACHE_TS.invoices") == 3


def test_a_whole_key_read_answers_the_entire_legacy_bag(register, master):
    from gnr.core.gnrbag import Bag

    store = register.globalStore()
    store.setItem("CACHE_TS.invoices", 1)
    store.setItem("CACHE_TS.customers", 2)
    cache = store.getItem("CACHE_TS")
    assert type(cache) is Bag
    assert cache.getItem("invoices") == 1
    assert cache.getItem("customers") == 2


def test_a_missing_path_answers_the_default(register, master):
    store = register.globalStore()
    assert store.getItem("never_written") is None
    assert store.getItem("never_written", 0) == 0
    assert store.getItem("never_written.deeper", 0) == 0


def test_a_stored_none_is_not_a_missing_key(register, master):
    store = register.globalStore()
    store.setItem("RESTART_TS", None)
    assert "RESTART_TS" in master
    assert store.getItem("RESTART_TS", 5) is None
    assert store.getItem("TASK_TS", 5) == 5


def test_a_sibling_leaf_survives_a_subpath_write(register, master):
    store = register.globalStore()
    store.setItem("CACHE_TS.invoices", 1)
    store.setItem("CACHE_TS.customers", 2)
    assert master["CACHE_TS"].getItem("invoices") == 1
    assert master["CACHE_TS"].getItem("customers") == 2


def test_a_subpath_delete_leaves_the_siblings_alone(register, master):
    store = register.globalStore()
    store.setItem("CACHE_TS.invoices", 1)
    store.setItem("CACHE_TS.customers", 2)
    store.delItem("CACHE_TS.invoices")
    assert master["CACHE_TS"].getItem("invoices") is None
    assert master["CACHE_TS"].getItem("customers") == 2


def test_deleting_the_whole_key_removes_it_from_the_dictionary(register, master):
    store = register.globalStore()
    store.setItem("CACHE_TS.invoices", 1)
    store.delItem("CACHE_TS")
    assert "CACHE_TS" not in master


def test_deleting_a_subpath_of_an_absent_key_creates_nothing_and_frees_the_lock(register, master):
    store = register.globalStore()
    store.delItem("CACHE_TS.invoices")
    # a normal exit would have published CACHE_TS = None: the turn must abort
    assert "CACHE_TS" not in master
    # and the lock is free — this write would park forever on a kept turn
    store.setItem("CACHE_TS.invoices", 1)
    assert master["CACHE_TS"].getItem("invoices") == 1


def test_a_path_under_a_scalar_key_answers_the_default(register, master):
    register.globalStore().setItem("RESTART_TS", 7)
    assert register.globalStore().getItem("RESTART_TS.deeper", 0) == 0


def test_the_whole_store_is_not_a_value_a_write_can_address(register, master):
    store = register.globalStore()
    with pytest.raises(ValueError):
        store.setItem("", 1)
    with pytest.raises(ValueError):
        store.delItem("")


def test_mutating_a_returned_bag_never_reaches_the_master(register, master):
    store = register.globalStore()
    store.setItem("CACHE_TS.invoices", 1)
    cache = store.getItem("CACHE_TS")
    cache.setItem("invoices", 999)
    cache.setItem("smuggled", 1)
    assert master["CACHE_TS"].getItem("invoices") == 1
    assert master["CACHE_TS"].getItem("smuggled") is None


def test_mutating_a_returned_dictionary_value_never_reaches_the_master(register, master):
    store = register.globalStore()
    store.setItem("RESTART_TS", {"stamp": 1})
    read = store.getItem("RESTART_TS")
    read["stamp"] = 999
    assert master["RESTART_TS"] == {"stamp": 1}


def test_an_aware_datetime_inside_a_bag_reads_back_naive_local(register, master):
    # gnrwebapp writes datetime.now() (naive) in CACHE_TS.* and compares with <:
    # an aware value coming back would raise TypeError in the legacy cache read.
    stamp = datetime.datetime(2026, 7, 10, 8, 30, 0)
    aware = stamp.astimezone()
    register.globalStore().setItem("CACHE_TS.stamp", aware)
    assert isinstance(master["CACHE_TS"].getItem("stamp"), datetime.datetime)
    back = register.globalStore().getItem("CACHE_TS.stamp")
    assert back.tzinfo is None
    assert back == stamp


# ------------------------------------------------------------------
# Two workers on one dictionary
# ------------------------------------------------------------------


def test_two_workers_write_two_leaves_of_the_same_key_and_both_survive(
    register, sibling_register, master
):
    # The read-modify-write turn is what makes this hold: a read-then-overwrite
    # would drop the leaf the other worker had already put under CACHE_TS.
    # The two writes are sequential on purpose — each lane of this harness runs
    # its own event loop, and the commander's FIFO lock is one asyncio.Lock,
    # which cannot be waited on from two loops. Contention is asserted between
    # two threads of ONE worker (test_another_thread_waits_until_the_turn_exits).
    register.globalStore().setItem("CACHE_TS.invoices", 1)
    sibling_register.globalStore().setItem("CACHE_TS.customers", 2)
    assert master["CACHE_TS"].getItem("invoices") == 1
    assert master["CACHE_TS"].getItem("customers") == 2
    # and either worker still reads the key as one complete Bag
    assert sibling_register.globalStore().getItem("CACHE_TS.invoices") == 1
    assert register.globalStore().getItem("CACHE_TS.customers") == 2


# ------------------------------------------------------------------
# Inside a turn: one whole-dictionary lock for the block
# ------------------------------------------------------------------


def test_a_turn_publishes_every_key_it_touched_at_once(register, master):
    register.globalStore().setItem("RESTART_TS", 1)
    with register.globalStore() as store:
        store.setItem("RESTART_TS", 2)
        store.setItem("CACHE_TS.invoices", 3)
        store.delItem("TASK_TS")
        assert master["RESTART_TS"] == 1  # nothing published mid-block
    assert master["RESTART_TS"] == 2
    assert master["CACHE_TS"].getItem("invoices") == 3


def test_a_turn_reads_what_it_has_just_written(register, master):
    with register.globalStore() as store:
        store.setItem("CACHE_TS.invoices", 5)
        assert store.getItem("CACHE_TS.invoices") == 5
        assert store.getItem("CACHE_TS").getItem("invoices") == 5


def test_a_turn_deletes_a_subpath_and_a_whole_key_together(register, master):
    store = register.globalStore()
    store.setItem("CACHE_TS.invoices", 1)
    store.setItem("CACHE_TS.customers", 2)
    store.setItem("RESTART_TS", 3)
    with register.globalStore() as turn:
        turn.delItem("CACHE_TS.invoices")
        turn.delItem("RESTART_TS")
    assert master["CACHE_TS"].getItem("invoices") is None
    assert master["CACHE_TS"].getItem("customers") == 2
    assert "RESTART_TS" not in master


def test_a_second_facade_inside_a_turn_sees_the_private_copy(register, master):
    with register.globalStore() as store:
        store.setItem("RESTART_TS", 9)
        # a second store built inside the block resolves the SAME active turn
        assert register.globalStore().getItem("RESTART_TS") == 9
        assert "RESTART_TS" not in master
    assert master["RESTART_TS"] == 9


def test_a_nested_turn_fails_at_once(register, master):
    with register.globalStore():
        with pytest.raises(RuntimeError):  # noqa: PT012 - the nesting IS the case
            with register.globalStore():
                pass
    # the outer turn was released normally: the next block runs
    with register.globalStore() as store:
        store.setItem("RESTART_TS", 1)
    assert master["RESTART_TS"] == 1


def test_a_raising_turn_body_applies_nothing_and_frees_the_lock(register, master):
    with pytest.raises(RuntimeError):  # noqa: PT012 - the body must raise inside
        with register.globalStore() as store:
            store.setItem("RESTART_TS", 1)
            raise RuntimeError("body failed")
    assert "RESTART_TS" not in master
    with register.globalStore() as store:
        assert store.getItem("RESTART_TS") is None


def test_another_thread_waits_until_the_turn_exits(register, master):
    read = {}
    answered = threading.Event()
    started = threading.Event()

    def read_outside():
        started.set()
        read["value"] = register.globalStore().getItem("RESTART_TS")
        answered.set()

    reader = threading.Thread(target=read_outside)
    with register.globalStore() as store:
        reader.start()
        started.wait(THREAD_TIMEOUT)
        store.setItem("RESTART_TS", 42)
        # the reader is parked on the commander's lock, which this block holds
        assert not answered.is_set()
    reader.join(THREAD_TIMEOUT)
    assert answered.is_set()
    assert read["value"] == 42  # it read the committed state, not the absent key


def test_a_thread_reused_after_a_turn_sees_no_stale_copy(register, master):
    def turn_body():
        with register.globalStore() as store:
            store.setItem("RESTART_TS", 1)

    def read_again():
        return register.globalStore().getItem("RESTART_TS")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(turn_body).result(THREAD_TIMEOUT)
        master["RESTART_TS"] = 2  # the master moves under the same thread
        assert pool.submit(read_again).result(THREAD_TIMEOUT) == 2


def test_the_user_conf_expire_read_modify_write_keeps_package_table_and_wildcard(
    register, master
):
    # adm/model/user_config.py emptyTableUserConfigCache, verbatim in shape:
    # read the whole key, build the Bag when absent, write it back.
    stamp = datetime.datetime(2026, 7, 10, 8, 30, 0)
    for key in ("mypkg.mytable", "mypkg.*", "*"):
        with register.globalStore() as gs:
            expirebag = gs.getItem("tables_user_conf_expire_ts")
            if not expirebag:
                expirebag = legacy_bag()
            expirebag[key] = stamp
            gs.setItem("tables_user_conf_expire_ts", expirebag)
    expirations = register.globalStore().getItem("tables_user_conf_expire_ts")
    assert expirations.getItem("mypkg.mytable") == stamp
    assert expirations.getItem("mypkg.*") == stamp
    assert expirations.getItem("*") == stamp


# ------------------------------------------------------------------
# The diagnostic reader and the pre-attach site
# ------------------------------------------------------------------


def test_the_diagnostic_read_answers_the_whole_dictionary(register, master):
    from gnr.core.gnrbag import Bag

    store = register.globalStore()
    store.setItem("RESTART_TS", 1)
    store.setItem("CACHE_TS.invoices", 2)
    for whole in (store.getItem(""), store.getItem(None), store.data):
        assert type(whole) is Bag
        assert whole.getItem("RESTART_TS") == 1
        assert whole.getItem("CACHE_TS.invoices") == 2
    # the diagnostic turn aborted: the dictionary is untouched and free
    assert set(master) == {"RESTART_TS", "CACHE_TS"}
    store.setItem("TASK_TS", 3)
    assert master["TASK_TS"] == 3


def test_the_diagnostic_read_inside_a_turn_answers_the_private_copy(register, master):
    with register.globalStore() as store:
        store.setItem("RESTART_TS", 1)
        assert store.data.getItem("RESTART_TS") == 1
        assert store.getItem("").getItem("RESTART_TS") == 1


def test_the_register_item_of_the_global_store_answers_the_adapter(register, master):
    register.globalStore().setItem("RESTART_TS", 1)
    item = register.get_item("*", register_name="global")
    assert item["register_name"] == "global"
    assert item["data"].getItem("RESTART_TS") == 1
    item["data"].setItem("RESTART_TS", 2)
    assert master["RESTART_TS"] == 2


def test_before_the_worker_attaches_the_store_is_local_only():
    from genropy_asgi.siteregister.siteregister_client import GenropyRegisterClient

    client = GenropyRegisterClient.__new__(GenropyRegisterClient)
    client.__dict__["site"] = SimpleNamespace(spa_worker=None)
    store = client.globalStore()
    store.setItem("CACHE_TS.invoices", 1)
    assert store.getItem("CACHE_TS.invoices") == 1
    assert store.getItem("RESTART_TS", 0) == 0
    assert store.data.getItem("CACHE_TS.invoices") == 1
    # the same local Bag answers the register item's own facade
    item = client.get_item("*", register_name="global")
    assert item["data"].getItem("CACHE_TS.invoices") == 1
    item["data"].delItem("CACHE_TS.invoices")
    assert store.getItem("CACHE_TS.invoices") is None


def test_a_turn_without_a_worker_is_a_locked_daemon():
    from genropy_asgi.siteregister.exceptions import GnrDaemonLocked
    from genropy_asgi.siteregister.siteregister_client import GenropyRegisterClient

    client = GenropyRegisterClient.__new__(GenropyRegisterClient)
    client.__dict__["site"] = SimpleNamespace(spa_worker=None)
    with pytest.raises(GnrDaemonLocked):
        client.globalStore().__enter__()
