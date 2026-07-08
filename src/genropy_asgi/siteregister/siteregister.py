# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""The ``gnr.web.daemon.siteregister`` face of the in-process register.

The legacy imports a handful of names from ``gnr.web.daemon.siteregister``
(``DEFAULT_PAGE_MAX_AGE``, ``GnrDaemonException``) and its backward-compat shim
re-exports ``RegisterResolver`` from here too. This module provides exactly those,
with no daemon server behind them: the register lives inside the SPA application, not
in a separate ``GnrSiteRegister`` process.
"""

from __future__ import annotations

from typing import Any

from .exceptions import GnrDaemonException

__all__ = ["DEFAULT_PAGE_MAX_AGE", "GnrDaemonException", "RegisterResolver"]

# The page eviction window the legacy cleanup config falls back to (seconds).
DEFAULT_PAGE_MAX_AGE = 120


class RegisterResolver:
    """Placeholder for the admin-UI lazy resolver (browse users/connections/pages).

    The legacy re-exports this name; the in-process build does not drive the daemon
    admin browser, so instantiating it is an explicit error rather than a silent
    empty tree. Kept only so the import resolves.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "RegisterResolver (daemon admin browser) is not available in the "
            "in-process siteregister"
        )
