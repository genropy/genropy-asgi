# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Tests for MonitorPage rendering — the per-app info branch.

The monitor reads the commander's user_registry, whose values are plain dicts
{"connections": set, "worker": alloc_id} (UserAffinity was removed). The user
line must render entry["worker"], not an attribute. These tests build the registry
through the public lifecycle flow, never by hand-cabling it.
"""

from genropy_asgi.monitor.monitor_app import MonitorPage
from genropy_asgi.worker_commander import WorkerCommanderApplication
from genropy_asgi.worker_orchestrator import Allocation


class _RunningOrchestrator:
    """Stub orchestrator with a fixed running pool (the monitor only reads it)."""

    def __init__(self, allocs: list[Allocation]) -> None:
        self._allocs = allocs

    def allocations(self, group: str | None = None) -> list[Allocation]:
        if group is None:
            return list(self._allocs)
        return [a for a in self._allocs if a.group == group]

    def scale(self, group: str, count: int) -> None:
        pass


def _commander_with_pool(n: int = 2) -> WorkerCommanderApplication:
    commander = WorkerCommanderApplication(site="x", max_users_first="50", max_users_other="50")
    commander.orchestrator = _RunningOrchestrator(
        [Allocation(f"pool_{i:02d}", "pool", "127.0.0.1", i) for i in range(1, n + 1)]
    )
    return commander


def test_app_info_renders_user_registry_entries():
    # A logged user (built via the public lifecycle flow) shows up as "user -> worker".
    commander = _commander_with_pool()
    commander.apply_lifecycle([{"op": "new_connection", "worker": "pool_01", "connection_id": "c1"}])
    commander.apply_lifecycle(
        [{"op": "change_connection_user", "worker": "pool_01",
          "connection_id": "c1", "user": "amelia.martin"}]
    )
    lines = MonitorPage()._app_info(commander)
    assert "users: 1" in lines
    assert "  - amelia.martin -> pool_01" in lines


def test_app_info_empty_registry_no_users():
    commander = _commander_with_pool()
    lines = MonitorPage()._app_info(commander)
    assert "users: 0" in lines


def test_app_info_generic_app_reports_protocol_only():
    # A non-commander app (no orchestrator) contributes only its protocol line.
    class _Plain:
        app_protocol = "wsgi"

    lines = MonitorPage()._app_info(_Plain())
    assert lines == ["protocol: wsgi"]
