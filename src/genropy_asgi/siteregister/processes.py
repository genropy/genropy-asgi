# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Fake ``gnr.web.daemon.processes`` — the daemon service-process machinery is gone.

No module outside the daemon package imported this at runtime; it exists only so the
``gnr.web.daemon.processes`` namespace resolves for any stray import. Empty on purpose.
"""

from __future__ import annotations

__all__: list[str] = []
