# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Fake ``gnr.web.daemon.handler`` — there is no daemon in this build.

The legacy lazily imports ``GnrDaemonProxy`` here only when something reads
``app.gnrdaemon`` (a control/stop handle for the register daemon). Nothing in the
request path does, so this is never reached in practice; it exists so the namespace
resolves, and any actual use is an explicit error rather than a silent no-op.
"""

from __future__ import annotations

from typing import Any

__all__ = ["GnrDaemonProxy"]

_NO_DAEMON = "no register daemon in this build (the register is served in-process)"


class GnrDaemonProxy:
    """Stub daemon-control proxy: constructing or calling it is an explicit error."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError(_NO_DAEMON)

    def proxy(self) -> Any:
        raise RuntimeError(_NO_DAEMON)
