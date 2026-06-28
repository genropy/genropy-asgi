# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Tests for the WorkerCommanderApplication (daemon-less model).

Covers the wiring done in on_init, the per-worker capacity policy (first-with-room
assignment, 80% scale-up), the HTTP forward through a REAL worker — a plain stdlib
``http.server`` echoing the request, no transport mocks — and the registry-driven
routing: the commander decides the worker from its registries (fed by the worker's
lifecycle piggyback) and our own ``gnr_cid`` cookie, never decoding the GenroPy
session cookie.
"""

import asyncio
import contextlib
import json
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from genro_asgi.exceptions import HTTPServiceUnavailable

from genropy_asgi.genropy_proxy import LIFECYCLE_HEADER
from genropy_asgi.worker_commander import GNR_CID_COOKIE, WorkerCommanderApplication
from genropy_asgi.worker_orchestrator import Allocation


class _EchoHandler(BaseHTTPRequestHandler):
    """A worker that echoes method + path + body, with a custom header back."""

    def log_message(self, *args):  # silence the test output
        pass

    def _reply(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/plain")
        self.send_header("x-worker-port", str(self.server.server_address[1]))
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._reply(f"GET {self.path}".encode())

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        payload = self.rfile.read(length)
        self._reply(b"POST " + self.path.encode() + b" " + payload)


class _RunningOrchestrator:
    """Stub orchestrator exposing a fixed list of running allocations."""

    def __init__(self, allocs: list[Allocation]) -> None:
        self._allocs = allocs
        self.scale_calls: list[tuple[str, int]] = []

    def allocations(self, group: str | None = None) -> list[Allocation]:
        if group is None:
            return list(self._allocs)
        return [a for a in self._allocs if a.group == group]

    def scale(self, group: str, count: int) -> None:
        """Record scale requests; the stub does not actually spawn."""
        self.scale_calls.append((group, count))


@pytest.fixture
def echo_worker():
    """A real HTTP echo server on a free port, torn down after the test."""
    server = HTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield Allocation("pool-1", "pool", host, port)
    finally:
        server.shutdown()
        server.server_close()


async def _drain(messages):
    """Build an ASGI receive callable that yields the given messages in turn."""
    queue = list(messages)

    async def receive():
        return queue.pop(0)

    return receive


def _scope(path: str = "/", cid: str | None = None) -> dict:
    """Build an HTTP scope carrying our gnr_cid cookie (or none)."""
    headers = [(b"host", b"main:8080")]
    if cid is not None:
        headers.append((b"cookie", f"{GNR_CID_COOKIE}={cid}".encode("latin-1")))
    return {"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": headers}


def test_on_init_wires_orchestrator_and_client():
    commander = WorkerCommanderApplication(
        site="x", workers="3", max_users_first="10", max_users_other="20"
    )
    assert commander.site == "x"
    assert commander.workers == 3
    assert commander.max_users_first == 10
    assert commander.max_users_other == 20
    assert commander.user_registry == {}
    assert commander.cid_to_user == {}
    assert commander.orchestrator.application is commander
    assert commander.client.commander is commander


def test_on_init_requires_site():
    with pytest.raises(ValueError):
        WorkerCommanderApplication()


@pytest.mark.asyncio
async def test_logins_fill_first_worker_then_spill():
    # First worker cap 2 (hosts guests too), others cap 3. Two logins fit on
    # pool_01; the third spills to pool_02. Driven through the public login flow.
    commander = _commander_pool(first=2, other=3)
    commander.client = _StubMoveClient()
    await _login(commander, "c1", "u1")
    await _login(commander, "c2", "u2")
    assert commander.user_registry["u1"]["worker"] == "pool_01"
    assert commander.user_registry["u2"]["worker"] == "pool_01"
    await _login(commander, "c3", "u3")  # third overflows pool_01 (cap 2)
    assert commander.user_registry["u3"]["worker"] == "pool_02"


def test_check_capacity_scales_at_80_percent():
    # cap_first=5 -> 80% = 4. With 4 users on pool_01 the threshold is reached
    # and a new worker is requested (pool count 1 -> 2).
    commander = WorkerCommanderApplication(site="x", max_users_first="5", max_users_other="5")
    orch = _RunningOrchestrator([Allocation("pool_01", "pool", "127.0.0.1", 1)])
    commander.orchestrator = orch
    for i in range(4):
        commander.user_registry[f"u{i}"] = {"connections": set(), "worker": "pool_01"}
    commander._check_capacity()
    assert ("pool", 2) in orch.scale_calls


def test_check_capacity_quiet_below_threshold():
    commander = WorkerCommanderApplication(site="x", max_users_first="5", max_users_other="5")
    orch = _RunningOrchestrator([Allocation("pool_01", "pool", "127.0.0.1", 1)])
    commander.orchestrator = orch
    for i in range(3):  # 3/5 = 60%, below 80%
        commander.user_registry[f"u{i}"] = {"connections": set(), "worker": "pool_01"}
    commander._check_capacity()
    assert orch.scale_calls == []


def test_pick_welcome_is_first_worker():
    commander = WorkerCommanderApplication(site="x")
    a = Allocation("pool_01", "pool", "127.0.0.1", 1)
    b = Allocation("pool_02", "pool", "127.0.0.1", 2)
    commander.orchestrator = _RunningOrchestrator([a, b])
    assert commander.pick_welcome().id == "pool_01"


def test_pick_welcome_no_worker_raises():
    commander = WorkerCommanderApplication(site="x")
    commander.orchestrator = _RunningOrchestrator([])
    with pytest.raises(HTTPServiceUnavailable):
        commander.pick_welcome()


@pytest.mark.asyncio
async def test_forward_get(echo_worker):
    commander = WorkerCommanderApplication(site="x")
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/hello",
        "query_string": b"a=1",
        "headers": [(b"host", b"main:8080"), (b"accept", b"text/plain")],
    }
    receive = await _drain([{"type": "http.request", "body": b"", "more_body": False}])
    status, headers, body = await commander.client.forward(echo_worker, scope, receive)
    await commander.client.aclose()
    assert status == 200
    assert body == b"GET /hello?a=1"
    header_map = {k: v for k, v in headers}
    assert header_map[b"x-worker-port"] == str(echo_worker.port).encode()


@pytest.mark.asyncio
async def test_forward_post_with_body(echo_worker):
    commander = WorkerCommanderApplication(site="x")
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/submit",
        "query_string": b"",
        "headers": [(b"host", b"main:8080"), (b"content-type", b"text/plain")],
    }
    receive = await _drain(
        [
            {"type": "http.request", "body": b"hel", "more_body": True},
            {"type": "http.request", "body": b"lo", "more_body": False},
        ]
    )
    status, _headers, body = await commander.client.forward(echo_worker, scope, receive)
    await commander.client.aclose()
    assert status == 200
    assert body == b"POST /submit hello"


@pytest.mark.asyncio
async def test_handle_request_forwards_and_relays(echo_worker):
    # echo_worker is a "pool" allocation; a known logged user routes to it via the
    # registries (no cookie decode), with the gnr_cid cookie naming the connection.
    commander = WorkerCommanderApplication(site="x")
    commander.orchestrator = _RunningOrchestrator([echo_worker])
    commander.cid_to_user["c1"] = "amelia.martin"
    commander.user_registry["amelia.martin"] = {"connections": {"c1"}, "worker": echo_worker.id}
    receive = await _drain([{"type": "http.request", "body": b"", "more_body": False}])
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await commander.handle_request(_scope("/x", cid="c1"), receive, send)
    await commander.client.aclose()
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200
    assert sent[1]["type"] == "http.response.body"
    assert sent[1]["body"] == b"GET /x"


# -- registry-driven routing (gnr_cid, no cookie decode) --


def _pool(n: int = 3) -> _RunningOrchestrator:
    """Stub with ``n`` running pool workers. The first one also serves guests."""
    return _RunningOrchestrator(
        [Allocation(f"pool_{i:02d}", "pool", "127.0.0.1", i) for i in range(1, n + 1)]
    )


def _commander_pool(n: int = 3, first: int = 50, other: int = 50) -> WorkerCommanderApplication:
    commander = WorkerCommanderApplication(
        site="x", max_users_first=str(first), max_users_other=str(other)
    )
    commander.orchestrator = _pool(n)
    return commander


def test_route_no_cookie_to_first_worker():
    # A fresh client (no gnr_cid) is a new connection: it goes to the welcome worker.
    commander = _commander_pool()
    assert commander.route(_scope()).id == "pool_01"


def test_route_unknown_cid_to_first_worker():
    # A stale cookie (worker restarted, cid forgotten) is treated as a fresh client.
    commander = _commander_pool()
    assert commander.route(_scope(cid="ghost")).id == "pool_01"


def test_route_known_cid_to_its_user_worker():
    # The registries (fed by the piggyback) bind the cid to a user on pool_02.
    commander = _commander_pool()
    commander.cid_to_user["c1"] = "amelia.martin"
    commander.user_registry["amelia.martin"] = {"connections": {"c1"}, "worker": "pool_02"}
    assert commander.route(_scope(cid="c1")).id == "pool_02"


def test_route_known_cid_is_sticky():
    commander = _commander_pool()
    commander.cid_to_user["c1"] = "amelia.martin"
    commander.user_registry["amelia.martin"] = {"connections": {"c1"}, "worker": "pool_03"}
    for _ in range(5):
        assert commander.route(_scope(cid="c1")).id == "pool_03"


def test_route_falls_back_to_welcome_when_worker_gone():
    # The user's worker died: with no live worker for it, the request falls back to
    # the welcome worker (a fresh assignment will follow from the next login).
    commander = _commander_pool()
    commander.cid_to_user["c1"] = "amelia.martin"
    commander.user_registry["amelia.martin"] = {"connections": {"c1"}, "worker": "pool_99"}
    assert commander.route(_scope(cid="c1")).id == "pool_01"


def test_cookie_cid_reads_only_our_cookie():
    # Other cookies (e.g. the GenroPy session cookie) are ignored; only gnr_cid is read.
    commander = _commander_pool()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(b"cookie", f"sitecookie=abc; {GNR_CID_COOKIE}=c9; other=z".encode("latin-1"))],
    }
    assert commander.cookie_cid(scope) == "c9"


# -- login worker decision + user move (Phase 4) --


class _StubMoveClient:
    """Stub forward client recording pop_user/add_user; add_user can be made to fail."""

    def __init__(self, *, fail_add: bool = False) -> None:
        self.fail_add = fail_add
        self.calls: list[tuple[str, str]] = []

    async def pop_user(self, alloc, user: str) -> bytes:
        self.calls.append(("pop", alloc.id))
        return b"blob-of-" + user.encode()

    async def add_user(self, alloc, blob: bytes) -> dict:
        self.calls.append(("add", alloc.id))
        if self.fail_add:
            raise RuntimeError("destination refused")
        return {"ok": True}


async def _login(commander, cid: str, user: str, worker: str = "pool_01") -> None:
    """Drive one full login through the public flow: a guest connection is born on
    the welcome worker, then renamed to a real user — both via the lifecycle batch
    handle_request applies. No registry is touched by hand.
    """
    commander.apply_lifecycle([{"op": "new_connection", "worker": worker, "connection_id": cid}])
    events = [{"op": "change_connection_user", "worker": worker, "connection_id": cid, "user": user}]
    commander.apply_lifecycle(events)
    await commander._handle_logins(events)


@pytest.mark.asyncio
async def test_first_users_stay_on_welcome_no_move():
    # The early users fit on the first worker (cap 5): each login stays put, no move.
    commander = _commander_pool(first=5, other=5)
    commander.client = _StubMoveClient()
    for i in range(5):
        await _login(commander, f"c{i}", f"user{i}")
    assert commander.client.calls == []  # all fit on pool_01
    assert all(e["worker"] == "pool_01" for e in commander.user_registry.values())


@pytest.mark.asyncio
async def test_overflow_user_is_moved_to_next_worker():
    # First worker cap 5: the 6th login lands on pool_01 (full) and is moved to pool_02.
    commander = _commander_pool(first=5, other=5)
    commander.client = _StubMoveClient()
    for i in range(5):
        await _login(commander, f"c{i}", f"user{i}")
    await _login(commander, "c6", "luigi.bianchi")  # the sixth
    assert commander.client.calls == [("pop", "pool_01"), ("add", "pool_02")]
    assert commander.user_registry["luigi.bianchi"]["worker"] == "pool_02"


@pytest.mark.asyncio
async def test_move_failure_leaves_user_on_welcome():
    # If the destination refuses, the allocation is NOT repointed: no half-moved state.
    commander = _commander_pool(first=5, other=5)
    commander.client = _StubMoveClient(fail_add=True)
    for i in range(5):
        await _login(commander, f"c{i}", f"user{i}")
    await _login(commander, "c6", "luigi.bianchi")
    assert commander.user_registry["luigi.bianchi"]["worker"] == "pool_01"  # stayed


# -- metrics polling --


class _StubMetricsClient:
    """Stub forward client: returns metrics for known workers, raises otherwise."""

    def __init__(self, metrics_by_id: dict) -> None:
        self._metrics_by_id = metrics_by_id

    async def get_metrics(self, alloc) -> dict:
        if alloc.id not in self._metrics_by_id:
            raise RuntimeError(f"{alloc.id} unreachable")
        return self._metrics_by_id[alloc.id]


async def _run_one_poll(commander) -> None:
    """Run the poll loop briefly (interval is tiny), then cancel it."""
    task = asyncio.create_task(commander._poll_metrics())
    await asyncio.sleep(commander.metrics_interval * 3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_poll_metrics_populates_map_and_skips_unreachable():
    commander = WorkerCommanderApplication(site="x", metrics_interval=0.01)
    a = Allocation("pool-1", "pool", "127.0.0.1", 1)
    b = Allocation("pool-2", "pool", "127.0.0.1", 2)
    commander.orchestrator = _RunningOrchestrator([a, b])
    # pool-1 answers, pool-2 is unreachable (raises): the loop must survive.
    commander.client = _StubMetricsClient({"pool-1": {"occupancy": 0.5, "busy": 2}})

    await _run_one_poll(commander)

    assert commander.worker_metrics["pool-1"] == {"occupancy": 0.5, "busy": 2}
    assert "pool-2" not in commander.worker_metrics  # skipped, no crash


# -- lifecycle piggyback worker -> commander (apply_lifecycle) --


def _commander() -> WorkerCommanderApplication:
    return WorkerCommanderApplication(site="x")


def test_apply_lifecycle_new_connection_registers_guest():
    # A connection is born guest on the worker that served it: its user key is the
    # cid itself, with a one-connection entry on that worker.
    commander = _commander()
    commander.apply_lifecycle(
        [{"op": "new_connection", "worker": "pool-1", "connection_id": "c1"}]
    )
    assert commander.cid_to_user["c1"] == "c1"
    entry = commander.user_registry["c1"]
    assert entry["connections"] == {"c1"}
    assert entry["worker"] == "pool-1"


def test_apply_lifecycle_login_relabels_guest_to_real_user():
    # First born guest, then login: the connection leaves its guest entry and joins
    # the real user's; the empty guest entry is removed.
    commander = _commander()
    commander.apply_lifecycle(
        [{"op": "new_connection", "worker": "pool-1", "connection_id": "c1"}]
    )
    commander.apply_lifecycle(
        [{"op": "change_connection_user", "worker": "pool-1",
          "connection_id": "c1", "user": "amelia.martin"}]
    )
    assert commander.cid_to_user["c1"] == "amelia.martin"
    assert "c1" not in commander.user_registry  # the guest entry is gone
    entry = commander.user_registry["amelia.martin"]
    assert entry["connections"] == {"c1"}
    assert entry["worker"] == "pool-1"


def test_apply_lifecycle_login_binds_connection_to_user():
    commander = _commander()
    commander.apply_lifecycle(
        [{"op": "change_connection_user", "worker": "pool-1",
          "connection_id": "c1", "user": "amelia.martin"}]
    )
    assert commander.cid_to_user["c1"] == "amelia.martin"
    entry = commander.user_registry["amelia.martin"]
    assert entry["connections"] == {"c1"}
    assert entry["worker"] == "pool-1"


def test_apply_lifecycle_multi_connection_same_user_one_worker():
    commander = _commander()
    for cid in ("c1", "c2", "c3"):
        commander.apply_lifecycle(
            [{"op": "change_connection_user", "worker": "pool-1",
              "connection_id": cid, "user": "amelia.martin"}]
        )
    entry = commander.user_registry["amelia.martin"]
    assert entry["connections"] == {"c1", "c2", "c3"}
    assert entry["worker"] == "pool-1"  # ten browsers, one worker


def test_apply_lifecycle_drop_connection_keeps_user_until_empty():
    commander = _commander()
    for cid in ("c1", "c2"):
        commander.apply_lifecycle(
            [{"op": "change_connection_user", "worker": "pool-1",
              "connection_id": cid, "user": "amelia.martin"}]
        )
    # drop_connection carries ONLY the connection_id: the commander knows the user.
    commander.apply_lifecycle([{"op": "drop_connection", "worker": "pool-1", "connection_id": "c1"}])
    assert "c1" not in commander.cid_to_user
    assert commander.user_registry["amelia.martin"]["connections"] == {"c2"}
    # last connection gone -> the user is gone.
    commander.apply_lifecycle([{"op": "drop_connection", "worker": "pool-1", "connection_id": "c2"}])
    assert "amelia.martin" not in commander.user_registry
    assert commander.cid_to_user == {}


def test_apply_lifecycle_drop_user_clears_all_connections():
    commander = _commander()
    for cid in ("c1", "c2"):
        commander.apply_lifecycle(
            [{"op": "change_connection_user", "worker": "pool-1",
              "connection_id": cid, "user": "amelia.martin"}]
        )
    commander.apply_lifecycle([{"op": "drop_user", "worker": "pool-1", "user": "amelia.martin"}])
    assert "amelia.martin" not in commander.user_registry
    assert commander.cid_to_user == {}


def test_apply_lifecycle_ignores_worker_local_ops():
    # Page/new_user ops never reach the commander's registries; they are no-ops here.
    commander = _commander()
    for op in ("new_user", "new_page", "drop_page", "drop_pages"):
        commander.apply_lifecycle([{"op": op, "connection_id": "c1", "user": "amelia.martin"}])
    assert commander.cid_to_user == {}
    assert commander.user_registry == {}


def test_apply_lifecycle_unknown_op_warns(caplog):
    commander = _commander()
    with caplog.at_level("WARNING"):
        commander.apply_lifecycle([{"op": "frobnicate", "connection_id": "c1"}])
    assert any("frobnicate" in r.message for r in caplog.records)
    assert commander.cid_to_user == {}


def test_take_lifecycle_strips_header_and_folds_events():
    commander = _commander()
    events = [{"op": "change_connection_user", "worker": "pool-1",
               "connection_id": "c1", "user": "amelia.martin"}]
    headers = [
        (b"content-type", b"text/plain"),
        (LIFECYCLE_HEADER, json.dumps(events).encode()),
        (b"x-worker-port", b"1234"),
    ]
    kept, parsed = commander._split_lifecycle(headers)
    commander.apply_lifecycle(parsed)
    names = {k for k, _ in kept}
    assert LIFECYCLE_HEADER not in names
    assert b"content-type" in names and b"x-worker-port" in names
    assert commander.cid_to_user["c1"] == "amelia.martin"


def test_birth_cookie_minted_on_new_connection():
    # On new_connection the commander mints a Set-Cookie for gnr_cid; the lifecycle
    # header is stripped and never relayed.
    commander = _commander()
    events = [{"op": "new_connection", "worker": "pool-1", "connection_id": "c7"}]
    headers = [
        (b"content-type", b"text/html"),
        (LIFECYCLE_HEADER, json.dumps(events).encode()),
    ]
    kept, parsed = commander._split_lifecycle(headers)
    commander.apply_lifecycle(parsed)
    cookies = commander._birth_cookies(parsed)
    set_cookies = [v for k, v in cookies if k.lower() == b"set-cookie"]
    assert len(set_cookies) == 1
    morsel = SimpleCookie(set_cookies[0].decode("latin-1")).get(GNR_CID_COOKIE)
    assert morsel is not None and morsel.value == "c7"
    assert LIFECYCLE_HEADER not in {k for k, _ in kept}
    assert commander.cid_to_user["c7"] == "c7"  # registered guest


@pytest.mark.asyncio
async def test_poll_metrics_drops_stale_workers():
    commander = WorkerCommanderApplication(site="x", metrics_interval=0.01)
    a = Allocation("pool-1", "pool", "127.0.0.1", 1)
    commander.orchestrator = _RunningOrchestrator([a])
    commander.client = _StubMetricsClient({"pool-1": {"occupancy": 0.1}})
    # Seed a stale entry for a worker no longer in the running set.
    commander.worker_metrics["gone-9"] = {"occupancy": 0.9}

    await _run_one_poll(commander)

    assert "pool-1" in commander.worker_metrics
    assert "gone-9" not in commander.worker_metrics  # dropped
