# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""WorkerCommanderApplication — the brain of the daemon-less worker model.

A single app mounted on the MAIN server (a standard multi-app AsgiServer). It
does NOT run GenroPy work: it only orchestrates and forwards. On startup it
registers a ``WorkerJob`` with a ``LocalOrchestrator`` (which spawns and keeps
alive N ``genropy_asgi.worker_entry`` worker processes, each a minimal ASGI
server hosting a GenropyProxy on the site); for each request it picks a running
worker and forwards the HTTP request to it over HTTP, relaying the response.

This is the Step-1 skeleton: no per-user stickiness yet — the routing policy is
a trivial round-robin over the running workers, just to prove the pipe end to
end. Per-user affinity, capacity and migration come in later phases.

HTTP-only: no websocket branch (Step 1).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from http.cookies import SimpleCookie
from typing import Any

import httpx

from genro_asgi.applications import AsgiApplication
from genro_asgi.exceptions import HTTPServiceUnavailable
from genro_toolbox.smartasync import smartasync  # type: ignore[import-untyped]

from .genropy_proxy import LIFECYCLE_HEADER
from .worker_orchestrator import (
    Allocation,
    LocalOrchestrator,
    WorkerGroup,
    WorkerJob,
)

log = logging.getLogger("genropy_asgi.commander")

# Headers the commander must not relay to the worker: httpx sets Host and the
# framing headers itself, and Connection is hop-by-hop. Relaying them would
# duplicate or break the forwarded request.
_HOP_BY_HOP = frozenset(
    {b"host", b"content-length", b"transfer-encoding", b"connection"}
)

# Our own connection cookie, set in clear by the commander on new_connection and
# read back on every request to route by connection_id. Not the GenroPy session
# cookie: no HMAC, no unmarshal, no shared secret.
GNR_CID_COOKIE = "gnr_cid"

# Timeout for a move request (pop_user/add_user) to a worker. Longer than the
# metrics poll: shipping a user's connections and pages with their content can be
# heavier than a gauge read, but it must still not stall the request forever.
MOVE_TIMEOUT = 10.0


class WorkerCommanderApplication(AsgiApplication):
    """Mounted app that spawns/supervises workers and forwards requests to them.

    For the MAIN server this is a normal AsgiApplication; internally it owns a
    ``LocalOrchestrator`` (semantic parent relationship: the orchestrator's
    ``application`` is this commander) and a ``WorkerForwardClient``. It never
    touches ``self.server``, request registry or auth: it is a pure router.

    Routing is sticky per user: a guest goes to the first pool worker (which also
    serves as reception); a logged user is assigned a pool worker at first login
    (read from the session cookie, decoded in-process) and sticks to it for every
    later request.
    """

    def on_init(
        self,
        site: str | None = None,
        host: str = "127.0.0.1",
        driver: str = "local",
        workers: int = 1,
        max_users_first: int = 20,
        max_users_other: int = 30,
        metrics_interval: float = 2.0,
        **kwargs: Any,
    ) -> None:
        """Wire the orchestrator and the forward client (no spawn here).

        Spawning is blocking (it waits for the workers to answer), so it lives in
        ``on_startup``, not at construction/mount time.
        """
        if site is None:
            raise ValueError("WorkerCommanderApplication requires a 'site'")
        self.site = site
        self.worker_host = host
        self.driver = driver
        # One pool of workers; the FIRST also hosts the guests, so it holds fewer
        # logged users. Per-worker user caps: the first uses max_users_first, the
        # others max_users_other. At 80% of a worker's cap a new worker is spawned.
        self.workers = int(workers)
        self.max_users_first = int(max_users_first)
        self.max_users_other = int(max_users_other)
        self.orchestrator = LocalOrchestrator(application=self)
        self.client = WorkerForwardClient(self)
        # Worker pressure map (alloc.id -> last metrics dict), refreshed by the
        # polling task every ``metrics_interval`` seconds. Read by /_monitor.
        # Observation only: nothing here decides anything yet.
        self.metrics_interval = float(metrics_interval)
        self.worker_metrics: dict[str, dict[str, Any]] = {}
        self._metrics_task: asyncio.Task[None] | None = None
        # Sticky-per-connection state, fed only by the worker's lifecycle events
        # (apply_lifecycle). Two plain registries in the worker's dict-of-dict style:
        #   cid_to_user:   connection_id -> user (a guest's user is its own cid)
        #   user_registry: user -> {"connections": set[connection_id], "worker": alloc_id}
        # The user OWNS its connections: a user's requests all go to its one worker;
        # when its connection set empties, the user is gone and is removed. Routing
        # reads our gnr_cid cookie (set on new_connection) and resolves it through
        # these maps — the GenroPy session cookie is never decoded.
        self.cid_to_user: dict[str, str] = {}
        self.user_registry: dict[str, dict[str, Any]] = {}

    async def on_startup(self) -> None:
        """Register the job and pre-spawn the workers (blocking, off the loop).

        ``register`` spawns the workers and waits until each answers on its port
        (boot of a GenroPy site is seconds). Run it through ``smartasync`` so it
        executes in a thread and does not block the MAIN event loop; the server's
        startup still awaits it, so the MAIN starts serving only once the workers
        are ready (synchronised pre-spawn).
        """
        job = WorkerJob(
            site=self.site,
            groups=[WorkerGroup("pool", count=self.workers)],
            driver=self.driver,
            host=self.worker_host,
        )
        await smartasync(self.orchestrator.register)(job)
        self._metrics_task = asyncio.create_task(self._poll_metrics())

    async def on_shutdown(self) -> None:
        """Stop the metrics poller, the orchestrator (kills the workers), client."""
        if self._metrics_task is not None:
            self._metrics_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._metrics_task
        await smartasync(self.orchestrator.stop)()
        await self.client.aclose()

    async def _poll_metrics(self) -> None:
        """Poll /_metrics from each running worker into ``worker_metrics``.

        Pull-based observation: every ``metrics_interval`` seconds, read each
        worker's pressure gauges. A worker still booting or already gone simply
        does not answer — it is skipped, the loop survives, and stale entries
        (allocations no longer present) are dropped.
        """
        while True:
            await asyncio.sleep(self.metrics_interval)
            allocs = self.orchestrator.allocations()
            live_ids = {alloc.id for alloc in allocs}
            for stale_id in [k for k in self.worker_metrics if k not in live_ids]:
                self.worker_metrics.pop(stale_id, None)
            for alloc in allocs:
                try:
                    self.worker_metrics[alloc.id] = await self.client.get_metrics(alloc)
                except Exception as exc:  # noqa: BLE001 — one unreachable worker must not kill the loop
                    log.debug("metrics poll skipped %s: %s", alloc.id, exc)

    def cookie_cid(self, scope: dict[str, Any]) -> str | None:
        """Read our opaque connection id from the request's gnr_cid cookie.

        Our own cookie, set in clear by the commander on new_connection (see
        _take_lifecycle): no HMAC, no unmarshal, no shared secret. Returns the
        connection_id, or None when the cookie is absent (a fresh client).
        """
        header = scope.get("headers", [])
        for name, value in header:
            if name.lower() == b"cookie":
                morsel = SimpleCookie(value.decode("latin-1")).get(GNR_CID_COOKIE)
                return morsel.value if morsel is not None else None
        return None

    def pick_welcome(self) -> Allocation:
        """The guests' reception: the FIRST pool worker.

        Guests are not sticky and have no state to preserve; they all land on the
        first worker, which therefore carries fewer logged users (its user cap is
        lower). No round-robin: it is deterministically the first.
        """
        running = self.orchestrator.allocations(group="pool")
        if not running:
            raise HTTPServiceUnavailable("no worker available")
        return running[0]

    def _user_cap(self, index: int) -> int:
        """The logged-user cap of the worker at position ``index`` in the pool.

        The first worker (index 0) also hosts the guests, so it takes fewer logged
        users than the others.
        """
        return self.max_users_first if index == 0 else self.max_users_other

    def _users_on(self, alloc_id: str) -> int:
        """How many logged users are currently routed to this worker.

        PROVISIONAL: the load proxy is the user count; it will become the worker's
        real work pressure.
        """
        return sum(1 for entry in self.user_registry.values() if entry["worker"] == alloc_id)

    def assign_pool_worker(self) -> Allocation:
        """Pick a worker for a just-logged user: the first one with room.

        Walks the running pool in order and returns the first worker still within
        its user cap. The user being decided is already counted on the worker it
        landed on (apply_lifecycle registered it before the decision), so "has room"
        is ``<= cap``: a cap of 5 holds five users on the welcome, the sixth spills.
        No round-robin. When the chosen worker crosses 80% of its cap a new worker is
        requested (see _check_capacity).

        PROVISIONAL criterion: today the load proxy is the user COUNT. It will become
        the worker's real work pressure (executor gauges), at which point this and
        _users_on/_user_cap/_check_capacity change together; the move handshake they
        feed does not.
        """
        running = self.orchestrator.allocations(group="pool")
        if not running:
            raise HTTPServiceUnavailable("no worker available")
        for index, alloc in enumerate(running):
            if self._users_on(alloc.id) <= self._user_cap(index):
                return alloc
        # All workers are at their cap: a new one is overdue. Place the user on the
        # last worker (least-bad) and let _check_capacity spawn more.
        log.warning("all workers at user cap; placing on %s", running[-1].id)
        return running[-1]

    def _check_capacity(self) -> None:
        """Spawn a new worker when any worker crosses 80% of its user cap.

        Capacity is provisioned ahead of saturation: at 80% fill a new pool worker
        is added, so assign_pool_worker normally always finds room.
        """
        running = self.orchestrator.allocations(group="pool")
        for index, alloc in enumerate(running):
            if self._users_on(alloc.id) >= 0.8 * self._user_cap(index):
                self.orchestrator.scale("pool", len(running) + 1)
                log.info("worker %s over 80%% of cap; scaled pool to %d", alloc.id, len(running) + 1)
                return

    def route(self, scope: dict[str, Any]) -> Allocation:
        """Choose the worker for a request from the registries (no cookie decode).

        The request carries our opaque gnr_cid cookie. We resolve it to a user via
        cid_to_user, then to that user's worker. No gnr_cid (a fresh client) or an
        unknown cid (a stale cookie, e.g. after a worker restart) means a new
        connection: it goes to the welcome worker, which mints the cookie on
        new_connection. The session cookie is never decoded here.
        """
        cid = self.cookie_cid(scope)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("route path=%r cid=%r", scope.get("path"), cid)
        if cid is None:
            return self.pick_welcome()
        user = self.cid_to_user.get(cid)
        entry = self.user_registry.get(user) if user is not None else None
        if entry is not None:
            current = self._running_by_id(entry["worker"])
            if current is not None:
                return current
        return self.pick_welcome()

    def _running_by_id(self, alloc_id: str) -> Allocation | None:
        """Return the running allocation with this id, or None if it is gone."""
        for alloc in self.orchestrator.allocations():
            if alloc.id == alloc_id:
                return alloc
        return None

    async def handle_request(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Forward the request to the routed worker and relay its response.

        The worker piggybacks its lifecycle events on the LIFECYCLE_HEADER. We pull
        the header off (it is internal, never client-facing), fold the events into
        the registries, mint the gnr_cid cookie for every connection born here, and
        — for a login — decide the user's worker and migrate it if needed, all
        before relaying the response.
        """
        alloc = self.route(scope)
        status, headers, body = await self.client.forward(alloc, scope, receive)
        headers, events = self._split_lifecycle(headers)
        self.apply_lifecycle(events)
        headers += self._birth_cookies(events)
        await self._handle_logins(events)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    def _split_lifecycle(
        self, headers: list[tuple[bytes, bytes]]
    ) -> tuple[list[tuple[bytes, bytes]], list[dict[str, Any]]]:
        """Split the worker's response headers from its piggybacked lifecycle events.

        Returns the headers with LIFECYCLE_HEADER removed (it must never reach the
        browser) and the decoded event list (empty if absent or malformed).
        """
        kept: list[tuple[bytes, bytes]] = []
        events: list[dict[str, Any]] = []
        for name, value in headers:
            if name.lower() == LIFECYCLE_HEADER:
                with contextlib.suppress(Exception):
                    events = json.loads(value.decode("latin-1"))
            else:
                kept.append((name, value))
        return kept, events

    def _birth_cookies(self, events: list[dict[str, Any]]) -> list[tuple[bytes, bytes]]:
        """Set-Cookie headers for every connection born in this batch (new_connection).

        The commander owns the gnr_cid cookie and mints it the moment a connection
        is born, so the next request from that client carries its connection_id.
        """
        cookies: list[tuple[bytes, bytes]] = []
        for event in events:
            if event.get("op") == "new_connection" and event.get("connection_id"):
                cookies.append((b"set-cookie", self._cid_cookie(event["connection_id"])))
        return cookies

    def _cid_cookie(self, cid: str) -> bytes:
        """Build the Set-Cookie value for our gnr_cid connection cookie.

        Clear value, root path, HttpOnly (no JS need read it). Session cookie (no
        Max-Age): it lives as long as the browser session, like the connection.
        """
        return f"{GNR_CID_COOKIE}={cid}; Path=/; HttpOnly; SameSite=Lax".encode("latin-1")

    async def _handle_logins(self, events: list[dict[str, Any]]) -> None:
        """Decide each fresh login's worker and migrate the user when it differs.

        A login (change_connection_user) was served on the SOURCE worker, which has
        already renamed the user in its own registries. The commander decides the
        user's worker: the same one until it saturates, then the next. When the
        decision equals the source nothing moves. When it differs, move_user ships
        the user's state from source to destination and only then re-points the
        allocation.
        """
        for event in events:
            if event.get("op") != "change_connection_user":
                continue
            user = event.get("user")
            source = event.get("worker")
            if not user or source is None:
                continue
            destination = self.decide_worker(user)
            if destination != source:
                await self.move_user(user, source, destination)

    def decide_worker(self, user: str) -> str:
        """Pick where a just-logged user belongs, by capacity — the worker it landed
        on if that one has room, otherwise the first that does.

        A login is always a fresh decision (a logout is death: there is no surviving
        affinity to honour). The user has just been registered on the welcome worker
        where it landed; assign_pool_worker — which counts that user too — returns the
        first worker under its cap, so the user stays on the welcome while it has room
        and is sent to the next once it is full. _check_capacity provisions ahead of
        saturation. Later requests of the same logged user do not pass here: route()
        sticks them to this worker via the consolidated registry.
        """
        alloc = self.assign_pool_worker()
        self._check_capacity()
        return alloc.id

    async def move_user(self, user: str, source: str, destination: str) -> None:
        """Move a user from source to destination via the two-step handshake.

        The commander is a blind courier: pop_user asks the source for the user's
        state as an opaque pickled blob (the source removes the user as it hands it
        over); add_user installs that blob on the destination. The allocation is
        re-pointed to the destination ONLY after the destination acks — a failure
        mid-handshake leaves the user on the source, no half-moved state.
        """
        src = self._running_by_id(source)
        dst = self._running_by_id(destination)
        if src is None or dst is None:
            log.warning("move_user %s: source/destination gone, abort", user)
            return
        try:
            blob = await self.client.pop_user(src, user)
            await self.client.add_user(dst, blob)
        except Exception:  # noqa: BLE001 — a failed move must not break the request
            log.exception("move_user %s %s->%s failed; user stays on source", user, source, destination)
            return
        entry = self.user_registry.get(user)
        if entry is not None:
            entry["worker"] = destination
        log.info("moved user %s from %s to %s", user, source, destination)

    def apply_lifecycle(self, events: list[dict[str, Any]]) -> None:
        """Fold worker lifecycle events into the commander's registries.

        Pure bookkeeping: it records what the worker has already done, on the worker
        that did it. The login worker decision and any migration live in
        handle_request, where the flow is async. The vocabulary has nine ops, each
        an explicit branch. new_connection: a connection is born guest on the worker
        that served it. change_connection_user (login): bind the connection to its
        real user on that same worker (the source). drop_connection: drop the
        connection; when the user's set empties, the user is gone. drop_connections
        / drop_user: drop a whole user. The remaining ops (new_user, new_page,
        drop_page, drop_pages) are worker-local; an unknown op is logged.
        """
        for event in events:
            op = event.get("op")
            cid = event.get("connection_id")
            if op == "new_connection":
                if cid is None:
                    continue
                self._register_connection(cid, cid, event.get("worker"))
            elif op == "change_connection_user":
                user = event.get("user")
                if not user or cid is None:
                    continue
                self._relabel_connection(cid, user, event.get("worker"))
            elif op == "drop_connection":
                if cid is None:
                    continue
                self._drop_connection(cid)
            elif op in ("drop_connections", "drop_user"):
                user = event.get("user")
                if user is None:
                    continue
                self._drop_user(user)
            elif op in ("new_user", "new_page", "drop_page", "drop_pages"):
                pass  # worker-local ops; the commander does not track pages/guests
            else:
                log.warning("apply_lifecycle: unknown op %r", op)

    def _register_connection(self, cid: str, user: str, worker: str | None) -> None:
        """Record a connection under ``user`` on ``worker`` (born guest: user==cid).

        Guests and logged users share one registry; a guest's user key is its own
        connection_id, so a fresh connection gets a one-connection entry on the
        worker that served it (the welcome worker for the first hit).
        """
        self.cid_to_user[cid] = user
        entry = self.user_registry.setdefault(user, {"connections": set(), "worker": worker})
        entry["connections"].add(cid)
        if worker:
            entry["worker"] = worker

    def _relabel_connection(self, cid: str, user: str, worker: str | None) -> None:
        """Move a connection from its current (possibly guest) user to ``user``.

        At login the connection leaves its guest entry and joins the real user's,
        keeping its worker. The old guest entry, now empty, is removed.
        """
        old = self.cid_to_user.get(cid)
        if old is not None and old != user:
            old_entry = self.user_registry.get(old)
            if old_entry is not None:
                old_entry["connections"].discard(cid)
                if not old_entry["connections"]:
                    del self.user_registry[old]
        self._register_connection(cid, user, worker)

    def _drop_connection(self, cid: str) -> None:
        """Remove a connection from its user; drop the user when its set empties."""
        user = self.cid_to_user.pop(cid, None)
        if user is None:
            return
        entry = self.user_registry.get(user)
        if entry is not None:
            entry["connections"].discard(cid)
            if not entry["connections"]:
                del self.user_registry[user]

    def _drop_user(self, user: str) -> None:
        """Remove a user and all of its connections from both registries."""
        entry = self.user_registry.pop(user, None)
        if entry is not None:
            for dead_cid in entry["connections"]:
                self.cid_to_user.pop(dead_cid, None)


class WorkerForwardClient:
    """HTTP forwarder from the commander to a worker (httpx behind one API).

    Holds a reused ``httpx.AsyncClient`` (redirects off, to stay a transparent
    proxy). Encapsulating httpx here keeps it a swappable detail: the commander
    knows only ``forward``/``get_metrics``/``pop_user``/``add_user``/``aclose``.
    """

    def __init__(self, commander: WorkerCommanderApplication) -> None:
        self.commander = commander
        self._client = httpx.AsyncClient(follow_redirects=False)

    async def forward(
        self, alloc: Allocation, scope: dict[str, Any], receive: Any
    ) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        """Forward one HTTP request to ``alloc`` and return its raw response.

        Returns ``(status_code, headers, body)`` where headers are the worker's
        response headers as a list of (name, value) byte tuples — the shape the
        ASGI ``send`` expects.
        """
        body = await self._read_body(receive)
        path = scope.get("path", "/")
        query = scope.get("query_string", b"").decode("latin-1")
        url = f"http://{alloc.host}:{alloc.port}{path}"
        if query:
            url = f"{url}?{query}"
        headers = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in scope.get("headers", [])
            if name.lower() not in _HOP_BY_HOP
        ]
        response = await self._client.request(
            scope.get("method", "GET"),
            url,
            headers=headers,
            content=body,
        )
        return response.status_code, list(response.headers.raw), response.content

    async def get_metrics(self, alloc: Allocation) -> dict[str, Any]:
        """GET /_metrics/pressure from a worker and return the parsed JSON dict.

        Short timeout: a slow or booting worker must not stall the poll loop.
        """
        url = f"http://{alloc.host}:{alloc.port}/_metrics/pressure"
        response = await self._client.get(url, timeout=1.0)
        return dict(response.json())

    async def pop_user(self, alloc: Allocation, user: str) -> bytes:
        """Ask a worker to hand over a user's state, returning the opaque blob.

        GET /_commands/pop_user/<user>: the worker pickles the user's connections
        and pages (with their content) and removes the user as it answers. The body
        is binary and the commander never decodes it — it forwards it to add_user.
        """
        url = f"http://{alloc.host}:{alloc.port}/_commands/pop_user/{user}"
        response = await self._client.get(url, timeout=MOVE_TIMEOUT)
        response.raise_for_status()
        return response.content

    async def add_user(self, alloc: Allocation, blob: bytes) -> dict[str, Any]:
        """Install a moved user's state on a worker, returning its JSON ack.

        POST /_commands/add_user with the opaque blob as the raw binary body; the
        worker unpickles it into its local registries and acks. raise_for_status so
        a non-2xx aborts the move before the allocation is re-pointed.
        """
        url = f"http://{alloc.host}:{alloc.port}/_commands/add_user"
        response = await self._client.post(
            url,
            content=blob,
            headers={"content-type": "application/octet-stream"},
            timeout=MOVE_TIMEOUT,
        )
        response.raise_for_status()
        return dict(response.json())

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()

    async def _read_body(self, receive: Any) -> bytes:
        """Read the full request body from the ASGI receive channel."""
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        return body


if __name__ == "__main__":
    pass
