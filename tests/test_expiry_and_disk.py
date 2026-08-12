# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Tests for the armed expiry sweep and the disk cleanup on drop (Phase 5).

State is built through the register's public commands on a real GenropyWorker
with tiny expiry ages (fractions of a second), then the sweep is invoked
directly — the same method the armed loop calls every ``sweep_interval``.
Disk assertions run against the site's real ``data/_connections`` folder,
with per-test unique ids and best-effort cleanup.

The whole module skips when GenroPy or the site is missing.
"""

import asyncio
import importlib.util
import os
import shutil
import time
import uuid
from contextlib import contextmanager

import pytest

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")

TINY = 0.2  # the guest age the expiry tests wait out


@contextmanager
def call_sink(worker):
    """Open the sinks a CALL would open (the core's own test convention)."""
    events_token = worker._call_events.set([])
    tasks_token = worker._call_tasks.set([])
    try:
        yield
    finally:
        worker._call_events.reset(events_token)
        worker._call_tasks.reset(tasks_token)


@pytest.fixture(scope="module")
def worker():
    """One real site under a worker with a tiny guest age and huge logged ages."""
    from genropy_asgi.spa.genropy_worker import GenropyWorker

    try:
        instance = GenropyWorker(
            "W:test",
            source=_SITE,
            debug=False,
            guest_max_age=TINY,
            page_max_age=3600,
            connection_max_age=3600,
        )
    except Exception as exc:  # site missing or broken: skip, don't fail
        pytest.skip(f"cannot build the {_SITE} site: {exc}")
    yield instance
    asyncio.run(instance.shutdown())


@pytest.fixture()
def client(worker):
    return worker.gnr_site.register


def fresh_ids():
    tag = uuid.uuid4().hex[:8]
    return f"c_{tag}", f"p_{tag}"


def open_page(client, worker, user=None):
    cid, page_id = fresh_ids()
    with call_sink(worker):
        client.new_connection(cid, user=user)
        client.new_page(page_id, None, connection_id=cid, user=user)
    return cid, page_id


def connection_dir(worker, cid, page_id=None):
    path = os.path.join(worker.connections_folder, cid)
    return os.path.join(path, page_id) if page_id else path


def make_dirs(worker, cid, page_id=None):
    path = connection_dir(worker, cid, page_id)
    os.makedirs(path, exist_ok=True)
    return path


def test_knob_defaults_are_the_ratified_values():
    from genropy_asgi.spa.genropy_worker import (
        CONNECTION_MAX_AGE,
        GUEST_MAX_AGE,
        PAGE_MAX_AGE,
        SWEEP_INTERVAL,
    )

    assert (PAGE_MAX_AGE, GUEST_MAX_AGE, CONNECTION_MAX_AGE) == (600, 1800, 86400)
    assert SWEEP_INTERVAL == 60


def test_the_sweep_is_armed_by_default_with_the_knobs(worker):
    # the fixture's explicit ages land on the worker — a named age always wins
    # over the site's own <cleanup> values; the cadence is the default
    assert worker.sweep_interval == 60
    assert worker.guest_max_age == TINY
    assert worker.page_max_age == 3600
    assert worker.connection_max_age == 3600


def test_a_guest_expires_before_a_logged_connection(client, worker):
    guest_cid, guest_page = open_page(client, worker)
    logged_cid, logged_page = open_page(client, worker)
    with call_sink(worker):
        client.change_connection_user(logged_cid, user=f"alice_{logged_cid}")
    time.sleep(TINY * 2)
    dropped = worker.sweep_expired()
    assert guest_page in dropped["pages"]
    assert client.connection(guest_cid) is None  # the cascade took the guest chain
    assert client.page(logged_page) is not None  # the logged one survives its huge ages
    assert client.connection(logged_cid) is not None
    with call_sink(worker):
        client.drop_connection(logged_cid)  # leave the module worker clean


def test_a_page_folder_disappears_at_drop_page(client, worker):
    cid, page_id = open_page(client, worker)
    with call_sink(worker):
        client.change_connection_user(cid, user=f"bob_{cid}")
        second_page = fresh_ids()[1]
        client.new_page(second_page, None, connection_id=cid, user=f"bob_{cid}")
    page_dir = make_dirs(worker, cid, page_id)
    second_dir = make_dirs(worker, cid, second_page)
    try:
        with call_sink(worker):
            client.drop_page(page_id)
        assert not os.path.isdir(page_dir)  # the page folder went with the row
        assert os.path.isdir(connection_dir(worker, cid))  # the connection lives on
        with call_sink(worker):
            client.drop_page(second_page)  # the LAST page, and still no cascade
        assert not os.path.isdir(second_dir)
        assert client.connection(cid) is not None  # the browser is still there...
        assert os.path.isdir(connection_dir(worker, cid))  # ...and so is its folder
        with call_sink(worker):
            client.drop_connection(cid)  # the logout is what takes the chain
        assert not os.path.isdir(connection_dir(worker, cid))
    finally:
        shutil.rmtree(connection_dir(worker, cid), ignore_errors=True)


def test_an_explicit_cascade_takes_the_connection_folder_too(client, worker):
    cid, page_id = open_page(client, worker)
    make_dirs(worker, cid, page_id)
    try:
        with call_sink(worker):
            client.drop_page(page_id, cascade=True)
        assert client.connection(cid) is None
        assert not os.path.isdir(connection_dir(worker, cid))
    finally:
        shutil.rmtree(connection_dir(worker, cid), ignore_errors=True)


def test_a_logout_removes_the_whole_connection_folder(client, worker):
    cid, page_id = open_page(client, worker)
    make_dirs(worker, cid, page_id)
    try:
        with call_sink(worker):
            client.drop_connection(cid)
        assert not os.path.isdir(connection_dir(worker, cid))
    finally:
        shutil.rmtree(connection_dir(worker, cid), ignore_errors=True)


def test_an_orphan_folder_disappears_at_the_sweep_pass(client, worker):
    orphan = f"orphan_{uuid.uuid4().hex[:8]}"
    path = make_dirs(worker, orphan)
    try:
        stale = time.time() - worker.connection_max_age - 10
        os.utime(path, (stale, stale))
        worker.sweep_expired()
        assert not os.path.isdir(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_a_pool_child_leaves_the_shared_folder_alone(client, worker):
    """The orphan pass reads "no row of mine explains it" as "nobody's".

    True in the single, which holds every connection of the site; false in a
    pool, where the children share one ``data/_connections`` and each holds its
    own share — there the same question would answer with a sibling's live
    folders.
    """
    from genro_asgi.channel import ChannelClient

    orphan = f"sibling_{uuid.uuid4().hex[:8]}"
    path = make_dirs(worker, orphan)
    stale = time.time() - worker.connection_max_age - 10
    os.utime(path, (stale, stale))
    assert worker.pool_member is False  # the fixture's worker IS the single
    # what a spawned child does at boot: a real channel toward the commander
    worker.attach_channel(ChannelClient("tcp:127.0.0.1:1", "W:child"))
    try:
        assert worker.pool_member is True
        worker.sweep_expired()
        assert os.path.isdir(path)  # a sibling's folder, left alone
    finally:
        worker.channel = None  # back to the single the other tests expect
        shutil.rmtree(path, ignore_errors=True)
    assert worker.pool_member is False


def test_a_fresh_unknown_folder_survives_the_sweep_pass(client, worker):
    fresh = f"fresh_{uuid.uuid4().hex[:8]}"
    path = make_dirs(worker, fresh)
    try:
        worker.sweep_expired()
        assert os.path.isdir(path)  # younger than connection_max_age: kept
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_the_legacy_cleanup_is_disarmed(client):
    from genropy_asgi.siteregister.siteregister import DEFAULT_PAGE_MAX_AGE

    assert client.claim_cleanup() is False
    assert client.claim_cleanup(interval=0) is False  # never granted, whatever the ask
    assert client.expire_pages() == []
    assert client.expire_connection() == []
    assert DEFAULT_PAGE_MAX_AGE == 600
