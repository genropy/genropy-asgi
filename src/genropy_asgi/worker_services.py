# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Worker service RoutingClasses, mounted on the GenropyProxy.

The worker is a container of services: each family of internal endpoints the
commander (or the monitor) calls is its own RoutingClass, mounted under its
namespace. Today there are two; tomorrow more can be added the same way, each a
class with its own ``@route`` methods — no dispatch logic accretes on the proxy.

    WorkerCommands  -> /_commands/*   orchestration commands from the commander
    WorkerMetrics   -> /_metrics/*    metric views (pressure, ...) for the commander

These are plain RoutingClasses (not full AsgiApplications): the proxy's canonical
dispatch runs their @route nodes, hydrates the body via genro-tytx, and serialises
the result by each node's meta_mime_type. A pop_user blob is raw bytes; an add_user
body arrives already as bytes (octet-stream passthrough in tytx); everything else
is JSON.
"""

from __future__ import annotations

import logging
import pickle
from typing import Any

from genro_routes import Router, RoutingClass, route  # type: ignore[import-untyped]

from genro_asgi.request import get_current_request

log = logging.getLogger("genropy_asgi.worker_services")


class WorkerCommands(RoutingClass):
    """The commands the commander sends the worker for orchestration.

    Owned by the proxy (semantic parent: ``self.worker``). Each command is a
    ``@route`` here, dispatched canonically when mounted under ``/_commands``. The
    user-move handshake is two atomic commands on the worker's local registries:
    pop_user removes a user and returns its state; add_user installs a moved state.
    """

    def __init__(self, worker: Any) -> None:
        """Args:
        worker: the GenropyProxy owning this service (semantic parent).
        """
        self.worker = worker
        self.main = Router(self, name="main")

    @route(meta_mime_type="application/octet-stream")
    def pop_user(self, user: str) -> bytes:
        """Remove a user from this worker and return its pickled state (a move).

        The opaque blob the commander forwards to the destination's add_user. The
        user no longer lives here after this call.
        """
        return pickle.dumps(self.worker.registry_handler.pop_user(user))

    @route(meta_mime_type="application/json")
    def add_user(self) -> dict[str, Any]:
        """Install a moved user's state on this worker, acking the move.

        The pickled blob arrives as the raw request body (octet-stream); genro-tytx
        hands it back as bytes, so request.data IS the blob.
        """
        request = get_current_request()
        assert request is not None  # guaranteed inside the dispatcher
        self.worker.registry_handler.add_user(pickle.loads(request.data))
        return {"ok": True}


class WorkerMetrics(RoutingClass):
    """The worker's metrics, read by the commander and the monitor.

    Owned by the proxy (semantic parent: ``self.worker``). A namespace of views, one
    @route each: ``/_metrics/pressure`` today; ``users``, ``all`` and others can be
    added as @route methods without touching the proxy.
    """

    def __init__(self, worker: Any) -> None:
        """Args:
        worker: the GenropyProxy owning this service (semantic parent).
        """
        self.worker = worker
        self.main = Router(self, name="main")

    @route(meta_mime_type="application/json")
    def pressure(self) -> dict[str, Any]:
        """Executor work-pressure gauges (busy/total/queue/occupancy).

        The signal the commander's scaling will read once it decides by load rather
        than by user count.
        """
        return self.worker.server.executor.metrics


if __name__ == "__main__":
    pass
