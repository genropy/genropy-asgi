# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""End-to-end datachange scenarios against a real GnrWsgiSite on the NEW single.

These REQUIRE GenroPy and the ``test_invoice_pg`` site; they skip cleanly when
either is missing. No register daemon anywhere: the front is
``GenropySpaApplication`` (the core ``SpaApplicationNew``) attached to the live
lane of ``tests/lane.py`` — the GenropyWorker with its real handler and a real
commander desk, all in this process so the register and the db stay
inspectable — and every request crosses the demux, the ``http`` CALL forward,
the real wire and the WSGI seam.

The asserts encode the register semantics the daemon rail established: the same
scenarios stayed green across daemon -> daemonless -> core rebase — this suite
is the golden reference, not a byte-compare. The envelope keeps the daemon's
contract whole: only the explicitly written leaves travel (the autocreated
parents the legacy capture records stay internal), each with its ``fired``
flag verbatim, delivered once — the drain is destructive.

Scenario coverage:
- page open -> the site's connection cookie AND the routing cookie, both
  carrying the SAME connection id,
  ``page_id`` in the bootstrap HTML
- ping -> empty envelope when no changes are pending
- subscribeTable + notifyDbEvents -> delivered once on ping (collect is
  destructive), origin page NOT excluded (legacy semantics)
- real db write -> commit -> onDbCommitted -> notifyDbEvents -> delivered on ping
- user-store: setStoreSubscription + userStore().set_datachange -> the leaf
  change delivered on the first pull, nothing on later pulls (destructive drain)
- second tab (same user): the user change reaches it too, once each
- pageStore().set_datachange (the batch/thermo write) -> delivered on ping
- chat (the ct_send_message replay): the fired write reaches sender AND
  recipient with ``fired=True`` and only the written leaf in the envelope
"""

import asyncio
import importlib.util
import re
import uuid
from contextlib import contextmanager

import pytest

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")


@contextmanager
def call_sink(worker):
    """The old base required an open CALL sink around every op; the new base's
    verbs announce straight onto ``worker_events``. Kept as a no-op so the
    scenarios read unchanged across the rebase."""
    yield


@pytest.fixture(scope="module")
def lane():
    """The live lane: the GenropyWorker, its real handler, a real desk."""
    from tests.lane import start_site_lane

    try:
        instance = start_site_lane(_SITE)
    except Exception as exc:  # site missing or broken: skip, don't fail
        pytest.skip(f"cannot start the {_SITE} lane: {exc}")
    yield instance
    instance.stop()


@pytest.fixture()
def app(lane):
    """The front attached to the lane's live pool.

    In production the pool is born at startup from the recipe; the lane
    already holds a started commander with the site worker presented on it,
    so the front is handed that commander directly — the one seam this suite
    wires by hand. Every request still crosses the demux, the cookie mint,
    the pack and the real wire.
    """
    from genropy_asgi.spa import GenropySpaApplication

    front = GenropySpaApplication()
    front._commander = lane.commander
    front.lane = lane
    return front


@pytest.fixture()
def register(lane):
    return lane.worker.gnr_site.register


@pytest.fixture()
def flush(register):
    """Play the end-of-request of the pytest thread: deliver its queued writes.

    The core lays every addressed write on the caller's request slot and ships
    it at that thread's own exchange — in production the writer IS a request
    thread, and its collect is the shipment. The tests write from the pytest
    thread, so this fixture exchanges on a throwaway page whose own queue is
    empty by construction: the writes reach the desk and land in each TARGET
    page's queue, exactly as a real request's tail would leave them.
    """
    cid = f"flush_{uuid.uuid4().hex[:8]}"
    page_id = f"flushp_{uuid.uuid4().hex[:8]}"
    register.new_connection(cid, user=None)
    register.new_page(page_id, None, connection_id=cid)

    def _flush():
        assert register.subscription_storechanges(None, page_id) == []

    return _flush


def fire(app, method, path, query=b"", cookies=None, body=b""):
    """Drive one request through the full ASGI stack, on the lane's loop."""
    headers = [(b"cookie", cookies.encode())] if cookies else []
    scope = {
        "type": "http", "method": method, "path": path, "query_string": query,
        "headers": headers, "server": ("localhost", 8000),
        "client": ("127.0.0.1", 12345), "scheme": "http", "http_version": "1.1",
    }
    received = {"status": None, "headers": [], "body": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            received["status"] = message["status"]
            received["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            received["body"] += message.get("body", b"")

    asyncio.run_coroutine_threadsafe(app(scope, receive, send), app.lane.loop).result(30)
    return received


def merge_cookies(received, cookies=None):
    """Fold the response's set-cookie headers into the request cookie string.

    The new single answers with TWO cookies — the site's own (named after the
    site) and the front's ``spa_connection_id`` — and the client must present
    both: the site cookie is the legacy session, the other is the routing key,
    and they carry the same connection id.
    """
    jar = {}
    if cookies:
        for pair in cookies.split("; "):
            name, _, value = pair.partition("=")
            jar[name] = value
    for name, value in received["headers"]:
        if name == b"set-cookie":
            pair = value.decode().split(";")[0]
            cookie_name, _, cookie_value = pair.partition("=")
            jar[cookie_name] = cookie_value
    return "; ".join(f"{name}={value}" for name, value in jar.items())


def open_page(app, cookies=None):
    """GET the root page; return (page_id, cookie_jar_string)."""
    received = fire(app, "GET", "/", cookies=cookies)
    assert received["status"] == 200
    match = re.search(r"page_id:'([\w-]+)'", received["body"].decode(errors="replace"))
    assert match, "no page_id in the bootstrap HTML"
    return match.group(1), merge_cookies(received, cookies)


def ping(app, page_id, cookies):
    """GET /_ping for the page; return the envelope as a legacy Bag."""
    from gnr.core.gnrbag import Bag

    received = fire(
        app, "GET", "/_ping", query=f"page_id={page_id}".encode(), cookies=cookies
    )
    assert received["status"] == 200
    return Bag(received["body"].decode(errors="replace"))


def datachanges(envelope):
    """The dataChanges nodes of a ping envelope as a list of (path, value, attributes)."""
    changes = envelope["dataChanges"]
    if changes is None:
        return []
    return [(node.attr.get("change_path"), node.value, node.attr) for node in changes]


def paths_and_values(envelope):
    """The (path, value) pairs of an envelope — the WHOLE envelope, no filtering.

    The envelope carries the daemon contract: only the explicitly written
    leaves, never the autocreated parents (filtered at the envelope boundary).
    """
    return [(path, value) for path, value, _ in datachanges(envelope)]


def unique_table():
    return f"probe.t{uuid.uuid4().hex[:8]}"


def test_page_open_mints_both_cookies_and_the_page(app, register):
    page_id, cookie = open_page(app)
    jar = dict(pair.split("=", 1) for pair in cookie.split("; "))
    assert _SITE in jar  # the site's own legacy session cookie
    assert "spa_connection_id" in jar  # the routing cookie
    page_item = register.page(page_id)
    assert page_item is not None
    assert page_item["register_item_id"] == page_id
    # ONE identity: what routes is the connection the site itself created.
    assert jar["spa_connection_id"] == page_item["connection_id"]


def test_ping_with_no_changes_returns_empty_envelope(app):
    page_id, cookie = open_page(app)
    envelope = ping(app, page_id, cookie)
    assert datachanges(envelope) == []


def test_db_event_reaches_subscribed_page_once_including_origin(app, register, flush):
    page_id, cookie = open_page(app)
    table = unique_table()
    register.subscribeTable(page_id, table=table, subscribe=True)
    register.notifyDbEvents(
        {table: [{"dbevent": "U", "pkey": "K1"}]},
        register_name="page", origin_page_id=page_id, dbevent_reason="probe",
    )
    flush()
    changes = datachanges(ping(app, page_id, cookie))
    # delivered even though this page IS the origin (legacy does not exclude it)
    assert len(changes) == 1
    path, value, attr = changes[0]
    assert path == "gnr.dbchanges." + table.replace(".", "_")
    assert attr["change_attr"]["from_page_id"] == page_id
    # the collect is destructive: nothing on the next ping
    assert datachanges(ping(app, page_id, cookie)) == []


def test_real_db_commit_notifies_subscribed_page(app, register, flush):
    page_id, cookie = open_page(app)
    table = "invc.customer_type"
    register.subscribeTable(page_id, table=table, subscribe=True)
    db = app.lane.worker.gnr_site.db
    code = uuid.uuid4().hex[:5]
    tbl = db.table(table)
    tbl.insert({"code": code, "description": "e2e probe"})
    db.commit()
    flush()
    changes = datachanges(ping(app, page_id, cookie))
    assert any(path == "gnr.dbchanges.invc_customer_type" for path, _, _ in changes)
    # clean up the record and drain the resulting event
    tbl.delete({"code": code})
    db.commit()
    flush()
    ping(app, page_id, cookie)


def test_user_store_change_delivered_then_drained(app, register, flush):
    # The core model: the user-store write is a REAL write on the owner's live
    # store, captured by each subscribed page's own user_view — no offsets, no
    # ``_new_datachange`` bookkeeping. The drain is destructive.
    page_id, cookie = open_page(app)
    user = register.connection(register.page(page_id)["connection_id"])["user"]
    register.setStoreSubscription(page_id, "user", "chat", True)
    with register.userStore(user) as store:
        store.set_datachange("chat.msg", "hello")
    flush()
    assert paths_and_values(ping(app, page_id, cookie)) == [("chat.msg", "hello")]
    # destructive drain: the same change never comes back on later pulls
    assert datachanges(ping(app, page_id, cookie)) == []


def test_user_store_change_reaches_both_tabs_once_each(app, register, flush):
    page1, cookie = open_page(app)
    page2, cookie = open_page(app, cookies=cookie)  # same connection, same user
    connection_id = register.page(page1)["connection_id"]
    assert register.page(page2)["connection_id"] == connection_id
    user = register.connection(connection_id)["user"]
    register.setStoreSubscription(page1, "user", "news", True)
    register.setStoreSubscription(page2, "user", "news", True)
    with register.userStore(user) as store:
        store.set_datachange("news.flash", "ready")
    flush()
    assert paths_and_values(ping(app, page1, cookie)) == [("news.flash", "ready")]
    assert paths_and_values(ping(app, page2, cookie)) == [("news.flash", "ready")]
    # each tab drained its own copy: nothing comes back to either
    assert datachanges(ping(app, page1, cookie)) == []
    assert datachanges(ping(app, page2, cookie)) == []


def test_page_store_set_datachange_delivered_like_batch_thermo(app, register, flush):
    page_id, cookie = open_page(app)
    with register.pageStore(page_id) as store:
        store.set_datachange("gnr.batch.thermo", {"progress": 50})
    flush()
    changes = datachanges(ping(app, page_id, cookie))
    assert [path for path, _, _ in changes] == ["gnr.batch.thermo"]
    assert datachanges(ping(app, page_id, cookie)) == []


def test_chat_fired_write_echoes_to_sender_and_recipient(app, register, flush):
    """The ct_send_message replay: the daemon envelope contract, whole.

    Chat builds everything on the one-shot write: ``setInClientData(...,
    fired=True)`` on each participant's user store. The envelope must carry
    (a) ``fired=True`` verbatim to sender AND recipient — the sender's copy IS
    the echo — and (b) ONLY the explicitly written leaf: the autocreated
    parent the legacy capture records never travels (the daemon built changes
    from the write's arguments, so no parent ever existed in its envelope).
    """
    from gnr.core.gnrbag import Bag

    page1, cookie1 = open_page(app)
    page2, cookie2 = open_page(app)  # separate jar: second connection
    participants = []
    for page_id, user in ((page1, "alice.chat"), (page2, "bob.chat")):
        connection_id = register.page(page_id)["connection_id"]
        with call_sink(app.lane.worker):
            register.change_connection_user(connection_id, user=user, user_name=user.title())
        register.setStoreSubscription(page_id, "user", "gnr.chat.msg", True)
        participants.append(user)
    # alice sends "ciao" to room1: one fired write per participant's store
    for user, in_out in zip(participants, ("out", "in"), strict=True):
        with register.userStore(user) as store:
            store.set_datachange(
                "gnr.chat.msg.room1",
                Bag(dict(msg="ciao", roomId="room1", from_user="alice.chat", in_out=in_out)),
                fired=True, reason="chat_out",
            )
    flush()
    for page_id, cookie, in_out in ((page1, cookie1, "out"), (page2, cookie2, "in")):
        changes = datachanges(ping(app, page_id, cookie))
        assert len(changes) == 1  # the leaf alone: no autocreate in the envelope
        path, value, attr = changes[0]
        assert path == "gnr.chat.msg.room1"
        assert attr["change_fired"] is True
        assert attr["change_reason"] == "chat_out"
        assert value["msg"] == "ciao"
        assert value["in_out"] == in_out
        # one-shot: delivered once, nothing on the next pull
        assert datachanges(ping(app, page_id, cookie)) == []


def test_register_is_served_by_the_worker_machinery(app, register, flush):
    """Guard: the register state lives in the worker (registers + index), nowhere else."""
    page_id, cookie = open_page(app)
    table = unique_table()
    register.subscribeTable(page_id, table=table, subscribe=True)
    worker = app.lane.worker
    # the subscription lives on the page row itself (the index is the desk's)
    assert table in worker.page_items.get(page_id)["table_subscriptions"]
    register.notifyDbEvents(
        {table: [{"dbevent": "I", "pkey": "G1"}]},
        register_name="page", origin_page_id=page_id, dbevent_reason="guard",
    )
    flush()
    # exactly one copy, delivered from the page's own pending list through the ping
    changes = datachanges(ping(app, page_id, cookie))
    assert len(changes) == 1
    assert changes[0][0] == "gnr.dbchanges." + table.replace(".", "_")
    # an unserved command is an explicit error, not a silent daemon fallback
    with pytest.raises(AttributeError):
        register.someUnknownRegisterCommand("p1")
