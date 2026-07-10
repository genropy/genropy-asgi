# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyWorkerApplication — the GenroPy legacy bridge as a pool child.

The same GnrWsgiSite hosting as ``GenropySpaApplication`` (the shared
``GnrSiteHostingMixin``), composed on ``SpaWorkerApplication`` instead of the single:
this application IS a worker in a multi's pool. What changes with the role, all
inherited — nothing legacy-specific to add:

- lifecycle/POST commands PRODUCED here ride up to the commander on the pool CHANNEL;
  the per-request sink the role seeds in the scope (carried into the WSGI environ by
  the hosting mixin) is an observer for the response headers (sticky_cid birth cookie,
  the login sync header);
- the datachange queues live LOCAL on this worker (switch model): the pulls
  (``subscription_storechanges``, the ping envelope) drain the page's own pending
  list — a cross-worker change was already deposited here by the commander's
  ``/datachange_in`` forward. No synchronous RPC to the commander;
- the global store is a replica the commander pushes (NB: the legacy ``globalStore()``
  Bag is still process-local here — the replica bridge is an open follow-up, so
  cross-worker global reads are NOT coherent yet, PROVISIONAL).

Spawned by the multi's worker entry:
    python -m genro_asgi...worker_entry --app-class \\
        genropy_asgi.spa.genropy_worker_application:GenropyWorkerApplication \\
        --app-arg source=<site> --commander-url http://...
"""

from __future__ import annotations

from genro_asgi.applications.spa_application import SpaWorkerApplication

from .genropy_spa_application import GnrSiteHostingMixin

__all__ = ["GenropyWorkerApplication"]


class GenropyWorkerApplication(GnrSiteHostingMixin, SpaWorkerApplication):
    """Legacy-GenroPy SPA host, POOL-CHILD role: a GnrWsgiSite inside a multi's pool."""


if __name__ == "__main__":
    pass
