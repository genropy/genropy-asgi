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


def test_knob_defaults():
    """Page and connection defaults are DAEMON PARITY; the guest one is NOT.

    600/7200 are the daemon's own (setConfiguration,
    gnr/web/daemon/siteregister.py), used when the site's <cleanup> section
    is silent. The guest default is deliberately 1800, not the daemon's 40:
    the daemon enforced its 40 through a 5% per-request lottery behind a
    240-minute claim gate, while this bridge ARMS the sweep every 60s, and
    the only writer of the stamp the sweep compares is the page ping —
    auto_polling 30s by default, >=60s on browser-throttled hidden tabs,
    disabled in dev mode — so a 40s default would reap live guests between
    two of their own pings. A ``guest_connection_max_age`` set in <cleanup>
    still wins (the site-knob tests below).
    """
    from genropy_asgi.spa.genropy_worker import (
        CONNECTION_MAX_AGE,
        GUEST_MAX_AGE,
        PAGE_MAX_AGE,
        SITE_EXPIRY_KNOBS,
        SWEEP_INTERVAL,
    )

    assert (PAGE_MAX_AGE, GUEST_MAX_AGE, CONNECTION_MAX_AGE) == (600, 1800, 7200)
    # every worker knob maps to its legacy <cleanup> key with that same default
    assert SITE_EXPIRY_KNOBS == {
        "page_max_age": ("page_max_age", PAGE_MAX_AGE),
        "guest_max_age": ("guest_connection_max_age", GUEST_MAX_AGE),
        "connection_max_age": ("connection_max_age", CONNECTION_MAX_AGE),
    }
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
    # page expiry never cascades: the emptied guest connection expired at its
    # OWN (guest) age, in the sweep's connection pass
    assert guest_cid in dropped["connections"]
    assert client.connection(guest_cid) is None
    assert client.page(logged_page) is not None  # the logged one survives its huge ages
    assert client.connection(logged_cid) is not None
    with call_sink(worker):
        client.drop_connection(logged_cid)  # leave the module worker clean


def test_a_logged_pages_expiry_leaves_the_connection_alive(client, worker):
    """Page expiry never climbs: the daemon's ``expire_pages`` dropped the page
    bare, and the connection went later, at its OWN age (``expire_connection``).
    A logged user's backgrounded tab must not take the browser's connection row
    or its whole disk folder with it."""
    cid, page_id = open_page(client, worker)
    with call_sink(worker):
        client.change_connection_user(cid, user=f"carol_{cid}")
    make_dirs(worker, cid, page_id)
    worker.page_max_age = TINY
    try:
        time.sleep(TINY * 2)
        dropped = worker.sweep_expired()
        assert page_id in dropped["pages"]
        assert cid not in dropped["connections"]
        assert client.page(page_id) is None
        assert client.connection(cid) is not None  # the row survives...
        assert os.path.isdir(connection_dir(worker, cid))  # ...and so does its folder
        assert not os.path.isdir(connection_dir(worker, cid, page_id))  # the page's went
        worker.connection_max_age = TINY  # now the connection's own age comes due
        time.sleep(TINY * 2)
        dropped = worker.sweep_expired()
        assert cid in dropped["connections"]
        assert client.connection(cid) is None
        assert not os.path.isdir(connection_dir(worker, cid))
    finally:
        worker.page_max_age = 3600
        worker.connection_max_age = 3600
        shutil.rmtree(connection_dir(worker, cid), ignore_errors=True)


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


def test_the_two_demolition_branches_match_when_neither_climbs(client, worker):
    """The ``cascade=False`` branch transcribes the core demolition's steps.

    The same drop through the two branches, on twin connections that BOTH keep
    another page — so neither climbs in effect — must leave identical
    observable worker state: register reads, the subscriptions index, the disk,
    a subsequent ping — and must ANNOUNCE the same events (what a transcribed
    branch could most easily get wrong). If a future core demolition change
    misses the transcription, this turns red. Public commands only.
    """

    def open_pair(tag):
        cid, first = open_page(client, worker)
        user = f"{tag}_{cid}"
        with call_sink(worker):
            client.change_connection_user(cid, user=user)
            second = fresh_ids()[1]
            client.new_page(second, None, connection_id=cid, user=user)
        client.subscribeTable(first, table="fake.table")
        client.subscribeTable(second, table="fake.table")
        make_dirs(worker, cid, first)
        make_dirs(worker, cid, second)
        return cid, user, first, second

    def observe(cid, user, dropped_page, kept_page):
        return {
            "dropped_row": client.page(dropped_page),
            "kept_row_alive": client.page(kept_page) is not None,
            "connection_alive": client.connection(cid) is not None,
            "user_alive": client.user(user) is not None,
            "kept_is_the_only_page": set(client.pages(connection_id=cid)) == {kept_page},
            "index_kept_only": (
                worker.subscriptions.pages_for("fake.table") & {dropped_page, kept_page}
                == {kept_page}
            ),
            "dropped_tables": worker.subscriptions.tables_for(dropped_page),
            "ping_answers": client.refresh(kept_page) is not None,
            "page_folder_gone": not os.path.isdir(connection_dir(worker, cid, dropped_page)),
            "connection_folder_alive": os.path.isdir(connection_dir(worker, cid)),
        }

    def normalize(events, cid, user, dropped_page, kept_page):
        # the same announcements up to the per-pair ids and the seq stamp
        roles = {cid: "CID", user: "USER", dropped_page: "DROPPED", kept_page: "KEPT"}
        return [
            {key: roles.get(value, value) if isinstance(value, str) else value
             for key, value in event.items() if key != "seq"}
            for event in events
        ]

    cid_bare, user_bare, bare_dropped, bare_kept = open_pair("nf")
    cid_casc, user_casc, casc_dropped, casc_kept = open_pair("ct")
    try:
        with call_sink(worker):
            client.drop_page(bare_dropped, cascade=False)
            bare_events = list(worker._call_events.get())
        with call_sink(worker):
            client.drop_page(casc_dropped, cascade=True)
            casc_events = list(worker._call_events.get())
        bare_announced = normalize(bare_events, cid_bare, user_bare, bare_dropped, bare_kept)
        casc_announced = normalize(casc_events, cid_casc, user_casc, casc_dropped, casc_kept)
        assert bare_announced == casc_announced
        assert [event["op"] for event in bare_announced] == ["drop_page"]
        bare_state = observe(cid_bare, user_bare, bare_dropped, bare_kept)
        casc_state = observe(cid_casc, user_casc, casc_dropped, casc_kept)
        assert bare_state == casc_state
        assert bare_state["dropped_row"] is None
        assert bare_state["dropped_tables"] == set()
        for fact in (
            "kept_row_alive", "connection_alive", "user_alive",
            "kept_is_the_only_page", "index_kept_only", "ping_answers",
            "page_folder_gone", "connection_folder_alive",
        ):
            assert bare_state[fact], fact
    finally:
        with call_sink(worker):
            for cid in (cid_bare, cid_casc):
                if client.connection(cid) is not None:
                    client.drop_connection(cid)
        for cid in (cid_bare, cid_casc):
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


class SocketChannelShape:
    """The SHAPE of a spawned child's channel — non-Local, non-None — and nothing else.

    ``pool_member`` only asks ``isinstance``; a real ``ChannelClient`` attached
    via ``attach_channel`` would also rebind ``channel.on_message`` and
    ``outbox.notify`` onto itself, side effects a module-scoped worker must not
    carry into the other tests.
    """


def test_a_pool_child_leaves_the_shared_folder_alone(client, worker):
    """The orphan pass reads "no row of mine explains it" as "nobody's".

    True in the single, which holds every connection of the site; false in a
    pool, where the children share one ``data/_connections`` and each holds its
    own share — there the same question would answer with a sibling's live
    folders.
    """
    orphan = f"sibling_{uuid.uuid4().hex[:8]}"
    path = make_dirs(worker, orphan)
    stale = time.time() - worker.connection_max_age - 10
    os.utime(path, (stale, stale))
    assert worker.pool_member is False  # the fixture's worker IS the single
    worker.channel = SocketChannelShape()  # the shape a spawned child sits on
    try:
        assert worker.pool_member is True
        worker.sweep_expired()
        assert os.path.isdir(path)  # a sibling's folder, left alone
    finally:
        worker.channel = None  # back to the single the other tests expect
        shutil.rmtree(path, ignore_errors=True)
    assert worker.pool_member is False


def test_a_declared_pool_worker_spares_orphans_even_without_a_channel(client, worker):
    """The composition root's word outweighs the channel's shape.

    A front with ``local_worker=True`` AND ``workers > 0`` has its local worker
    on a ``LocalChannel`` — not a pool member by the channel test — yet it
    shares ``data/_connections`` with the spawned children: declared
    ``sole_registry_owner=False``, it must leave the shared root alone.
    """
    orphan = f"shared_{uuid.uuid4().hex[:8]}"
    path = make_dirs(worker, orphan)
    stale = time.time() - worker.connection_max_age - 10
    os.utime(path, (stale, stale))
    worker.sole_registry_owner = False  # what the composition root declares on a pool
    try:
        assert worker.pool_member is False  # no socket channel...
        worker.sweep_expired()
        assert os.path.isdir(path)  # ...and the shared root is spared anyway
    finally:
        worker.sole_registry_owner = None  # back to deriving from the channel
        shutil.rmtree(path, ignore_errors=True)
    assert worker.sole_registry_owner is True  # the derived single, restored


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
