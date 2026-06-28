# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Tests for the minimal worker orchestrator.

Exercises the base Orchestrator logic (register/scale/allocations/stop,
readiness wait, supervision and relaunch) with REAL worker subprocesses and
REAL sockets — no mocks. The worker here is a plain stdlib ``http.server`` on a
free port, so the test does not need a GenroPy site; LocalOrchestrator (which
spawns ``gnrwsgiserve``) shares the very same base, so the logic under test is
the production one. Its own subprocess wiring is covered by the smoke test.
"""

import socket
import subprocess
import sys
import time

import pytest

from genropy_asgi.worker_orchestrator import (
    Allocation,
    LocalOrchestrator,
    Orchestrator,
    WorkerGroup,
    WorkerJob,
)


def _job(**counts: int) -> WorkerJob:
    """A job over a real HTTP-server task with the given group->count map."""
    groups = [WorkerGroup(name, count) for name, count in counts.items()]
    return WorkerJob(site="x", groups=groups)

# A worker that is a real HTTP server on the given port, nothing more.
_HTTP_WORKER = (
    "import sys,http.server;"
    "p=int(sys.argv[1]);"
    "http.server.HTTPServer(('127.0.0.1',p),http.server.BaseHTTPRequestHandler)"
    ".serve_forever()"
)


class HttpOrchestrator(Orchestrator):
    """Test driver: each worker is a real stdlib HTTP server subprocess."""

    SUPERVISE_INTERVAL = 0.2

    def _start_worker(self, alloc_id: str, group: str, job: WorkerJob) -> Allocation:
        port = self._free_port(job.host)
        alloc = Allocation(alloc_id, group, job.host, port)
        alloc.handle = subprocess.Popen([sys.executable, "-c", _HTTP_WORKER, str(port)])
        return alloc

    def _is_alive(self, alloc: Allocation) -> bool:
        proc = alloc.handle
        return proc is not None and proc.poll() is None

    def _stop_allocation(self, alloc: Allocation) -> None:
        proc = alloc.handle
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _free_port(self, host: str) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])


@pytest.fixture
def orch():
    o = HttpOrchestrator(application=None)
    yield o
    o.stop()


def _responds(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


class TestRegisterAndScale:
    """register starts each group's count of workers; they run and are reachable."""

    def test_register_starts_running_allocations(self, orch: HttpOrchestrator) -> None:
        orch.register(_job(pool=2))
        allocs = orch.allocations()
        assert len(allocs) == 2
        assert all(a.status == "running" for a in allocs)
        assert all(_responds(a.host, a.port) for a in allocs)

    def test_each_worker_has_its_own_port(self, orch: HttpOrchestrator) -> None:
        orch.register(_job(pool=3))
        ports = {a.port for a in orch.allocations()}
        assert len(ports) == 3  # auto-assigned, all distinct

    def test_scale_up_adds_workers(self, orch: HttpOrchestrator) -> None:
        orch.register(_job(pool=1))
        orch.scale("pool", 3)
        assert len(orch.allocations("pool")) == 3

    def test_scale_down_stops_surplus(self, orch: HttpOrchestrator) -> None:
        orch.register(_job(pool=3))
        before = {(a.host, a.port) for a in orch.allocations()}
        orch.scale("pool", 1)
        survivors = orch.allocations()
        assert len(survivors) == 1
        time.sleep(0.5)
        stopped = before - {(a.host, a.port) for a in survivors}
        assert len(stopped) == 2
        assert all(not _responds(h, p) for h, p in stopped)


class TestGroups:
    """Named groups: a 'beta' (1) group plus a 'pool' (2)."""

    def test_groups_have_their_own_counts(self, orch: HttpOrchestrator) -> None:
        orch.register(_job(beta=1, pool=2))
        assert len(orch.allocations()) == 3
        assert len(orch.allocations("beta")) == 1
        assert len(orch.allocations("pool")) == 2

    def test_allocation_carries_its_group(self, orch: HttpOrchestrator) -> None:
        orch.register(_job(beta=1, pool=2))
        beta = orch.allocations("beta")
        assert beta[0].group == "beta"
        assert {a.group for a in orch.allocations("pool")} == {"pool"}

    def test_unknown_group_scale_raises(self, orch: HttpOrchestrator) -> None:
        orch.register(_job(pool=1))
        with pytest.raises(ValueError):
            orch.scale("nope", 2)


class TestSupervision:
    """A worker that dies is detected and relaunched up to its group's count."""

    def test_dead_worker_is_relaunched_in_its_group(self, orch: HttpOrchestrator) -> None:
        orch.register(_job(beta=1, pool=2))
        victim = orch.allocations("beta")[0]
        victim.handle.kill()
        victim.handle.wait()
        # supervision (0.2s) detects the death and restarts within the same group
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            beta = orch.allocations("beta")
            if len(beta) == 1 and victim.id not in {a.id for a in beta}:
                break
            time.sleep(0.2)
        beta = orch.allocations("beta")
        assert len(beta) == 1
        assert beta[0].group == "beta"
        assert _responds(beta[0].host, beta[0].port)
        assert len(orch.allocations("pool")) == 2  # the other group is untouched


class TestWorkerGroupCapacity:
    """A group carries a per-worker connection capacity (0 = unlimited)."""

    def test_capacity_defaults_to_unlimited(self) -> None:
        assert WorkerGroup("standard", 3).capacity == 0

    def test_capacity_is_stored(self) -> None:
        assert WorkerGroup("standard", 3, capacity=5).capacity == 5


class TestStop:
    """stop terminates supervision and every worker of every group."""

    def test_stop_terminates_all(self, orch: HttpOrchestrator) -> None:
        orch.register(_job(beta=1, pool=2))
        addrs = [(a.host, a.port) for a in orch.allocations()]
        orch.stop()
        time.sleep(0.5)
        assert all(not _responds(h, p) for h, p in addrs)


# A worker that spawns a CHILD http server (on a second free port) and prints the
# child port on stdout. The child outlives a bare parent terminate(); only a
# process-group signal reaps it. Mirrors gnrwsgiserve spawning a werkzeug child.
_PARENT_CHILD_WORKER = (
    "import sys,socket,subprocess,http.server,threading;"
    "p=int(sys.argv[1]);"
    "s=socket.socket();s.bind(('127.0.0.1',0));cp=s.getsockname()[1];s.close();"
    "child=subprocess.Popen([sys.executable,'-c',"
    "\"import sys,http.server;"
    "http.server.HTTPServer(('127.0.0.1',int(sys.argv[1])),"
    "http.server.BaseHTTPRequestHandler).serve_forever()\",str(cp)]);"
    "print(cp,flush=True);"
    "http.server.HTTPServer(('127.0.0.1',p),http.server.BaseHTTPRequestHandler)"
    ".serve_forever()"
)


class ParentChildOrchestrator(LocalOrchestrator):
    """Driver whose worker spawns a child, started via the real _stop_allocation.

    It inherits LocalOrchestrator (so start_new_session + process-group kill are
    the production code under test) and only swaps how the worker is launched: a
    parent that forks a listening child, reporting the child's port on stdout.
    """

    SUPERVISE_INTERVAL = 0.2

    def _start_worker(self, alloc_id: str, group: str, job: WorkerJob) -> Allocation:
        port = self._free_port(job.host)
        alloc = Allocation(alloc_id, group, job.host, port)
        alloc.handle = subprocess.Popen(
            [sys.executable, "-c", _PARENT_CHILD_WORKER, str(port)],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        return alloc


class TestProcessGroupReap:
    """stop() reaps the worker's children too, leaving no orphan in listen."""

    def test_no_child_left_listening_after_stop(self) -> None:
        orch = ParentChildOrchestrator(application=None)
        try:
            orch.register(_job(pool=1))
            alloc = orch.allocations("pool")[0]
            child_port = int(alloc.handle.stdout.readline().strip())
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not _responds(alloc.host, child_port):
                time.sleep(0.1)
            assert _responds(alloc.host, child_port), "child never came up"
            parent_port = alloc.port
        finally:
            orch.stop()
        time.sleep(0.5)
        assert not _responds("127.0.0.1", parent_port), "parent still listening"
        assert not _responds("127.0.0.1", child_port), "child orphaned after stop"
