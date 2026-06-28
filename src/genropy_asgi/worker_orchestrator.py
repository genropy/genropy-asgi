# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Minimal worker orchestrator — a poor man's Nomad.

The sticky-worker proxy needs a pool of worker processes (each serving the same
GenroPy site) and a live view of where they are. That job — create the workers,
keep them alive, tell who is where — is isolated here behind an interface that
deliberately mimics Nomad's vocabulary and operations, so a future swap to real
Nomad is a change of implementation, not of the calling code.

Vocabulary (from Nomad, verified):
    WorkerJob   -- the declaration of what to run and how many (job + group +
                   task + count). ``driver`` selects the implementation.
    Allocation  -- a live worker: a concrete instance of the job placed
                   somewhere, reachable at host:port, with a status.
    Orchestrator -- the control interface, with Nomad-named operations:
                   register(job), scale(count), allocations(), stop(). A
                   supervision thread keeps "running allocations == count",
                   relaunching/rediscovering the dead ones (resilience is in,
                   not bolted on later).

Implementations:
    LocalOrchestrator -- driver "local": spawns each worker as a
                   ``genropy_asgi.worker_entry`` subprocess on an auto-assigned
                   free port, waits until it answers, and relaunches it if it dies.
    (future) NomadOrchestrator / registration-based, behind the same interface.

The orchestrator does NOT talk to the workers (it never forwards requests): it
only creates, supervises and lists them. Talking to the chosen worker is the
transport's job — a separate boundary.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any


class WorkerGroup:
    """A named set of identical workers with its own replica count (Nomad group).

    The ``name`` is the role (e.g. ``"pool"`` for the served pool; later
    ``"green"``/``"blue"``/``"canary"``). The first worker of the pool also hosts
    the guests, so it carries fewer logged users than the others.
    ``count`` is how many replicas of this group to keep alive. ``capacity`` is
    the per-worker connection cap the proxy honours for sticky affinity (0 =
    unlimited); it is a declarative attribute of the job, not orchestrator logic.
    """

    __slots__ = ("name", "count", "capacity")

    def __init__(self, name: str, count: int = 1, capacity: int = 0) -> None:
        self.name = name
        self.count = count
        self.capacity = capacity


class WorkerJob:
    """Declaration of what to run and how many (Nomad job + groups + task).

    ``site`` is the GenroPy site name each worker serves (the task); ``groups``
    are the named groups (each with its own count); ``driver`` selects the
    orchestrator implementation ("local" for now); ``host`` is the bind/contact
    host. Every worker of every group serves the same ``site``.
    """

    __slots__ = ("site", "groups", "driver", "host")

    def __init__(
        self,
        site: str,
        groups: list[WorkerGroup],
        driver: str = "local",
        host: str = "127.0.0.1",
    ) -> None:
        self.site = site
        self.groups = groups
        self.driver = driver
        self.host = host

    def group(self, name: str) -> WorkerGroup | None:
        """Return the group declaration by name, or None."""
        for g in self.groups:
            if g.name == name:
                return g
        return None


class Allocation:
    """A live worker of a group: reachable at host:port (Nomad allocation).

    ``group`` is the role it belongs to (e.g. ``"pool"``).
    ``status`` is ``"pending"`` while starting, ``"running"`` once it answers,
    ``"dead"`` when its process is gone. ``handle`` holds the implementation's
    grip on the instance (e.g. the subprocess), not part of the public address.
    """

    __slots__ = ("id", "group", "host", "port", "status", "handle")

    def __init__(self, alloc_id: str, group: str, host: str, port: int) -> None:
        self.id = alloc_id
        self.group = group
        self.host = host
        self.port = port
        self.status: str = "pending"
        self.handle: Any = None


class Orchestrator:
    """Control interface mimicking Nomad: register/scale/allocations/stop.

    Owns the supervision thread that keeps the running allocations equal to the
    job's ``count``. Subclasses implement how a worker is started, checked and
    stopped; the supervision loop and the public API live here.
    """

    SUPERVISE_INTERVAL = 1.0  # seconds between supervision checks
    READY_TIMEOUT = 30.0  # seconds to wait for a worker to answer
    READY_INTERVAL = 0.2  # seconds between readiness probes

    def __init__(self, application: Any) -> None:
        """Args:
        application: the proxy that owns this orchestrator (semantic parent).
        """
        self.application = application
        self.job: WorkerJob | None = None
        self._allocations: dict[str, Allocation] = {}
        self._group_seq: dict[str, int] = {}  # per-group counter for worker names
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, job: WorkerJob) -> None:
        """Register the job and start every group at its declared count."""
        self.job = job
        for group in job.groups:
            self.scale(group.name, group.count)
        self._start_supervision()

    def scale(self, group: str, count: int) -> None:
        """Bring the running allocations of ``group`` to ``count``.

        Adds allocations when short, stops the surplus when over. The group's
        ``count`` is updated so supervision targets the new number.
        """
        if self.job is None:
            raise RuntimeError("scale requires a registered job")
        declaration = self.job.group(group)
        if declaration is None:
            raise ValueError(f"unknown group {group!r}")
        declaration.count = count
        with self._lock:
            current = [
                a
                for a in self._allocations.values()
                if a.group == group and a.status != "dead"
            ]
            while len(current) < count:
                alloc = self._new_allocation(group)
                self._allocations[alloc.id] = alloc
                current.append(alloc)
            while len(current) > count:
                alloc = current.pop()
                self._stop_allocation(alloc)
                self._allocations.pop(alloc.id, None)

    def allocations(self, group: str | None = None) -> list[Allocation]:
        """Return the live (running) allocations, optionally of one group only."""
        with self._lock:
            return [
                a
                for a in self._allocations.values()
                if a.status == "running" and (group is None or a.group == group)
            ]

    def stop(self) -> None:
        """Stop supervision and all allocations (deregister the job)."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        with self._lock:
            for alloc in self._allocations.values():
                self._stop_allocation(alloc)
            self._allocations.clear()

    def _new_allocation(self, group: str) -> Allocation:
        """Start one worker of ``group``, waiting until it answers.

        The allocation id is the worker's speaking name: ``<group>_NN`` with a
        per-group counter (pool_01, blue_01, green_01...). The name identifies the
        worker in the events it re-emits to the commander (it is set into the
        worker process as GENRO_WORKER_NAME).
        """
        if self.job is None:
            raise RuntimeError("_new_allocation requires a registered job")
        seq = self._group_seq.get(group, 0) + 1
        self._group_seq[group] = seq
        alloc_id = f"{group}_{seq:02d}"
        alloc = self._start_worker(alloc_id, group, self.job)
        if self._wait_ready(alloc):
            alloc.status = "running"
        else:
            self._stop_allocation(alloc)
            alloc.status = "dead"
        return alloc

    def _wait_ready(self, alloc: Allocation) -> bool:
        """Poll the worker's port until it accepts a connection or times out."""
        deadline = time.monotonic() + self.READY_TIMEOUT
        while time.monotonic() < deadline:
            if self._is_alive(alloc) is False:
                return False
            try:
                with socket.create_connection((alloc.host, alloc.port), timeout=1.0):
                    return True
            except OSError:
                time.sleep(self.READY_INTERVAL)
        return False

    def _start_supervision(self) -> None:
        """Launch the daemon thread that keeps running allocations == count."""
        self._thread = threading.Thread(target=self._supervise, daemon=True)
        self._thread.start()

    def _supervise(self) -> None:
        """Mark dead allocations and relaunch each group up to its count."""
        while not self._stop_event.wait(self.SUPERVISE_INTERVAL):
            if self.job is None:
                continue
            with self._lock:
                for alloc in list(self._allocations.values()):
                    if alloc.status == "running" and self._is_alive(alloc) is False:
                        alloc.status = "dead"
                missing = {
                    g.name: g.count
                    - len(
                        [
                            a
                            for a in self._allocations.values()
                            if a.group == g.name and a.status != "dead"
                        ]
                    )
                    for g in self.job.groups
                }
            for group, shortfall in missing.items():
                for _ in range(max(0, shortfall)):
                    replacement = self._new_allocation(group)
                    with self._lock:
                        self._allocations[replacement.id] = replacement

    # -- driver-specific: subclasses implement these --

    def _start_worker(self, alloc_id: str, group: str, job: WorkerJob) -> Allocation:
        """Start one worker of ``group`` for ``job`` and return its allocation."""
        raise NotImplementedError

    def _is_alive(self, alloc: Allocation) -> bool:
        """Return True if the worker behind ``alloc`` is still running."""
        raise NotImplementedError

    def _stop_allocation(self, alloc: Allocation) -> None:
        """Stop the worker behind ``alloc`` (idempotent)."""
        raise NotImplementedError


class LocalOrchestrator(Orchestrator):
    """Driver "local": each worker is a ``genropy_asgi.worker_entry`` subprocess.

    The port is auto-assigned (bind to port 0, let the OS pick), passed to the
    worker entry point; the supervision thread relaunches a subprocess that exits.
    The worker is a ``GenroAsgiWorker`` (minimal single-app ASGI server) hosting a
    ``GenropyProxy`` on the job's site — the daemon-less executor unit.

    Each worker is started in its own session (``start_new_session=True``), so it
    leads a fresh process group its children inherit; stopping signals the whole
    group, reaping any child the worker spawned (no orphans left behind).
    """

    def _start_worker(self, alloc_id: str, group: str, job: WorkerJob) -> Allocation:
        port = self._free_port(job.host)
        alloc = Allocation(alloc_id, group, job.host, port)
        alloc.handle = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "genropy_asgi.worker_entry",
                job.site,
                "-p",
                str(port),
                "-H",
                job.host,
                "--name",
                alloc_id,
                "--group",
                group,
                "--nodebug",
            ],
            start_new_session=True,
        )
        return alloc

    def _is_alive(self, alloc: Allocation) -> bool:
        proc = alloc.handle
        return proc is not None and proc.poll() is None

    def _stop_allocation(self, alloc: Allocation) -> None:
        """Signal the worker's whole process group, then reap.

        A worker normally has no children (single uvicorn process), but signalling
        the whole group reaps any descendant it might spawn, leaving no orphans.
        Since the worker leads its own group, the group signal covers them all.
        """
        proc = alloc.handle
        if proc is None or proc.poll() is not None:
            return
        self._signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self._signal_group(proc, signal.SIGKILL)

    def _signal_group(self, proc: subprocess.Popen[Any], sig: int) -> None:
        """Send ``sig`` to the worker's process group (idempotent)."""
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except ProcessLookupError:
            pass

    def _free_port(self, host: str) -> int:
        """Return a free TCP port on ``host`` (bind to 0, read it back)."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])


if __name__ == "__main__":
    import sys

    site_name = sys.argv[1] if len(sys.argv) > 1 else "test_invoice_pg"
    orch = LocalOrchestrator(application=None)
    orch.register(
        WorkerJob(
            site=site_name,
            groups=[WorkerGroup("pool", count=2)],
        )
    )
    print("all:", [(a.id, a.group, a.port, a.status) for a in orch.allocations()])
    print("pool:", [a.id for a in orch.allocations(group="pool")])
    orch.stop()
