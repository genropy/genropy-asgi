# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropySpaApplication — the GenroPy front on the core ``SpaApplicationNew``.

The core front owns the whole serving machinery: the pool born from the
recipe at startup (``SpaCommander`` and its groups), the two-stage demux
(native routes vs the hosted site), the ``sticky_cid`` cookie mint and the
``http`` CALL forward. The pool is not configured here: its words live in
the recipe, under ``applications.<code>.commander`` — see
``genropy_asgi.spa.config`` for the recipe this package ships. This
subclass adds exactly the GenroPy fit:

- the ROOT mount, declared as the class attribute the core reads
  (``mount = ""``): a GenroPy site builds absolute URLs, so a prefix would
  serve the first page and 404 every path that page asks for. A non-empty
  ``mount`` kwarg is refused at construction rather than silently obeyed;
- the startup check that every group's ``worker_kwargs`` names a
  ``source``: a pool of workers that host no site would come up and refuse
  every request, so the recipe defect is said at boot, not discovered in
  the log of the first child;
- ``/metrics``: the Prometheus exposition served natively by the demux.
  ``genropy_site_counters`` keeps the legacy MEANING — the three population
  counters (users, pages, connections), read from the commander's own
  indexes — and the commander's aggregate event counters are exposed as
  additional ``genropy_site_events`` lines. Nothing an existing scrape read
  disappears.
"""

from __future__ import annotations

from typing import Any

from genro_routes import route

from genro_asgi.applications.spa_app_new import SpaApplicationNew

__all__ = ["GenropySpaApplication"]

# The metric name the legacy /metrics webtool exposes (kept identical so existing
# Prometheus scrape configs and dashboards keep working unchanged).
METRIC_PREFIX = "genropy_site_counters"

# The commander's aggregate event counters, exposed as their own metric family.
EVENTS_PREFIX = "genropy_site_events"


class GenropySpaApplication(SpaApplicationNew):
    """The GenroPy legacy front: a ``SpaApplicationNew`` whose workers host a site.

    Mounted at the ROOT by design (``mount = ""``): a GenroPy site builds its own
    absolute URLs — ``/_ping``, ``/_rpc``, every static and resource path — so a
    prefix in front of them would answer the first page and 404 everything it asks
    for next.
    """

    mount = ""

    def __init__(self, **kwargs: Any) -> None:
        """Args:
        kwargs: up the application chain. A ``mount`` can only ever confirm
            the root: an empty one is dropped, a non-empty one is refused.
            The pool (worker class, site source, policies) is NOT configured
            here — it is recipe words under ``applications.<code>.commander``.
        """
        mount = kwargs.pop("mount", None)
        if mount:
            raise ValueError(
                "GenropySpaApplication is mounted at the root: a GenroPy site owns its "
                f"absolute URLs, so mount={mount!r} would break every path it builds. "
                "Serve it on its own host or port instead."
            )
        super().__init__(**kwargs)

    async def on_startup(self) -> None:  # wf:phase-2:new
        """Check the recipe names a site for every group, then build the pool.

        Raises:
            ValueError: a group's ``worker_kwargs`` carries no ``source`` —
                its workers would host no site and refuse every request.
        """
        for name, group in self.server.config.group_kwargs(self.code).items():
            if not (group.get("worker_kwargs") or {}).get("source"):
                raise ValueError(
                    f"group {name!r}: worker_kwargs names no source — a GenroPy "
                    "worker without a site serves nothing; declare "
                    "worker_kwargs={'source': <site name or path>} in the recipe"
                )
        await super().on_startup()

    @route(media_type="text/plain")
    def metrics(self) -> str:
        """Prometheus exposition: the population counters, then the event counters.

        ``users``/``pages``/``connections`` are the exact ``len()`` of the
        commander's own indexes — the whole pool's view, kept by the lifecycle
        fold — under the legacy metric name. The commander's aggregate event
        counters (refusals, lost pendings, discards) follow as their own family.
        """
        commander = self.commander
        population = {
            "users": len(commander.user_map),
            "pages": len(commander.page_connection_map),
            "connections": len(commander.connection_user_map),
        }
        lines = [
            f'{METRIC_PREFIX}{{counter="{name}"}} {value}'
            for name, value in population.items()
        ]
        lines.extend(
            f'{EVENTS_PREFIX}{{event="{name}"}} {value}'
            for name, value in sorted(commander.counters.items())
        )
        return "\n".join(lines)
