# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Fake ``gnr.web.daemon.service`` — there is no daemon service in this build.

The legacy CLI (``gnr web daemon``) imports ``DaemonService`` here. In the in-process
build there is no daemon to start/stop, so running that command is an explicit error;
the import itself resolves so the CLI dispatch table does not break at load time.
"""

from __future__ import annotations

from typing import Any

__all__ = ["DaemonService"]


class DaemonService:
    """Stub: there is no register daemon to control in the in-process build."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._args = args

    def run(self) -> None:
        raise RuntimeError(
            "no register daemon in this build: the register is served in-process, "
            "there is nothing to start with `gnr web daemon`"
        )
