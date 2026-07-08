# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Tests for GenropyProxyMixin — the GenroPy legacy db behind an OpenApiApplication.

The structural/unit tests use a fake GnrApp (an external dependency stub) and run
always: they verify the hooks the mixin owns — ``route_cleanup`` closes the db
connection, ``gnr_app`` exposes the hosted app, and the MRO is correct.

The end-to-end test REQUIRES GenroPy installed and the ``test_invoice_pg``
instance available; it is skipped otherwise. It mounts the proxy on a worker with
a minimal RoutingClass that reads ``parent.gnr_app`` and drives a real request,
checking the db connection is closed by ``route_cleanup``.
"""

import importlib.util

import pytest
from genro_routes import RoutingClass, route

from genro_asgi.applications.openapi_application import OpenApiApplication
from genropy_asgi.proxy import GenropyProxyMixin, GenropyProxyOpenApiApplication

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_INSTANCE = "test_invoice_pg"


class _FakeDb:
    """Stub of GnrApp.db: records how many times closeConnection was called."""

    def __init__(self):
        self.closed = 0

    def closeConnection(self):
        self.closed += 1


class _FakeGnrApp:
    """Stub of GnrApp: exposes a db with closeConnection, nothing else."""

    def __init__(self):
        self.db = _FakeDb()


@pytest.fixture
def proxy(monkeypatch):
    """A GenropyProxyOpenApiApplication whose GnrApp is a stub.

    Patches gnr.app.gnrapp.GnrApp — the external dependency — so no real GenroPy
    instance is needed. Everything else is the real genro-asgi machinery.
    """
    import sys
    import types

    fake_module = types.ModuleType("gnr.app.gnrapp")
    fake_module.GnrApp = lambda instance, debug=False: _FakeGnrApp()
    monkeypatch.setitem(sys.modules, "gnr", types.ModuleType("gnr"))
    monkeypatch.setitem(sys.modules, "gnr.app", types.ModuleType("gnr.app"))
    monkeypatch.setitem(sys.modules, "gnr.app.gnrapp", fake_module)
    return GenropyProxyOpenApiApplication(instance=_INSTANCE)


def test_is_openapi_application_subclass():
    assert issubclass(GenropyProxyOpenApiApplication, OpenApiApplication)


def test_mixin_comes_before_base_in_mro():
    mro = GenropyProxyOpenApiApplication.__mro__
    assert mro.index(GenropyProxyMixin) < mro.index(OpenApiApplication)


def test_requires_an_instance(monkeypatch):
    import sys
    import types

    fake_module = types.ModuleType("gnr.app.gnrapp")
    fake_module.GnrApp = lambda instance, debug=False: _FakeGnrApp()
    monkeypatch.setitem(sys.modules, "gnr", types.ModuleType("gnr"))
    monkeypatch.setitem(sys.modules, "gnr.app", types.ModuleType("gnr.app"))
    monkeypatch.setitem(sys.modules, "gnr.app.gnrapp", fake_module)
    with pytest.raises(ValueError):
        GenropyProxyOpenApiApplication()


def test_gnr_app_is_exposed(proxy):
    assert isinstance(proxy.gnr_app, _FakeGnrApp)


def test_route_cleanup_closes_connection(proxy):
    assert proxy.gnr_app.db.closed == 0
    proxy.route_cleanup()
    assert proxy.gnr_app.db.closed == 1


def test_on_shutdown_closes_connection(proxy):
    proxy.on_shutdown()
    assert proxy.gnr_app.db.closed == 1


class _DemoApi(RoutingClass):
    """Minimal RoutingClass that reaches the GnrApp through its parent."""

    def __init__(self, application):
        self.application = application

    @route()
    def whoami(self):
        return type(self.application.gnr_app).__name__


@pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")
def test_e2e_real_gnrapp_closes_connection():
    from genro_asgi import GenroAsgiWorker

    api = _DemoApi(None)
    app = GenropyProxyOpenApiApplication(instance=_INSTANCE, routing_class=api)
    api.application = app  # the demo api reads parent.gnr_app
    assert app.gnr_app is not None
    assert app.gnr_app.db is not None

    worker = GenroAsgiWorker(app, host="127.0.0.1", port=8000)
    received = _fire_get(worker, "/api/whoami")
    assert received["status"] == 200
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
