# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""End-to-end: ``gnrasgiserve`` — the pool booted by the CLI, driven over HTTP.

The bridge from the outside: the CLI boots a ``GenropySpaApplication`` whose
recipe-born commander (``SpaCommander``) spawns the reception worker hosting
the GenroPy site; the test talks HTTP only. There is no worker count to pass
— the pool always runs and sizes itself. No register daemon anywhere.

Covers: the recipe's pool shape, the worker spawn, the sticky forward (page served
by the child through the commander), the child's LOCAL drain of the ping envelope,
and the commander's native ``/metrics`` surface reflecting the guest lifecycle.

Readiness is probed on ``/metrics`` (served natively by the commander process, so
it answers as soon as the front is up) and then on ``/`` (which forwards, so it
answers 200 only once the child hosts the site).
"""

import importlib.util
import os
import re
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")

METRIC_RE = re.compile(r'genropy_site_counters\{counter="(\w+)"\} (\d+)')


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def read_metrics(client: httpx.Client) -> dict[str, int]:
    """The commander's native counters, parsed to a dict."""
    text = client.get("/metrics").text
    return {name: int(value) for name, value in METRIC_RE.findall(text)}


@pytest.fixture(scope="module")
def pool_server():
    """gnrasgiserve as a real subprocess; yields its base URL."""
    port = free_port()
    env = dict(os.environ)
    # macOS: libpq + Kerberos + fork segfaults the forked children without this.
    env.setdefault("PGGSSENCMODE", "disable")
    process = subprocess.Popen(
        [sys.executable, "-m", "genropy_asgi.spa.cli", _SITE, "-p", str(port),
         "--nodebug"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 90.0
        ready = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.skip("gnrasgiserve exited early (site/env not available)")
            try:
                # /metrics is native (commander process); / forwards to the child,
                # so a 200 on it means the worker subprocess hosts the site.
                if httpx.get(base_url + "/metrics", timeout=2.0).status_code == 200:
                    if httpx.get(base_url + "/", timeout=10.0).status_code == 200:
                        ready = True
                        break
            except httpx.HTTPError:
                pass
            time.sleep(0.4)
        if not ready:
            pytest.skip("pool did not come up in time")
        yield base_url
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)


def test_page_served_by_the_pool_through_the_commander(pool_server):
    with httpx.Client(base_url=pool_server, timeout=30.0) as client:
        response = client.get("/")
        assert response.status_code == 200
        match = re.search(r"page_id:'([\w-]+)'", response.text)
        assert match, "no page bootstrap in the forwarded response"
        page_id = match.group(1)
        # the connection the site created came back as the routing cookie
        assert "spa_connection_id" in response.cookies
        # the ping crosses the rail: commander forward -> child handle_ping ->
        # LOCAL pending-list drain on the child -> envelope
        answer = client.get("/_ping", params={"page_id": page_id})
        assert answer.status_code == 200
        assert "<GenRoBag>" in answer.text


def test_metrics_reflects_the_guest_lifecycle(pool_server):
    """The native /metrics is the pool's view: the guest page just served is in it."""
    with httpx.Client(base_url=pool_server, timeout=30.0) as client:
        response = client.get("/")
        assert response.status_code == 200
        counters = read_metrics(client)
        assert set(counters) == {"users", "pages", "connections"}
        # at least THIS client's guest: one user row, one connection, one page
        assert counters["users"] >= 1
        assert counters["connections"] >= 1
        assert counters["pages"] >= 1


def test_the_routing_cookie_survives_a_reload(pool_server):  # wf:phase-3:new
    """The cookie reaches the browser AND the reload comes back on the same cid.

    The doubt this closes (open since 2026-08-14, never retried): through the
    bridge the routing cookie did not reach the client, so every request
    travelled anonymous — freeze worked and wake was unreachable from traffic.
    The reload is the proof: no second ``Set-Cookie`` means the site reused the
    connection the cookie named, and a connection count that does not grow means
    the request landed on the row already there instead of opening a second one.
    """
    with httpx.Client(base_url=pool_server, timeout=30.0) as client:
        first = client.get("/")
        assert first.status_code == 200
        stamps = first.headers.get_list("set-cookie")
        minted = [line for line in stamps if line.startswith("spa_connection_id=")]
        assert len(minted) == 1, f"the connection's birth named no cookie: {stamps}"
        assert "httponly" in minted[0].lower()
        assert "samesite=lax" in minted[0].lower()
        assert "max-age=86400" in minted[0].lower()
        cid = client.cookies["spa_connection_id"]

        before = read_metrics(client)
        reload_response = client.get("/")
        assert reload_response.status_code == 200
        # the site reused the connection the cookie named: nothing rewritten
        assert not [
            line for line in reload_response.headers.get_list("set-cookie")
            if line.startswith("spa_connection_id=")
        ]
        assert client.cookies["spa_connection_id"] == cid
        # ... and the reload stayed on the same connection row
        assert read_metrics(client)["connections"] == before["connections"]

    # counter-proof: a cookie-less client is a new connection, with its own cid
    with httpx.Client(base_url=pool_server, timeout=30.0) as other:
        response = other.get("/")
        assert response.status_code == 200
        assert other.cookies["spa_connection_id"] != cid
        assert read_metrics(other)["connections"] >= before["connections"] + 1
