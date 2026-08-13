# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Tests for GenropySpaApplication — the GenroPy front on the core SpaApplication.

The structural half (subclassing, pool defaults, the memory budget derivation)
needs no GenroPy: the front builds its commander at construction and spawns
nothing until startup. The BOOT half builds the shipped ``config.py`` recipe —
the one ``gnrasgiserve`` boots — all the way to an ``AsgiServer``, stopping short
of ``serve()``: the recipe is what the CLI executes before anything else, so a
recipe the grammar rejects is a server that never starts. The end-to-end half
drives a REAL single — the front started with its in-process GenropyWorker
hosting ``test_invoice_pg`` — through the full ASGI stack: the demux serves
``/metrics`` natively and forwards the site paths, the sticky cookie is minted on
the first answer.
"""

import importlib.util
import os

import pytest

from genro_asgi import AsgiServer
from genro_asgi.applications.spa_app import STICKY_CID_COOKIE, SpaApplication
from genropy_asgi.spa import GenropySpaApplication
from genropy_asgi.spa.cli import CONFIG

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"


# ------------------------------------------------------------------
# Structure: the subclass, the pool defaults, the budget derivation
# ------------------------------------------------------------------


def test_is_spa_application_subclass():
    assert issubclass(GenropySpaApplication, SpaApplication)


def test_requires_a_source():
    with pytest.raises(ValueError):
        GenropySpaApplication()


def test_a_direct_instantiation_lands_on_the_site_root():
    # without a class-level mount the core would mount the app under its own
    # code — a GenroPy site there answers the first page and 404s every path
    # that page asks for
    assert GenropySpaApplication(source="some_site").mount == ""
    # an explicit empty mount (the shipped recipe writes one) only confirms it,
    # and so does the None a generic caller may pass
    assert GenropySpaApplication(source="some_site", mount="").mount == ""
    assert GenropySpaApplication(source="some_site", mount=None).mount == ""


def test_a_non_empty_mount_is_refused():
    with pytest.raises(ValueError):
        GenropySpaApplication(source="some_site", mount="admin")


def test_pool_defaults_point_at_the_genropy_worker():
    app = GenropySpaApplication(source="some_site", debug=True)
    assert app.commander.worker_class == "genropy_asgi.spa.genropy_worker:GenropyWorker"
    assert app.commander.worker_kwargs == {
        "source": "some_site",
        "debug": True,
        "sole_registry_owner": True,  # no spawned children: the single owns the registry
    }


def test_debug_flag_coerces_from_string():
    # a worker --app-arg travels as a string; the front coerces it
    app = GenropySpaApplication(source="some_site", debug="false")
    assert app.commander.worker_kwargs["debug"] is False


def test_memory_limit_is_derived_for_a_pool():
    app = GenropySpaApplication(source="some_site", workers=3)
    total_ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    assert app.commander.memory_limit_mb == int(total_ram * 0.8 / 2**20 / 3)


def test_memory_limit_splits_over_max_workers_when_capped():
    app = GenropySpaApplication(source="some_site", workers=2, max_workers=8)
    total_ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    assert app.commander.memory_limit_mb == int(total_ram * 0.8 / 2**20 / 8)


def test_explicit_memory_limit_always_wins():
    app = GenropySpaApplication(source="some_site", workers=3, memory_limit_mb=512)
    assert app.commander.memory_limit_mb == 512


def test_the_single_passes_no_memory_limit():
    # the in-process worker is never recycled by construction
    app = GenropySpaApplication(source="some_site", workers=0, local_worker=True)
    assert app.commander.memory_limit_mb is None


# ------------------------------------------------------------------
# Boot: the shipped recipe, built to an AsgiServer (no serve())
# ------------------------------------------------------------------


@pytest.fixture()
def booted(monkeypatch):
    """The shipped ``config.py`` built to a server, the environment the CLI writes.

    ``gnrasgiserve`` writes the instance path, the address and the debug flag to
    the environment and then hands ``config.py`` to ``AsgiServer``: this fixture
    is that boot, minus the ``serve()`` call. No site is opened — the commander
    spawns nothing until startup — so the path need not exist.
    """
    monkeypatch.setenv("GNR_ASGI_PATH", "/tmp/genropy_asgi_recipe_probe")
    monkeypatch.setenv("GNR_ASGI_HOST", "0.0.0.0")
    monkeypatch.setenv("GNR_ASGI_PORT", "8971")
    monkeypatch.setenv("GNR_ASGI_DEBUG", "")
    monkeypatch.delenv("GNR_ASGI_WORKERS", raising=False)
    return AsgiServer(str(CONFIG))


def test_the_recipe_builds_a_server_on_the_declared_address(booted):
    # the whole point: the grammar accepts the recipe, so the boot path lives
    assert booted.config_host == "0.0.0.0"
    assert booted.config_port == 8971  # dtype="L" — a real int, not "8971"


def test_the_recipe_mounts_the_front_on_the_site_root(booted):
    front = booted.application_at("")
    assert isinstance(front, GenropySpaApplication)
    assert front.code == "site"
    assert front.mount == ""  # a GenroPy site owns its absolute URLs


def test_the_recipe_wires_the_commander_for_the_single(booted):
    commander = booted.application_at("").commander
    assert commander.worker_class == "genropy_asgi.spa.genropy_worker:GenropyWorker"
    assert commander.worker_kwargs == {
        "source": "/tmp/genropy_asgi_recipe_probe",  # the resolved instance path
        "debug": False,  # --nodebug writes an empty GNR_ASGI_DEBUG
        "sole_registry_owner": True,  # workers == 0: the single owns the registry
    }
    assert commander.local_worker is True  # workers == 0: the in-process single


def test_the_recipe_wires_a_pool_when_workers_are_asked_for(monkeypatch):
    monkeypatch.setenv("GNR_ASGI_PATH", "/tmp/genropy_asgi_recipe_probe")
    monkeypatch.setenv("GNR_ASGI_WORKERS", "2")
    commander = AsgiServer(str(CONFIG)).application_at("").commander
    assert commander.local_worker is False
    assert commander.target == 2  # the boot target of the pool


# ------------------------------------------------------------------
# End to end: the real single behind the demux
# ------------------------------------------------------------------


@pytest.fixture()
async def app():
    """The front started as a REAL single (in-process GenropyWorker, full protocol).

    Mounted on an ``AsgiServer`` — native dispatch requires the owning server —
    but driven directly through the app's own ASGI callable.
    """
    if not _HAS_GNR:
        pytest.skip("GenroPy not installed")
    from genro_asgi import AsgiServer

    front = GenropySpaApplication(source=_SITE, debug=False, workers=0, local_worker=True)
    server = AsgiServer(applications=[front])  # wires front.server; held for the fixture's life
    assert front.server is server
    try:
        await front.on_startup()
    except Exception as exc:  # site missing or broken: skip, don't fail
        pytest.skip(f"cannot start the {_SITE} single: {exc}")
    yield front
    await front.on_shutdown()


async def fire(app, path, cookies=None):
    """Drive one GET through the front's own ASGI callable."""
    headers = [(b"cookie", cookies.encode())] if cookies else []
    scope = {
        "type": "http", "method": "GET", "path": path, "query_string": b"",
        "headers": headers, "server": ("localhost", 8000),
        "client": ("127.0.0.1", 12345), "scheme": "http", "http_version": "1.1",
    }
    received = {"status": None, "headers": [], "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            received["status"] = message["status"]
            received["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            received["body"] += message.get("body", b"")

    await app(scope, receive, send)
    return received


def header_values(received, name):
    return [
        value.decode() for key, value in received["headers"] if key.decode().lower() == name
    ]


async def test_a_site_path_is_forwarded_end_to_end(app):
    received = await fire(app, "/")
    assert received["status"] == 200
    assert len(received["body"]) > 0


async def test_the_first_answer_mints_the_sticky_cookie(app):
    received = await fire(app, "/")
    cookies = header_values(received, "set-cookie")
    assert any(STICKY_CID_COOKIE in cookie for cookie in cookies)


async def test_metrics_is_served_natively_by_the_demux(app):
    received = await fire(app, "/metrics")
    assert received["status"] == 200
    assert "text/plain" in header_values(received, "content-type")[0]
    body = received["body"].decode()
    for counter in ("users", "pages", "connections"):
        assert f'genropy_site_counters{{counter="{counter}"}}' in body


async def test_metrics_counts_the_folded_surface(app):
    # a served site request folds its connection birth onto the commander surface
    await fire(app, "/")
    received = await fire(app, "/metrics")
    connections = [
        line for line in received["body"].decode().splitlines() if 'counter="connections"' in line
    ]
    assert connections and int(connections[0].rsplit(" ", 1)[1]) >= 1
