# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Tests for GenropySpaApplication — the GenroPy legacy bridge over SpaApplication.

These REQUIRE GenroPy installed and the ``test_invoice_pg`` site available; they are
skipped otherwise. The end-to-end test drives a real GnrWsgiSite through the full ASGI
stack (GenroAsgiWorker -> SpaApplication.serve_app -> WSGI) and checks a 200 response.

The structural tests (subclassing, source classification) need no GenroPy.
"""

import importlib.util

import pytest

from genro_asgi.applications.spa_application import SpaApplication
from genropy_asgi.spa import GenropySpaApplication

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"


def test_is_spa_application_subclass():
    assert issubclass(GenropySpaApplication, SpaApplication)


def test_serve_app_is_overridden():
    assert GenropySpaApplication.serve_app is not SpaApplication.serve_app


def test_requires_a_source():
    with pytest.raises(ValueError):
        GenropySpaApplication()


@pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")
def test_creates_real_gnr_site_and_serves_200():
    from genro_asgi import GenroAsgiWorker

    app = GenropySpaApplication(source=_SITE, debug=False)
    assert app.source_kind == "path"
    assert app.gnr_site is not None
    # the lifecycle registries are wired by the single-worker base (the global store
    # is a GlobalStore on the app — you have it because you are a worker/commander)
    rh = app.registry_handler
    assert all(hasattr(rh, r) for r in ("users", "connections", "pages"))
    assert app.global_store.keys() == []

    worker = GenroAsgiWorker(app, host="127.0.0.1", port=8000)
    received = _fire_get(worker, "/")
    assert received["status"] == 200
    assert len(received["body"]) > 0
    app.on_shutdown()


@pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")
def test_global_store_is_served_in_process_with_the_legacy_api():
    app = GenropySpaApplication(source=_SITE, debug=False)
    register = app.gnr_site.register
    # write through the legacy context-manager API; read it straight back — no daemon
    with register.globalStore() as gs:
        gs.setItem("CACHE_TS.orders", 42)
    assert register.globalStore().getItem("CACHE_TS.orders") == 42
    # write-by-reference: the same in-process Bag backs every globalStore() call
    with register.globalStore() as gs:
        gs.setItem("CACHE_TS.orders", 99)
    assert register.globalStore().getItem("CACHE_TS.orders") == 99
    app.on_shutdown()


def _fire_get(app, path):
    import asyncio

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [],
        "server": ("localhost", 8000),
        "client": ("127.0.0.1", 12345),
        "scheme": "http",
        "http_version": "1.1",
    }
    received = {"status": None, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            received["status"] = message["status"]
        elif message["type"] == "http.response.body":
            received["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return received
