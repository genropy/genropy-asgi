# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""End-to-end datachange scenarios against a real GnrWsgiSite on the NEW single.

These REQUIRE GenroPy and the ``test_invoice_pg`` site; they skip cleanly when
either is missing. No register daemon anywhere: the front is
``GenropySpaApplication`` (the core ``SpaApplication``) holding its one
``GenropyWorker`` in-process — the full protocol on a ``LocalChannel`` — and
every request crosses the demux, the ``http`` CALL forward and the WSGI seam.

The asserts encode the register semantics the daemon rail established: the same
scenarios stayed green across daemon -> daemonless -> core rebase — this suite
is the golden reference, not a byte-compare. The envelope keeps the daemon's
contract whole: only the explicitly written leaves travel (the autocreated
parents the legacy capture records stay internal), each with its ``fired``
flag verbatim, delivered once — the drain is destructive.

Scenario coverage:
- page open -> the site's connection cookie AND the front's sticky_cid minted,
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
    """Open the sinks a CALL would open (the core's own test convention)."""
    events_token = worker._call_events.set([])
    tasks_token = worker._call_tasks.set([])
    try:
        yield
    finally:
        worker._call_events.reset(events_token)
        worker._call_tasks.reset(tasks_token)


@pytest.fixture()
async def app():
    """One real single per test: the front + its in-process GenropyWorker."""
    from genro_asgi import AsgiServer

    from genropy_asgi.spa import GenropySpaApplication

    front = GenropySpaApplication(source=_SITE, debug=False, workers=0, local_worker=True)
    server = AsgiServer(applications=[front])  # native dispatch needs the owner
    assert front.server is server
    try:
        await front.on_startup()
    except Exception as exc:  # site missing or broken: skip, don't fail
        pytest.skip(f"cannot start the {_SITE} single: {exc}")
    yield front
    await front.on_shutdown()


@pytest.fixture()
def register(app):
    return app.gnr_site.register


async def fire(app, method, path, query=b"", cookies=None, body=b""):
    """Drive one request through the full ASGI stack, in process."""
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

    await app(scope, receive, send)
    return received


def merge_cookies(received, cookies=None):
    """Fold the response's set-cookie headers into the request cookie string.

    The new single answers with TWO cookies — the site's own (named after the
    site) and the front's ``sticky_cid`` — and the client must present both:
    the site cookie is the legacy session, the sticky one is the routing key.
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


async def open_page(app, cookies=None):
    """GET the root page; return (page_id, cookie_jar_string)."""
    received = await fire(app, "GET", "/", cookies=cookies)
    assert received["status"] == 200
    match = re.search(r"page_id:'([\w-]+)'", received["body"].decode(errors="replace"))
    assert match, "no page_id in the bootstrap HTML"
    return match.group(1), merge_cookies(received, cookies)


async def ping(app, page_id, cookies):
    """GET /_ping for the page; return the envelope as a legacy Bag."""
    from gnr.core.gnrbag import Bag

    received = await fire(
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


async def test_page_open_mints_both_cookies_and_the_page(app, register):
    page_id, cookie = await open_page(app)
    jar = dict(pair.split("=", 1) for pair in cookie.split("; "))
    assert _SITE in jar  # the site's own legacy session cookie
    assert "sticky_cid" in jar  # the front's routing cookie
    page_item = register.page(page_id)
    assert page_item is not None
    assert page_item["register_item_id"] == page_id


async def test_ping_with_no_changes_returns_empty_envelope(app):
    page_id, cookie = await open_page(app)
    envelope = await ping(app, page_id, cookie)
    assert datachanges(envelope) == []


async def test_db_event_reaches_subscribed_page_once_including_origin(app, register):
    page_id, cookie = await open_page(app)
    table = unique_table()
    register.subscribeTable(page_id, table=table, subscribe=True)
    register.notifyDbEvents(
        {table: [{"dbevent": "U", "pkey": "K1"}]},
        register_name="page", origin_page_id=page_id, dbevent_reason="probe",
    )
    changes = datachanges(await ping(app, page_id, cookie))
    # delivered even though this page IS the origin (legacy does not exclude it)
    assert len(changes) == 1
    path, value, attr = changes[0]
    assert path == "gnr.dbchanges." + table.replace(".", "_")
    assert attr["change_attr"]["from_page_id"] == page_id
    # the collect is destructive: nothing on the next ping
    assert datachanges(await ping(app, page_id, cookie)) == []


async def test_real_db_commit_notifies_subscribed_page(app, register):
    page_id, cookie = await open_page(app)
    table = "invc.customer_type"
    register.subscribeTable(page_id, table=table, subscribe=True)
    db = app.gnr_site.db
    code = uuid.uuid4().hex[:5]
    tbl = db.table(table)
    tbl.insert({"code": code, "description": "e2e probe"})
    db.commit()
    changes = datachanges(await ping(app, page_id, cookie))
    assert any(path == "gnr.dbchanges.invc_customer_type" for path, _, _ in changes)
    # clean up the record and drain the resulting event
    tbl.delete({"code": code})
    db.commit()
    await ping(app, page_id, cookie)


async def test_user_store_change_delivered_then_drained(app, register):
    # The core model: the user-store write is a REAL write on the owner's live
    # store, captured by each subscribed page's own user_view — no offsets, no
    # ``_new_datachange`` bookkeeping. The drain is destructive.
    page_id, cookie = await open_page(app)
    user = register.connection(register.page(page_id)["connection_id"])["user"]
    register.setStoreSubscription(page_id, "user", "chat", True)
    with register.userStore(user) as store:
        store.set_datachange("chat.msg", "hello")
    assert paths_and_values(await ping(app, page_id, cookie)) == [("chat.msg", "hello")]
    # destructive drain: the same change never comes back on later pulls
    assert datachanges(await ping(app, page_id, cookie)) == []


async def test_user_store_change_reaches_both_tabs_once_each(app, register):
    page1, cookie = await open_page(app)
    page2, cookie = await open_page(app, cookies=cookie)  # same connection, same user
    connection_id = register.page(page1)["connection_id"]
    assert register.page(page2)["connection_id"] == connection_id
    user = register.connection(connection_id)["user"]
    register.setStoreSubscription(page1, "user", "news", True)
    register.setStoreSubscription(page2, "user", "news", True)
    with register.userStore(user) as store:
        store.set_datachange("news.flash", "ready")
    assert paths_and_values(await ping(app, page1, cookie)) == [("news.flash", "ready")]
    assert paths_and_values(await ping(app, page2, cookie)) == [("news.flash", "ready")]
    # each tab drained its own copy: nothing comes back to either
    assert datachanges(await ping(app, page1, cookie)) == []
    assert datachanges(await ping(app, page2, cookie)) == []


async def test_page_store_set_datachange_delivered_like_batch_thermo(app, register):
    page_id, cookie = await open_page(app)
    with register.pageStore(page_id) as store:
        store.set_datachange("gnr.batch.thermo", {"progress": 50})
    changes = datachanges(await ping(app, page_id, cookie))
    assert [path for path, _, _ in changes] == ["gnr.batch.thermo"]
    assert datachanges(await ping(app, page_id, cookie)) == []


async def test_chat_fired_write_echoes_to_sender_and_recipient(app, register):
    """The ct_send_message replay: the daemon envelope contract, whole.

    Chat builds everything on the one-shot write: ``setInClientData(...,
    fired=True)`` on each participant's user store. The envelope must carry
    (a) ``fired=True`` verbatim to sender AND recipient — the sender's copy IS
    the echo — and (b) ONLY the explicitly written leaf: the autocreated
    parent the legacy capture records never travels (the daemon built changes
    from the write's arguments, so no parent ever existed in its envelope).
    """
    from gnr.core.gnrbag import Bag

    page1, cookie1 = await open_page(app)
    page2, cookie2 = await open_page(app)  # separate jar: second connection
    participants = []
    for page_id, user in ((page1, "alice.chat"), (page2, "bob.chat")):
        connection_id = register.page(page_id)["connection_id"]
        with call_sink(app.commander.worker):
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
    for page_id, cookie, in_out in ((page1, cookie1, "out"), (page2, cookie2, "in")):
        changes = datachanges(await ping(app, page_id, cookie))
        assert len(changes) == 1  # the leaf alone: no autocreate in the envelope
        path, value, attr = changes[0]
        assert path == "gnr.chat.msg.room1"
        assert attr["change_fired"] is True
        assert attr["change_reason"] == "chat_out"
        assert value["msg"] == "ciao"
        assert value["in_out"] == in_out
        # one-shot: delivered once, nothing on the next pull
        assert datachanges(await ping(app, page_id, cookie)) == []


async def test_register_is_served_by_the_worker_machinery(app, register):
    """Guard: the register state lives in the worker (registers + index), nowhere else."""
    page_id, cookie = await open_page(app)
    table = unique_table()
    register.subscribeTable(page_id, table=table, subscribe=True)
    worker = app.commander.worker
    assert page_id in worker.subscriptions.pages_for(table)
    register.notifyDbEvents(
        {table: [{"dbevent": "I", "pkey": "G1"}]},
        register_name="page", origin_page_id=page_id, dbevent_reason="guard",
    )
    # exactly one copy, delivered from the page's own pending list through the ping
    changes = datachanges(await ping(app, page_id, cookie))
    assert len(changes) == 1
    assert changes[0][0] == "gnr.dbchanges." + table.replace(".", "_")
    # an unserved command is an explicit error, not a silent daemon fallback
    with pytest.raises(AttributeError):
        register.someUnknownRegisterCommand("p1")
