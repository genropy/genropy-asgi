# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyWorkerApplication — the GenroPy legacy bridge as a pool child.

The same GnrWsgiSite hosting as ``GenropySpaApplication`` (the shared
``GnrSiteHostingMixin``), composed on ``SpaWorkerApplication`` instead of the single:
this application IS a worker in a multi's pool. What changes with the role, all
inherited:

- lifecycle/POST commands PRODUCED here ride up to the commander on the pool CHANNEL;
  the per-request sink the role seeds in the scope (carried into the WSGI environ by
  the hosting mixin) is an observer for the response headers (sticky_cid birth cookie,
  the login sync header);
- the datachange queues live LOCAL on this worker (switch model): the pulls
  (``subscription_storechanges``, the ping envelope) drain the page's own pending
  list — a cross-worker change was already deposited here by the commander's
  ``/datachange_in`` forward. No synchronous RPC to the commander;
- the global store rides the store rail: the legacy ``globalStore()`` Bag ships its
  leaf writes up the channel (full-path TYTX scalars, see the register client) and is
  the materialized view of the commander's master — ``/update_global`` pushes and the
  ``/store_snapshot`` late-worker seed land here (``handle_channel_message``) and are
  poured back into the Bag by the register. Coherence is eventual (one round-trip).

Spawned by the multi's worker entry:
    python -m genro_asgi...worker_entry --app-class \\
        genropy_asgi.spa.genropy_worker_application:GenropyWorkerApplication \\
        --app-arg source=<site> --commander-url http://...
"""

from __future__ import annotations

from typing import Any

from genro_asgi.applications.spa_application import SpaWorkerApplication

from .genropy_spa_application import GnrSiteHostingMixin

__all__ = ["GenropyWorkerApplication"]


class GenropyWorkerApplication(GnrSiteHostingMixin, SpaWorkerApplication):
    """Legacy-GenroPy SPA host, POOL-CHILD role: a GnrWsgiSite inside a multi's pool."""

    async def handle_channel_message(self, envelope: dict[str, Any]) -> None:
        """Apply the descending envelope, then pour the global-store pushes into the Bag.

        The base applies ``/update_global`` and ``/store_snapshot`` to the worker's
        read-only replica; here the same write is materialized into the legacy
        ``globalStore()`` Bag through the register (full-path key, TYTX scalar), so
        the legacy reads stay local and coherent with the commander's master.
        """
        await super().handle_channel_message(envelope)
        path = envelope.get("path")
        if path not in ("/update_global", "/store_snapshot"):
            return
        register = getattr(self.gnr_site, "register", None)
        if register is None:
            return
        data = envelope.get("data") or {}
        if path == "/update_global":
            register.apply_global_write(data.get("op"), data.get("key"), data.get("value"))
        else:
            register.load_global_snapshot(dict(data))


if __name__ == "__main__":
    pass
