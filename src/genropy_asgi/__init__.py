# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""genropy-asgi — the bridge between genro-asgi and legacy GenroPy.

The generic commander/worker model lives in genro-asgi core
(``genro_asgi.applications.multi_worker_application``); this package is the
GenroPy-specific bridge on top of it. Three submodules, the only ``gnr.*``-aware code:

- ``genropy_asgi.spa`` — the SPA bridge: ``GenropySpaApplication`` (single) and
  ``GenropyWorkerApplication`` (pool child) host a legacy ``GnrWsgiSite``, plus the
  ``gnrasgiserve`` CLI and the in-process register client.
- ``genropy_asgi.proxy`` — the OpenAPI bridge: a ``GnrApp`` behind an
  ``OpenApiApplication`` with thread-local db cleanup.
- ``genropy_asgi.siteregister`` — the daemonless register the legacy imports as
  ``gnr.web.daemon`` (entry-point ``gnr.web:daemon``), replacing the register daemon.
"""

__version__ = "0.1.0"
