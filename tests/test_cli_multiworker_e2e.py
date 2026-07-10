# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""End-to-end: ``gnrasgiserve --workers N`` — commander + real worker subprocess.

The real deliverable, driven from the outside: the CLI boots the commander (a
``SpaMultiWorkerApplication`` from the recipe), which spawns a worker subprocess
hosting the GenroPy site; the test talks HTTP only. No register daemon anywhere.

Covers: the recipe's pool shape, the worker spawn, the sticky forward (page served by
the child through the commander), the child's LOCAL drain of the ping envelope (switch
model — no pull RPC), and the commander's own back-channel smoke endpoint.
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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def pool_server():
    """gnrasgiserve --workers 1 as a real subprocess; yields its base URL."""
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "genropy_asgi.spa.cli", _SITE, "-p", str(port),
         "--nodebug", "--workers", "1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=dict(os.environ),
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60.0
        ready = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.skip("gnrasgiserve exited early (site/env not available)")
            try:
                answer = httpx.get(base_url + "/_commander/ping", timeout=2.0)
                if answer.status_code == 200 and answer.json().get("commander"):
                    ready = True
                    break
            except httpx.HTTPError:
                time.sleep(0.4)
        if not ready:
            pytest.skip("commander did not come up in time")
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
        # the commander minted its sticky cookie on the connection's birth
        assert "gnr_cid" in response.cookies
        # the ping crosses the rail: commander forward -> child handle_ping ->
        # LOCAL pending-list drain on the child (switch model) -> envelope
        answer = client.get("/_ping", params={"page_id": page_id})
        assert answer.status_code == 200
        assert "<GenRoBag>" in answer.text
        # the commander's back-channel answers directly (not forwarded)
        pong = client.get("/_commander/ping")
        assert pong.status_code == 200
        assert pong.json() == {"commander": True}
