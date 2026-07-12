# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Standard genro-asgi configuration for serving one GenroPy instance as a SPA.

Used by the ``gnrasgiserve`` CLI: a normal multi-app ``AsgiServer`` on which the GenroPy
instance is mounted on the root. The variable elements (the resolved instance ``path``,
host/port/debug, the worker count) come from the environment; the rest of the recipe is
fixed. No register daemon in either shape: the register is served in-process.

Two shapes, chosen by ``GNR_ASGI_WORKERS``:

- ``0`` (default) — the SINGLE: one ``GenropySpaApplication`` serves the site in this
  process (commander of itself).
- ``N > 0`` — the POOL: a ``GenropyCommanderApplication`` (the commander, a
  ``SpaMultiWorkerApplication`` subclass adding the site-wide ``/metrics`` endpoint) spawns
  N worker subprocesses, each hosting a ``GenropyWorkerApplication`` on the same site path;
  the commander forwards by sticky affinity and serves the ``/_commander/*`` back-channel
  (datachange pull) at its own public URL.
"""

import os
from typing import Any

from genro_bag.resolvers import EnvResolver

from genro_asgi.config import AsgiConfigBuilder

from genropy_asgi.spa.genropy_commander_application import GenropyCommanderApplication
from genropy_asgi.spa.genropy_spa_application import GenropySpaApplication


class ServerConfiguration(AsgiConfigBuilder):
    def setup(self, data: Any) -> None:
        # The only variable elements: the resolved instance path (or name) and the
        # server address, set by the CLI through the environment.
        data["path"] = EnvResolver("GNR_ASGI_PATH")
        data["host"] = EnvResolver("GNR_ASGI_HOST", default="127.0.0.1")
        data["port"] = EnvResolver("GNR_ASGI_PORT", default=8000, dtype="L")
        data["debug"] = EnvResolver("GNR_ASGI_DEBUG", default=True, dtype="B")

    def main(self, root: Any) -> None:
        root.server(host="^host", port="^port")
        root.middleware()
        apps = root.applications(default="site")
        workers = int(os.environ.get("GNR_ASGI_WORKERS") or "0")
        if workers:
            # The commander's own public URL: the workers call the /_commander/*
            # back-channel here (the app is mounted on the root, no mount suffix).
            port = int(os.environ.get("GNR_ASGI_PORT") or "8000")
            apps.application(
                code="site",
                app_class=GenropyCommanderApplication,
                worker_app_class=(
                    "genropy_asgi.spa.genropy_worker_application:GenropyWorkerApplication"
                ),
                app_args={
                    "source": os.environ.get("GNR_ASGI_PATH", ""),
                    "debug": os.environ.get("GNR_ASGI_DEBUG", ""),
                },
                workers=workers,
                commander_url=f"http://127.0.0.1:{port}",
            )
        else:
            apps.application(
                code="site",
                app_class=GenropySpaApplication,
                source="^path",
                debug="^debug",
            )
