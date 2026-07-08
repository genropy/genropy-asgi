# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Exceptions for the in-process siteregister.

The names mirror the register-daemon hierarchy the legacy code catches (it does
``except GnrDaemonLocked`` around a store lock). There is no daemon here, so
``GnrDaemonLocked`` only ever fires on the in-process lock retry budget, and the
transport errors (``GnrDaemonUnavailable``, ``GnrDaemonMethodNotFound``) can never
happen — they are kept only so legacy imports and ``except`` clauses resolve.
"""

from __future__ import annotations

__all__ = [
    "GnrDaemonException",
    "GnrDaemonLocked",
    "GnrDaemonMethodNotFound",
    "GnrDaemonUnavailable",
]


class GnrDaemonException(Exception):
    """Base class for register errors (kept for legacy compatibility)."""


class GnrDaemonLocked(GnrDaemonException):
    """An in-process item lock could not be acquired within the retry budget."""


class GnrDaemonMethodNotFound(GnrDaemonException):
    """Never raised in-process; kept so legacy imports resolve."""


class GnrDaemonUnavailable(GnrDaemonException):
    """Never raised in-process (no transport); kept for import compatibility."""
