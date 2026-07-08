# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""End-to-end datachange scenarios against a real GnrWsgiSite (the daemonless baseline).

These REQUIRE GenroPy, the ``test_invoice_pg`` site and a reachable register daemon
(``gnr web daemon``; point both sides with ``GNR_DAEMON_PORT``). They skip cleanly when
any piece is missing. The asserts encode the register semantics observed on the daemon
rail (envelope shape, destructive collect, offsets/dedup, ``_new_datachange``): the same
scenarios must stay green when the register goes daemonless — this suite is the golden
reference, not a byte-compare.

Scenario coverage:
- page open -> connection cookie minted, ``page_id`` in the bootstrap HTML
- ping -> empty envelope when no changes are pending
- subscribeTable + notifyDbEvents -> delivered once on ping (collect is destructive),
  origin page NOT excluded (legacy semantics)
- real db write -> commit -> onDbCommitted -> notifyDbEvents -> delivered on ping
- user-store: setStoreSubscription + userStore().set_datachange -> delivered with
  ``_new_datachange`` on the first pull, deduped on later pulls (per-page offset)
- second tab (same user): the user change reaches it too, without ``_new_datachange``
  (global per-user offset marks the first consumer only)
- pageStore().set_datachange (the batch/thermo write) -> delivered on ping
"""

import asyncio
import importlib.util
import re
import uuid

import pytest

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")


@pytest.fixture(scope="module")
def app():
    """One real GnrWsgiSite for the whole module; skip if the register daemon is down."""
    from genropy_asgi.spa import GenropySpaApplication

    try:
        application = GenropySpaApplication(source=_SITE, debug=False)
    except Exception as exc:  # daemon down or site broken: skip, don't fail
        pytest.skip(f"cannot build the {_SITE} site: {exc}")
    yield application
    application.on_shutdown()


@pytest.fixture()
def register(app):
    return app.gnr_site.register


def fire(app, method, path, query=b"", cookies=None, body=b""):
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

    asyncio.run(app(scope, receive, send))
    return received


def open_page(app, cookies=None):
    """GET the root page; return (page_id, connection_cookie)."""
    r = fire(app, "GET", "/", cookies=cookies)
    assert r["status"] == 200
    match = re.search(r"page_id:'([\w-]+)'", r["body"].decode(errors="replace"))
    assert match, "no page_id in the bootstrap HTML"
    cookie = cookies
    for name, value in r["headers"]:
        if name == b"set-cookie":
            cookie = value.decode().split(";")[0]
    return match.group(1), cookie


def ping(app, page_id, cookies):
    """GET /_ping for the page; return the envelope as a legacy Bag."""
    from gnr.core.gnrbag import Bag

    r = fire(app, "GET", "/_ping", query=f"page_id={page_id}".encode(), cookies=cookies)
    assert r["status"] == 200
    return Bag(r["body"].decode(errors="replace"))


def datachanges(envelope):
    """The dataChanges nodes of a ping envelope as a list of (path, value, attributes)."""
    changes = envelope["dataChanges"]
    if changes is None:
        return []
    return [(node.attr.get("change_path"), node.value, node.attr) for node in changes]


def unique_table():
    return f"probe.t{uuid.uuid4().hex[:8]}"


def test_page_open_mints_connection_cookie_and_page(app, register):
    page_id, cookie = open_page(app)
    assert cookie and cookie.startswith(_SITE + "=")
    page_item = register.page(page_id)
    assert page_item is not None
    assert page_item["register_item_id"] == page_id


def test_ping_with_no_changes_returns_empty_envelope(app):
    page_id, cookie = open_page(app)
    envelope = ping(app, page_id, cookie)
    assert datachanges(envelope) == []


def test_db_event_reaches_subscribed_page_once_including_origin(app, register):
    page_id, cookie = open_page(app)
    table = unique_table()
    register.subscribeTable(page_id, table=table, subscribe=True)
    register.notifyDbEvents(
        {table: [{"dbevent": "U", "pkey": "K1"}]},
        register_name="page", origin_page_id=page_id, dbevent_reason="probe",
    )
    changes = datachanges(ping(app, page_id, cookie))
    # delivered even though this page IS the origin (legacy does not exclude it)
    assert len(changes) == 1
    path, value, attr = changes[0]
    assert path == "gnr.dbchanges." + table.replace(".", "_")
    assert attr["change_attr"]["from_page_id"] == page_id
    # the collect is destructive: nothing on the next ping
    assert datachanges(ping(app, page_id, cookie)) == []


def test_real_db_commit_notifies_subscribed_page(app, register):
    page_id, cookie = open_page(app)
    table = "invc.customer_type"
    register.subscribeTable(page_id, table=table, subscribe=True)
    db = app.gnr_site.db
    code = uuid.uuid4().hex[:5]
    tbl = db.table(table)
    tbl.insert({"code": code, "description": "e2e probe"})
    db.commit()
    changes = datachanges(ping(app, page_id, cookie))
    assert any(path == "gnr.dbchanges.invc_customer_type" for path, _, _ in changes)
    # clean up the record and drain the resulting event
    tbl.delete({"code": code})
    db.commit()
    ping(app, page_id, cookie)


def test_user_store_change_delivered_then_deduped(app, register):
    page_id, cookie = open_page(app)
    user = register.page(page_id)["user"]
    register.setStoreSubscription(page_id, "user", "chat", True)
    with register.userStore(user) as store:
        store.set_datachange("chat.msg", "hello")
    changes = datachanges(ping(app, page_id, cookie))
    assert [(path, value) for path, value, _ in changes] == [("chat.msg", "hello")]
    assert changes[0][2]["change_attr"]["_new_datachange"] is True
    # per-page offset: the same change never comes back on later pulls
    assert datachanges(ping(app, page_id, cookie)) == []


def test_user_store_change_reaches_second_tab_without_new_flag(app, register):
    page1, cookie = open_page(app)
    page2, cookie = open_page(app, cookies=cookie)  # same connection, same user
    user = register.page(page1)["user"]
    assert register.page(page2)["user"] == user
    register.setStoreSubscription(page1, "user", "news", True)
    register.setStoreSubscription(page2, "user", "news", True)
    with register.userStore(user) as store:
        store.set_datachange("news.flash", "ready")
    first = datachanges(ping(app, page1, cookie))
    second = datachanges(ping(app, page2, cookie))
    assert [(p, v) for p, v, _ in first] == [("news.flash", "ready")]
    assert [(p, v) for p, v, _ in second] == [("news.flash", "ready")]
    # the global per-user offset marks only the first consumer as "new"
    assert first[0][2]["change_attr"]["_new_datachange"] is True
    assert "_new_datachange" not in (second[0][2]["change_attr"] or {})


def test_page_store_set_datachange_delivered_like_batch_thermo(app, register):
    page_id, cookie = open_page(app)
    with register.pageStore(page_id) as store:
        store.set_datachange("gnr.batch.thermo", {"progress": 50})
    changes = datachanges(ping(app, page_id, cookie))
    assert [path for path, _, _ in changes] == ["gnr.batch.thermo"]
    assert datachanges(ping(app, page_id, cookie)) == []


def test_register_is_served_by_the_application_machinery(app, register):
    """Guard: the register state lives in the app (surface + mailbox), nowhere else."""
    page_id, cookie = open_page(app)
    table = unique_table()
    register.subscribeTable(page_id, table=table, subscribe=True)
    assert page_id in app.app_registry.pages_subscribing(table)
    register.notifyDbEvents(
        {table: [{"dbevent": "I", "pkey": "G1"}]},
        register_name="page", origin_page_id=page_id, dbevent_reason="guard",
    )
    # exactly one copy, delivered from the mailbox through the ping
    changes = datachanges(ping(app, page_id, cookie))
    assert len(changes) == 1
    assert changes[0][0] == "gnr.dbchanges." + table.replace(".", "_")
    # an unserved command is an explicit error, not a silent daemon fallback
    with pytest.raises(NotImplementedError):
        register.someUnknownRegisterCommand("p1")
