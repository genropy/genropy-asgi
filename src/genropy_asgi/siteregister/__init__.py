# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""genropy_asgi.siteregister — the daemonless register the legacy imports.

This subpackage is the provider of the ``gnr.web:daemon`` entry-point: the legacy
``gnr.web.daemon`` switcher installs it as ``gnr.web.daemon`` in ``sys.modules``, so
``from gnr.web.daemon.siteregister_client import SiteRegisterClient`` and the handful of
other legacy imports resolve to the files here — with no register daemon behind them.

The register is served entirely in-process by ``GenropyRegisterClient`` (the SPA
application's own registries/surface/mailbox). The ``handler`` / ``service`` /
``processes`` modules are fakes: they exist only so the ``gnr.web.daemon.*`` namespace
resolves for the daemon-CLI / ``app.gnrdaemon`` paths (never reached in the request
path), and raise if actually used — there is no daemon in this build.
"""

__all__ = ["SiteRegisterClient", "GenropyRegisterClient"]

from .siteregister_client import GenropyRegisterClient, SiteRegisterClient
