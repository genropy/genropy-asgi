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

The environment enters the tree as ``EnvResolver`` VALUES sitting in the element
attributes — the shape the core's own recipes use (``BaseConfiguration``) and the
one its grammar types declare (``port: int | BagResolver``). The read door
resolves such a resolver transparently and AT READ TIME, so the recipe follows
the environment the CLI writes just before the server is built. The builder
datastore plus ``^name`` pointers is a different mechanism: the configuration read
stack never dereferences those strings, and the grammar rejects them outright.
"""

import os
from typing import Any

from genro_bag.resolvers import EnvResolver

from genro_asgi.config import AsgiConfigBuilder

from genropy_asgi.spa.genropy_spa_application import GenropySpaApplication


class ServerConfiguration(AsgiConfigBuilder):
    def main(self, root: Any) -> None:
        """The one document: the listener, the middleware and the site on the root."""
        cfg = root.configuration()
        cfg.server(
            host=EnvResolver("GNR_ASGI_HOST", default="127.0.0.1"),
            port=EnvResolver("GNR_ASGI_PORT", default=8000, dtype="L"),
        )
        cfg.middleware()
        workers = int(os.environ.get("GNR_ASGI_WORKERS") or "0")
        # mount="" IS the site root: a GenroPy site owns its absolute URLs
        # (/_rsrc, /sys, the dojo tree), so it cannot live under a /site prefix.
        cfg.applications().application(
            code="site",
            mount="",
            app_class=GenropySpaApplication,
            source=EnvResolver("GNR_ASGI_PATH"),
            debug=EnvResolver("GNR_ASGI_DEBUG", default=True, dtype="B"),
            workers=workers,
            local_worker=(workers == 0),
        )
