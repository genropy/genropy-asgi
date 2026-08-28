# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Tests for GenropySpaApplication — the GenroPy front on the core SpaApplication.

The structural half (subclassing, the root mount, the startup source check)
needs no GenroPy: the front holds no pool until the server starts. The BOOT
half builds the shipped ``config.py`` recipe — the one ``gnrasgiserve``
boots — all the way to an ``AsgiServer``, stopping short of ``serve()``: the
pool is recipe words now, so a recipe the grammar rejects is a server that
never starts, and the words must land where the core reads them
(``commander_kwargs``/``group_kwargs``). The end-to-end half starts the REAL
pool — the commander spawns one worker subprocess hosting ``test_invoice_pg``
— and drives the front's own ASGI callable: the demux serves ``/metrics``
natively and forwards the site paths, and the first answer carries the
connection the site named as the routing cookie.
"""

import importlib.util
import shutil
import tempfile

import pytest

from genro_asgi import AsgiServer
from genro_asgi.applications.spa_app import SPA_CONNECTION_ID_COOKIE, SpaApplication
from genro_asgi.config import AsgiConfigBuilder

from genropy_asgi.spa import GenropySpaApplication
from genropy_asgi.spa.cli import CONFIG

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"


# ------------------------------------------------------------------
# Structure: the subclass, the root mount, the startup source check
# ------------------------------------------------------------------


def test_is_spa_application_new_subclass():
    assert issubclass(GenropySpaApplication, SpaApplication)


def test_a_direct_instantiation_lands_on_the_site_root():
    # without a class-level mount the core would mount the app under its own
    # code — a GenroPy site there answers the first page and 404s every path
    # that page asks for
    assert GenropySpaApplication().mount == ""
    # an explicit empty mount (the shipped recipe writes one) only confirms it,
    # and so does the None a generic caller may pass
    assert GenropySpaApplication(mount="").mount == ""
    assert GenropySpaApplication(mount=None).mount == ""


def test_a_non_empty_mount_is_refused():
    with pytest.raises(ValueError):
        GenropySpaApplication(mount="admin")


class _SourcelessRecipe(AsgiConfigBuilder):
    """A pool whose group names no site: the startup check must refuse it."""

    def main(self, root):
        cfg = root.configuration()
        cfg.server(host="127.0.0.1", port=8972)
        front = cfg.applications().application(
            code="site", mount="", app_class=GenropySpaApplication
        )
        commander = front.commander(frozen_users_path="/tmp/gnr_probe_frozen")
        commander.groups().group(name="pool", entry_module="never.launched")


async def test_startup_refuses_a_group_without_a_source():
    server = AsgiServer(config=_SourcelessRecipe)
    front = server.applications["site"]
    with pytest.raises(ValueError, match="names no source"):
        await front.on_startup()


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
    monkeypatch.setenv("GNR_ASGI_IDLE_FREEZE_MINUTES", "45")
    monkeypatch.delenv("GNR_ASGI_FROZEN_USERS_PATH", raising=False)
    monkeypatch.delenv("GNR_ASGI_INSTANCE_DIR", raising=False)
    monkeypatch.delenv("GNR_ASGI_WORKERS", raising=False)
    monkeypatch.delenv("GNR_ASGI_WORKER_MAX_USERS", raising=False)
    monkeypatch.delenv("GNR_ASGI_DEBUGGER", raising=False)
    monkeypatch.delenv("GNR_ASGI_ORCHESTRATION_PROFILES", raising=False)
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


def test_the_recipe_declares_the_pool_where_the_core_reads_it(booted):
    # the pool is recipe words: the core's own config door must hand them back
    commander_kwargs = booted.config.commander_kwargs("site")
    assert commander_kwargs["frozen_users_path"] == (
        "/tmp/genropy_asgi_recipe_probe/data/_frozen_users"
    )
    groups = booted.config.group_kwargs("site")
    assert set(groups) == {"pool"}
    pool = groups["pool"]
    assert pool["entry_module"] == "genro_asgi.spa.orchestration.worker_entry"
    assert pool["worker_class"] == "genropy_asgi.spa.genropy_worker:GenropyWorker"
    assert pool["instance_dir"]  # the sockets root travels to the group
    worker_kwargs = pool["worker_kwargs"]
    assert worker_kwargs["source"] == "/tmp/genropy_asgi_recipe_probe"
    assert worker_kwargs["debug"] is False  # --nodebug writes an empty GNR_ASGI_DEBUG
    # the env-driven valve is a GROUP setting: the group reads the clocks and
    # decides who sleeps, the worker keeps no gauge of its own (wf/41)
    assert pool["user_idle_freeze_minutes"] == 45.0


@pytest.mark.parametrize(
    "value,expected",
    [("", False), ("0", False), ("false", False), ("False", False), ("no", False),
     ("off", False), ("1", True), ("true", True), ("yes", True)],
)
def test_the_debug_flag_is_read_as_a_word_not_as_a_truthy_string(monkeypatch, value, expected):
    # a truthy-string read would turn GNR_ASGI_DEBUG=false into debug ON: the
    # Werkzeug debugger around the site, and the site's SQL time counters
    # (incremented only under debug) suddenly filled — a measured run would be
    # measuring another system
    monkeypatch.setenv("GNR_ASGI_PATH", "/tmp/genropy_asgi_recipe_probe")
    monkeypatch.setenv("GNR_ASGI_DEBUG", value)
    server = AsgiServer(str(CONFIG))
    pool = server.config.group_kwargs("site")["pool"]
    assert pool["worker_kwargs"]["debug"] is expected


def test_the_debug_flag_unset_stays_the_dev_default(monkeypatch):
    monkeypatch.setenv("GNR_ASGI_PATH", "/tmp/genropy_asgi_recipe_probe")
    monkeypatch.delenv("GNR_ASGI_DEBUG", raising=False)
    server = AsgiServer(str(CONFIG))
    assert server.config.group_kwargs("site")["pool"]["worker_kwargs"]["debug"] is True


def test_the_werkzeug_debugger_is_off_until_it_is_asked_for_by_name(booted):
    # its error page evaluates Python in the process, so it must never come on
    # as a side effect of the flag somebody set to get the SQL counters
    assert booted.config.group_kwargs("site")["pool"]["worker_kwargs"]["debugger"] is False


def test_the_debugger_travels_beside_debug_when_asked(monkeypatch):
    monkeypatch.setenv("GNR_ASGI_PATH", "/tmp/genropy_asgi_recipe_probe")
    monkeypatch.setenv("GNR_ASGI_DEBUG", "1")
    monkeypatch.setenv("GNR_ASGI_DEBUGGER", "1")
    worker_kwargs = AsgiServer(str(CONFIG)).config.group_kwargs("site")["pool"]["worker_kwargs"]
    assert (worker_kwargs["debug"], worker_kwargs["debugger"]) == (True, True)


def test_debug_alone_does_not_bring_the_debugger(monkeypatch):
    # the pair the bench needs: real SQL counters, no interactive error page
    monkeypatch.setenv("GNR_ASGI_PATH", "/tmp/genropy_asgi_recipe_probe")
    monkeypatch.setenv("GNR_ASGI_DEBUG", "1")
    monkeypatch.delenv("GNR_ASGI_DEBUGGER", raising=False)
    worker_kwargs = AsgiServer(str(CONFIG)).config.group_kwargs("site")["pool"]["worker_kwargs"]
    assert (worker_kwargs["debug"], worker_kwargs["debugger"]) == (True, False)


def test_the_user_ceiling_is_absent_until_somebody_declares_one(booted):
    # unset means the core's own default governs: a worker takes everybody, and
    # the recipe must not put a number of its own in front of that decision
    assert "worker_max_users" not in booted.config.group_kwargs("site")["pool"]


def test_the_user_ceiling_reaches_the_group_as_a_number(monkeypatch):
    # the bench sets it to 1 so each user lands on a worker of his own, which is
    # what exercises the cross-worker paths at all
    monkeypatch.setenv("GNR_ASGI_PATH", "/tmp/genropy_asgi_recipe_probe")
    monkeypatch.setenv("GNR_ASGI_WORKER_MAX_USERS", "1")
    server = AsgiServer(str(CONFIG))
    assert server.config.group_kwargs("site")["pool"]["worker_max_users"] == 1


def test_a_leftover_workers_variable_changes_nothing(monkeypatch):
    # the single/pool selector is gone: the pool always runs and sizes itself
    monkeypatch.setenv("GNR_ASGI_PATH", "/tmp/genropy_asgi_recipe_probe")
    monkeypatch.setenv("GNR_ASGI_WORKERS", "2")
    server = AsgiServer(str(CONFIG))
    assert set(server.config.group_kwargs("site")) == {"pool"}


# ------------------------------------------------------------------
# End to end: the real pool behind the demux
# ------------------------------------------------------------------


@pytest.fixture()
async def app():
    """The front started on the REAL pool: one spawned worker hosting the site.

    Mounted on an ``AsgiServer`` built from the shipped recipe — native
    dispatch requires the owning server — and driven directly through the
    app's own ASGI callable. The paths the pool needs (freezer, sockets) go
    to a short-lived temp root.
    """
    if not _HAS_GNR:
        pytest.skip("GenroPy not installed")
    import os

    from gnr.app.pathresolver import PathResolver

    try:
        site_path = PathResolver().site_name_to_path(_SITE)
    except Exception as exc:
        pytest.skip(f"cannot resolve the {_SITE} site: {exc}")
    root = tempfile.mkdtemp(prefix="gnrspa_")
    os.environ["GNR_ASGI_PATH"] = site_path
    os.environ["GNR_ASGI_DEBUG"] = ""
    os.environ["GNR_ASGI_FROZEN_USERS_PATH"] = f"{root}/frozen"
    os.environ["GNR_ASGI_INSTANCE_DIR"] = f"{root}/i"
    server = AsgiServer(str(CONFIG))
    front = server.application_at("")
    try:
        await front.on_startup()
    except Exception as exc:  # site missing or broken: skip, don't fail
        pytest.skip(f"cannot start the {_SITE} pool: {exc}")
    # Readiness: the reception may still be presenting itself on the wire;
    # a refusal at boot is the pool's polite 503, so the fixture retries the
    # first page until the child serves it (the cli e2e convention).
    import asyncio

    for _ in range(60):
        try:
            received = await fire(front, "/")
        except TimeoutError:
            received = {"status": None}
        if received["status"] == 200:
            break
        await asyncio.sleep(0.5)
    yield front
    await front.on_shutdown()
    shutil.rmtree(root, ignore_errors=True)


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


async def test_the_first_answer_carries_the_connection_the_site_named(app):
    """The site creates its connection while serving, and that id — nothing
    minted by the front — is what the routing cookie is written with."""
    received = await fire(app, "/")
    cookies = header_values(received, "set-cookie")
    assert any(SPA_CONNECTION_ID_COOKIE in cookie for cookie in cookies)


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
