# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Standard genro-asgi configuration for serving one GenroPy instance as a SPA.

Used by the ``gnrasgiserve`` CLI: a normal multi-app ``AsgiServer`` on which the
GenroPy instance is mounted on the root. The variable elements (the resolved
instance ``path``, host/port/debug, the installation paths, the idle valve)
come from the environment; the rest of the recipe is fixed. No register daemon
in either shape: the register is served in-process, on each worker.

ONE shape: a single ``GenropySpaApplication`` whose pool is declared here, on
its ``commander`` section — one group named ``pool`` (the cemented decision:
one group, the reception serves the guests). There is no worker count to
declare and no single/pool selector: the pool always runs and sizes itself.
A ``GNR_ASGI_WORKERS`` still set in the environment is reported by the CLI
and ignored.

The environment enters the tree as VALUES read at recipe-build time: the CLI
writes the variables just before the server is built, and ``main()`` runs
during that build. ``EnvResolver`` stays only where the core grammar declares
resolver-typed attributes (host/port).

The two installation paths and the valve, overridable per installation:

- ``GNR_ASGI_FROZEN_USERS_PATH`` — the freezer root; durable by necessity
  (a frozen user is kept for days), so it defaults INSIDE the site's own
  ``data`` directory.
- ``GNR_ASGI_INSTANCE_DIR`` — where the workers' sockets live; ephemeral,
  so it defaults under the system temp directory (UDS paths must stay
  short).
- ``GNR_ASGI_IDLE_FREEZE_MINUTES`` — the silence past which a user is
  parked in the freezer. Unset, the worker reads the site's ``<cleanup>``
  section (``connection_max_age`` seconds, 7200 where the site is silent).
- ``GNR_ASGI_CONSOLE`` — set to any value, the pool's debug door is mounted
  on ``_console`` as MCP tools: an expression is evaluated inside the
  commander or inside a named worker, and its ``repr`` comes back. It reads
  the live registers WITHOUT going through the site, so looking does not mint
  a cid nor open a connection — an observer that leaves no trace in what it
  observes. The door is full ``eval`` by construction, so mounting IS the
  gate (core doctrine): unset here means the door does not exist, and a
  production environment never sets it.
"""

import os
import tempfile
from typing import Any

from genro_bag.resolvers import EnvResolver

from genro_asgi.applications.spa_console import SpaConsoleMcpApplication
from genro_asgi.config import AsgiConfigBuilder

from genropy_asgi.spa.genropy_spa_application import GenropySpaApplication

# The words that turn the debug flag OFF when GNR_ASGI_DEBUG carries one.
DEBUG_OFF_WORDS = frozenset({"", "0", "false", "no", "off"})


class ServerConfiguration(AsgiConfigBuilder):
    def main(self, root: Any) -> None:
        """The one document: the listener, the middleware, the site and its pool."""
        cfg = root.configuration()
        cfg.server(
            host=EnvResolver("GNR_ASGI_HOST", default="127.0.0.1"),
            port=EnvResolver("GNR_ASGI_PORT", default=8000, dtype="L"),
        )
        cfg.middleware()
        source = os.environ.get("GNR_ASGI_PATH") or ""
        # Unset means the dev default (True), exactly as the pre-rebase recipe
        # read it; the CLI's --nodebug writes the empty string. A value is read
        # as a word, never as a truthy string: "false"/"0"/"no"/"off" mean OFF,
        # and getting that wrong would serve the site wrapped in the Werkzeug
        # debugger AND change what the site measures (the SQL time counters are
        # incremented only under debug).
        debug_env = os.environ.get("GNR_ASGI_DEBUG")
        debug = True if debug_env is None else debug_env.strip().lower() not in DEBUG_OFF_WORDS
        site_key = os.path.basename(os.path.normpath(source)) or "site"
        frozen_users_path = os.environ.get("GNR_ASGI_FROZEN_USERS_PATH") or os.path.join(
            source, "data", "_frozen_users"
        )
        instance_dir = os.environ.get("GNR_ASGI_INSTANCE_DIR") or os.path.join(
            tempfile.gettempdir(), f"gnrasgi_{site_key}"
        )
        # mount="" IS the site root: a GenroPy site owns its absolute URLs
        # (/_rsrc, /sys, the dojo tree), so it cannot live under a /site prefix.
        applications = cfg.applications()
        front = applications.application(
            code="site",
            mount="",
            app_class=GenropySpaApplication,
        )
        if os.environ.get("GNR_ASGI_CONSOLE"):
            # The first path segment decides the app, so the door answers on
            # /_console while the site keeps every other URL of the root mount.
            applications.application(
                code="console",
                mount="_console",
                app_class=SpaConsoleMcpApplication,
            )
        commander = front.commander(
            frozen_users_path=frozen_users_path,
            instance_dir=instance_dir,
        )
        group_kwargs: dict[str, Any] = {
            "name": "pool",
            "entry_module": "genro_asgi.spa.orchestration.worker_entry",
            "worker_class": "genropy_asgi.spa.genropy_worker:GenropyWorker",
            "worker_kwargs": {"source": source, "debug": debug},
        }
        idle_minutes = os.environ.get("GNR_ASGI_IDLE_FREEZE_MINUTES")
        if idle_minutes:
            group_kwargs["user_idle_freeze_minutes"] = float(idle_minutes)
        commander.groups().group(**group_kwargs)
