# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyWorkerApplication — the pool-child rail, driven in-process.

A real GnrWsgiSite hosted by the pool-child role: the register commands produced while
serving ride OUT on the pool channel — every shaped event queues in the worker's outbox
(the channel sender drains it onto ``/events``), never folding into a local surface —
the child has none. No commander runs here, so the outbox is observed directly and the
response is checked for the client-facing header the child now mints (the ``sticky_cid``
birth cookie). The datachange read is LOCAL (switch model): with nothing deposited the
ping envelope carries no changes.
"""

import asyncio
import importlib.util
import re

import pytest

from tests.test_legacy_e2e import fire, ping

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")


@pytest.fixture(scope="module")
def worker_app(request):
    from genropy_asgi.spa import GenropyWorkerApplication

    try:
        application = GenropyWorkerApplication(
            source=_SITE, debug=False, worker_name="pool_test"
        )
    except Exception as exc:
        pytest.skip(f"cannot build the {_SITE} site: {exc}")
    yield application
    asyncio.run(application.on_shutdown())


def test_is_a_pool_child_hosting_the_site(worker_app):
    assert worker_app.worker.name == "pool_test"
    assert worker_app.gnr_site is not None
    # a pool child holds no surface: the global picture is the commander's
    assert not hasattr(worker_app, "app_registry")


def test_lifecycle_events_queue_on_the_channel_outbox(worker_app):
    from genro_asgi.applications.spa_application import STICKY_CID_COOKIE

    response = fire(worker_app, "GET", "/")
    assert response["status"] == 200
    # the shaped events wait in the outbox for the channel sender (nothing acked here)
    events = worker_app.worker.outbox.drain()
    ops = [event["op"] for event in events]
    assert "new_connection" in ops
    assert "new_page" in ops
    # shaped for the commander: worker name and per-worker seq on every event
    assert all(event["worker"] == "pool_test" for event in events)
    assert all(event.get("seq") for event in events)
    # the child mints the client-facing birth cookie for the connection born here
    cookies = [value for name, value in response["headers"] if name == b"set-cookie"]
    assert any(
        value.decode("latin-1").startswith(f"{STICKY_CID_COOKIE}=") for value in cookies
    )


def test_ping_reads_the_local_queue_which_is_empty(worker_app):
    response = fire(worker_app, "GET", "/")
    match = re.search(r"page_id:'([\w-]+)'", response["body"].decode(errors="replace"))
    assert match
    page_id = match.group(1)
    envelope = ping(worker_app, page_id, None)
    # the page is alive (local registries answer the refresh); the pending list is
    # local and empty — nothing was deposited, no commander is asked for anything
    assert envelope["result"] is None
    assert envelope["dataChanges"] is None
