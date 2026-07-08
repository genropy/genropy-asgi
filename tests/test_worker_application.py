# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyWorkerApplication — the pool-child rail, driven in-process.

A real GnrWsgiSite hosted by the pool-child role: the register commands produced while
serving must ride OUT (the piggyback header / the outbox), never fold into a local
surface — the child has none. The commander is not running here: the pull is empty by
contract (no GENRO_COMMANDER_URL), and the ascending events are read straight from the
response header the synchronous rail writes.
"""

import importlib.util
import json

import pytest

from tests.test_legacy_e2e import fire, ping

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")


@pytest.fixture(scope="module")
def worker_app(request):
    from genro_asgi.applications.spa_application import LIFECYCLE_HEADER  # noqa: F401

    from genropy_asgi.spa import GenropyWorkerApplication

    try:
        application = GenropyWorkerApplication(
            source=_SITE, debug=False, worker_name="pool_test"
        )
    except Exception as exc:
        pytest.skip(f"cannot build the {_SITE} site: {exc}")
    yield application
    application.on_shutdown()


def lifecycle_events(response):
    from genro_asgi.applications.spa_application import LIFECYCLE_HEADER

    for name, value in response["headers"]:
        if name == LIFECYCLE_HEADER:
            return json.loads(value.decode("latin-1"))
    return []


def test_is_a_pool_child_hosting_the_site(worker_app):
    assert worker_app.worker.name == "pool_test"
    assert worker_app.gnr_site is not None
    # a pool child holds no surface and no mailbox: those are the commander's
    assert not hasattr(worker_app, "app_registry")
    assert not hasattr(worker_app, "mailbox")


def test_lifecycle_events_ride_the_piggyback_header(worker_app):
    response = fire(worker_app, "GET", "/")
    assert response["status"] == 200
    events = lifecycle_events(response)
    ops = [event["op"] for event in events]
    assert "new_connection" in ops
    assert "new_page" in ops
    # shaped for the commander: worker name and per-worker seq on every event
    assert all(event["worker"] == "pool_test" for event in events)
    assert all(event.get("seq") for event in events)


def test_ping_pulls_nothing_without_a_commander(worker_app, monkeypatch):
    monkeypatch.delenv("GENRO_COMMANDER_URL", raising=False)
    response = fire(worker_app, "GET", "/")
    page_id = None
    import re

    match = re.search(r"page_id:'([\w-]+)'", response["body"].decode(errors="replace"))
    assert match
    page_id = match.group(1)
    envelope = ping(worker_app, page_id, None)
    # the page is alive (local registries answer the refresh); no queues to deliver
    assert envelope["result"] is None
    assert envelope["dataChanges"] is None
