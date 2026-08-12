# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropySpaApplication — the GenroPy front on the core ``SpaApplication``.

The core front owns the whole serving machinery: the user-sticky pool
(``UserStickyCommander``), the two-stage demux (native routes vs the hosted
site), the ``sticky_cid`` cookie mint and the ``http`` CALL forward. This
subclass adds exactly the GenroPy fit:

- the pool defaults: ``worker_class`` is the dotted path of
  :class:`~genropy_asgi.spa.genropy_worker.GenropyWorker` and
  ``worker_kwargs`` carries ``source``/``debug`` — every worker of the pool
  (or the in-process single) hosts the same ``GnrWsgiSite``;
- ``/metrics``: the Prometheus exposition of the site-wide counters, served
  natively by the demux (the commander's own surface is the whole pool's
  view, as the daemon-central siteregister gave it). The metric name
  ``genropy_site_counters`` is the legacy webtool's, so existing scrape
  configs keep working unchanged;
- the ``memory_limit_mb`` auto-derivation (D6, ratified): a POOL whose
  budget was not set explicitly gets ``total_ram * 0.8 / workers`` — from
  ``os.sysconf``, stdlib only — logged at INFO; an explicit value always
  wins, and the single (``workers == 0``) passes nothing at all: the
  in-process worker is never recycled by construction.

Single-process scope: the pool spawn path exists by construction (the core
owns it) but the bridge is validated on the single; the pool bridge is
stage two.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from genro_routes import route

from genro_asgi.applications.spa_app import SpaApplication

log = logging.getLogger("genropy_asgi.spa")

__all__ = ["GenropySpaApplication"]

# The metric name the legacy /metrics webtool exposes (kept identical so existing
# Prometheus scrape configs and dashboards keep working unchanged).
METRIC_PREFIX = "genropy_site_counters"

# The share of total RAM a pool may budget across its workers (D6, ratified).
RAM_SHARE = 0.8


def _as_bool(value: Any) -> bool:
    """Coerce a constructor flag that may arrive as a string (worker --app-arg)."""
    if isinstance(value, str):
        return value.lower() in ("1", "true", "t", "y", "yes")
    return bool(value)


class GenropySpaApplication(SpaApplication):
    """The GenroPy legacy front: a ``SpaApplication`` whose workers host a site."""

    def __init__(self, *, source: str | None = None, debug: Any = False, **kwargs: Any) -> None:
        """Args:
        source: the GenroPy site (name or path) every worker hosts. Required.
        debug: True wraps each worker's site in the Werkzeug debugger.
        kwargs: the base peel — the commander's own kwargs (``workers``,
            ``local_worker``, ``max_workers``, ...) go to the pool, the rest
            up the application chain.
        """
        if not source:
            raise ValueError("GenropySpaApplication requires a source (site name or path)")
        kwargs.setdefault("worker_class", "genropy_asgi.spa.genropy_worker:GenropyWorker")
        kwargs.setdefault("worker_kwargs", {"source": source, "debug": _as_bool(debug)})
        workers = int(kwargs.get("workers") or 0)
        if kwargs.get("memory_limit_mb") is None and workers > 0:
            kwargs["memory_limit_mb"] = self.derive_memory_limit_mb(
                int(kwargs.get("max_workers") or 0) or workers
            )
        super().__init__(**kwargs)

    def derive_memory_limit_mb(self, worker_slots: int) -> int:
        """The per-worker memory budget when none was given (D6, ratified).

        ``total_ram * 0.8``, split over the pool's slots (``max_workers`` when
        capped, the boot target otherwise). Total RAM from ``os.sysconf`` —
        stdlib, never psutil. Logged at INFO so the derived budget is on
        record.
        """
        total_ram_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        derived = int(total_ram_bytes * RAM_SHARE / 2**20 / worker_slots)
        log.info(
            "memory_limit_mb not set: derived %d MB per worker (%.0f%% of RAM over %d slots)",
            derived, RAM_SHARE * 100, worker_slots,
        )
        return derived

    @property
    def gnr_site(self) -> Any:
        """The single's hosted ``GnrWsgiSite`` (the in-process worker's).

        Only the single holds its worker in this process; on a pool the sites
        live in the children and there is no site here to hand out.
        """
        return self.commander.worker.gnr_site

    @route(media_type="text/plain")
    def metrics(self) -> str:
        """Prometheus exposition of the site-wide counters.

        ``users``/``pages``/``connections`` are the exact ``len()`` of the
        commander's routing surface — the whole pool's view, fed by the
        lifecycle fold. The legacy webtool also exposed
        ``stale_connections_5min``, dropped here: the surface is
        keys-and-locations only, so there is no honest value to report.
        """
        commander = self.commander
        counters = {
            "users": len(commander.user_worker_map),
            "pages": len(commander.page_connection),
            "connections": len(commander.connection_user),
        }
        return "\n".join(
            f'{METRIC_PREFIX}{{counter="{name}"}} {value}' for name, value in counters.items()
        )
