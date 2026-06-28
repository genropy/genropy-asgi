# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Tests for the worker service RoutingClasses (WorkerCommands, WorkerMetrics).

These exercise the @route methods directly against a real RegistryHandler — no
transport, no dispatch: the routing/serialisation is the framework's job (covered
elsewhere); here we check the command logic. add_user reads the body from the
current request, so a tiny request stub carrying ``.data`` is set as current.
"""

import os
import pickle
import types

from genro_asgi.request import set_current_request

from genropy_asgi.registry_handler import RegistryHandler
from genropy_asgi.worker_services import WorkerCommands, WorkerMetrics


def _msg(op: str, *args, **kwargs) -> dict:
    return {"ts": 0, "op": op, "args": list(args), "kwargs": kwargs}


class _Worker:
    """Minimal stand-in for the GenropyProxy owning the services."""

    def __init__(self) -> None:
        os.environ["GENRO_WORKER_NAME"] = "pool-1"
        self.registry_handler = RegistryHandler(self)
        # server.executor.metrics, read by WorkerMetrics
        self.server = types.SimpleNamespace(
            executor=types.SimpleNamespace(metrics={"busy": 1, "total": 4, "occupancy": 0.25})
        )


class _Request:
    """A request stub exposing only ``data`` (the parsed/raw body)."""

    def __init__(self, data) -> None:
        self.data = data


def test_pop_user_returns_pickled_blob_and_removes_user():
    worker = _Worker()
    worker.registry_handler.process(
        [_msg("change_connection_user", "c1", user="amelia.martin")]
    )
    commands = WorkerCommands(worker)
    blob = commands.pop_user("amelia.martin")
    assert isinstance(blob, bytes)
    restored = pickle.loads(blob)
    assert restored["user"] == "amelia.martin" and "c1" in restored["connections"]
    assert "amelia.martin" not in worker.registry_handler.users  # removed


def test_add_user_installs_blob_from_request_body():
    worker = _Worker()
    blob = pickle.dumps(
        {
            "user": "amelia.martin",
            "user_entry": {"user": "amelia.martin"},
            "connections": {"c1": {"connection_id": "c1", "user": "amelia.martin"}},
            "pages": {},
        }
    )
    commands = WorkerCommands(worker)
    set_current_request(_Request(blob))
    try:
        ack = commands.add_user()
    finally:
        set_current_request(None)
    assert ack == {"ok": True}
    assert "amelia.martin" in worker.registry_handler.users
    assert "c1" in worker.registry_handler.connections


def test_pop_add_round_trip_between_workers():
    source = _Worker()
    source.registry_handler.process(
        [
            _msg("change_connection_user", "c1", user="amelia.martin"),
            _msg("new_page", "p1", connection_id="c1", user="amelia.martin"),
        ]
    )
    blob = WorkerCommands(source).pop_user("amelia.martin")

    dest = _Worker()
    set_current_request(_Request(blob))
    try:
        WorkerCommands(dest).add_user()
    finally:
        set_current_request(None)
    assert "amelia.martin" in dest.registry_handler.users
    assert "c1" in dest.registry_handler.connections
    assert "p1" in dest.registry_handler.pages


def test_metrics_pressure_reports_executor_gauges():
    worker = _Worker()
    assert WorkerMetrics(worker).pressure() == {"busy": 1, "total": 4, "occupancy": 0.25}
