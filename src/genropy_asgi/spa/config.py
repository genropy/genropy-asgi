# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Standard genro-asgi configuration for serving one GenroPy instance as a SPA.

Used by the ``gnrasgiserve`` CLI: a normal multi-app ``AsgiServer`` on which the
GenroPy instance is mounted on the root. The variable elements (the resolved
instance ``path``, host/port/debug, the worker count) come from the environment;
the rest of the recipe is fixed. No register daemon in either shape: the register
is served in-process, on each worker.

ONE shape, whatever ``GNR_ASGI_WORKERS`` says: a single ``GenropySpaApplication``
owning the user-sticky pool. ``0`` (the default) is the SINGLE — the commander
holds its one worker in this process (``local_worker``); ``N > 0`` spawns N
worker subprocesses, each hosting the same site behind the core ``worker_entry``.
"""

import os
from typing import Any

from genro_bag.resolvers import EnvResolver

from genro_asgi.config import AsgiConfigBuilder

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
        apps.application(
            code="site",
            app_class=GenropySpaApplication,
            source="^path",
            debug="^debug",
            workers=workers,
            local_worker=(workers == 0),
        )
