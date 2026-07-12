# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyCommanderApplication — the pool commander for GenroPy sites.

The multi's commander (``SpaMultiWorkerApplication``) with one GenroPy-specific
addition: the ``/metrics`` Prometheus endpoint. It emulates the legacy ``/metrics``
webtool (``genropy/webtools/prometheus.py``), which read ``site.datacollector`` — a view
over the DAEMON-central ``siteregister`` that saw every user/connection/page of the site.

In the daemonless model that central view is GONE from the worker: each worker's
in-process register sees only ITS OWN slice. But the commander already keeps the
site-wide surface (``app_registry``: ``user_registry``/``cid_to_user``/``pages_index``),
so the endpoint lives HERE. Being an ``@route`` on the commander, ``/metrics`` is a
service segment served locally (the demux in ``SpaApplication.handle_request``) instead
of being forwarded to a single worker — the counters are the whole pool's, as the daemon
gave them.

Composed the same way as the single/worker GenroPy apps, but on the multi: no
``GnrSiteHostingMixin`` here — the commander hosts NO GnrWsgiSite (it is a reverse proxy),
it only reads the aggregated registries it already holds.
"""

from __future__ import annotations

from genro_asgi import route
from genro_asgi.applications.multi_worker_application import SpaMultiWorkerApplication

__all__ = ["GenropyCommanderApplication"]

# The metric name the legacy /metrics webtool exposes (kept identical so existing
# Prometheus scrape configs and dashboards keep working unchanged).
METRIC_PREFIX = "genropy_site_counters"


class GenropyCommanderApplication(SpaMultiWorkerApplication):
    """Pool commander for GenroPy, serving the site-wide ``/metrics`` endpoint."""

    @route(media_type="text/plain")
    def metrics(self) -> str:
        """Prometheus exposition of the pool's site-wide counters.

        ``users``/``pages``/``connections`` are the exact ``len()`` of the commander's
        aggregated registries — the whole pool, as the daemon-central siteregister gave
        them. The legacy webtool also exposed ``stale_connections_5min``, dropped here:
        it needs a per-connection ``last_refresh_ts`` the commander does not keep (its
        surface is keys-and-locations only), so there is no honest value to report.
        """
        reg = self.app_registry
        users = len(reg.user_registry)
        pages = len(reg.pages_index)
        connections = len(reg.cid_to_user)
        lines = [
            f'{METRIC_PREFIX}{{counter="users"}} {users}',
            f'{METRIC_PREFIX}{{counter="pages"}} {pages}',
            f'{METRIC_PREFIX}{{counter="connections"}} {connections}',
        ]
        return "\n".join(lines)


if __name__ == "__main__":
    pass
