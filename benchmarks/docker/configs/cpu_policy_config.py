# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Lab recipe: the standard bridge recipe plus the pool thresholds, env-driven.

A copy of ``genropy_asgi/spa/config.py`` for the measurement lab ONLY. The one
difference: the group accepts the orchestration thresholds from the
environment, so a benchmark can enable the experimental CPU growth policy
without touching the package recipe. Unset variables leave the core defaults —
with them all unset this recipe builds the same pool as the standard one.

The variables, each mapping 1:1 onto the core grammar word of the same name:

- ``GNR_ASGI_CPU_GROW_PERCENT``            -> ``cpu_grow_percent``
- ``GNR_ASGI_CPU_GROW_REARM_PERCENT``      -> ``cpu_grow_rearm_percent``
- ``GNR_ASGI_OCCUPANCY_MAX_PERCENT``       -> ``occupancy_max_percent``
- ``GNR_ASGI_RECEPTION_RESERVED_PERCENT``  -> ``reception_reserved_percent``
- ``GNR_ASGI_ORCH_LOG``                    -> ``orchestration_log_path``
  (commander word: the orders land in a file the host can collect)

The lab also opens the core's own monitor, which lives at
``/_server/monitor/`` in every server and is gated ``SERVER_ADMIN``. Three
variables, all REQUIRED — a missing one is a boot error, never a server that
comes up open:

- ``GNR_ASGI_MONITOR_STORAGE`` -> the ``monitor`` mount's ``base_path``, a
  directory of the host's runtime, so the identity records survive a restart;
- ``GNR_ASGI_STORAGE_KEY``     -> ``storage_key``, the at-rest key material the
  user and token records are written under;
- ``GNR_ASGI_ADMIN_PASSWORD``  -> ``admin_password``, the bootstrap admin's
  password. ``AuthMixin`` gives that identity both ``SUPERADMIN`` and
  ``SERVER_ADMIN``, which is what opens the monitor.

None of the three is ever written here: they arrive as ``EnvResolver`` values,
which is also what the grammar's signature demands of ``admin_password``.

Used via ``gnrasgiserve <instance> --config <this file>``.
"""

import os
import tempfile
from typing import Any

from genro_bag.resolvers import EnvResolver
from genro_storage import StorageManager

from genro_asgi.applications.spa_console import SpaConsoleMcpApplication
from genro_asgi.config import AsgiConfigBuilder

from genropy_asgi.spa.genropy_spa_application import GenropySpaApplication

# The words that turn the debug flag OFF when GNR_ASGI_DEBUG carries one.
DEBUG_OFF_WORDS = frozenset({"", "0", "false", "no", "off"})

# Group threshold words and the environment variables that may carry them.
GROUP_THRESHOLD_ENV = {
    "cpu_grow_percent": "GNR_ASGI_CPU_GROW_PERCENT",
    "cpu_grow_rearm_percent": "GNR_ASGI_CPU_GROW_REARM_PERCENT",
    "occupancy_max_percent": "GNR_ASGI_OCCUPANCY_MAX_PERCENT",
    "reception_reserved_percent": "GNR_ASGI_RECEPTION_RESERVED_PERCENT",
}


class ServerConfiguration(AsgiConfigBuilder):
    def monitor_identity(self, cfg: Any) -> None:
        """Storage and identity, so the core's monitor answers an admin and nobody else.

        Only the ``monitor`` mount is declared here: ``site`` comes from the
        layer underneath, on the deployment directory, and is left exactly as it
        was — the measured stack must not change because the monitor was
        opened. ``monitor`` is a directory of the host's runtime, so the admin
        record survives a container restart.

        The stores are named rather than defaulted: ``users`` and ``tokens``
        both on ``monitor``, so nothing about identity is written into the
        deployment directory the site itself uses.
        """
        storage_dir = os.environ["GNR_ASGI_MONITOR_STORAGE"]
        storage = cfg.storage(
            app=StorageManager, storage_key=EnvResolver("GNR_ASGI_STORAGE_KEY")
        )
        storage.local(name="monitor", base_path=storage_dir)
        authentication = cfg.authentication()
        authentication.admin_password(EnvResolver("GNR_ASGI_ADMIN_PASSWORD"))
        authentication.users(mount="monitor", prefix="users")
        authentication.tokens(mount="monitor", prefix="api_keys")

    def main(self, root: Any) -> None:
        """The standard bridge recipe, plus the env-driven pool thresholds."""
        cfg = root.configuration()
        cfg.server(
            host=EnvResolver("GNR_ASGI_HOST", default="127.0.0.1"),
            port=EnvResolver("GNR_ASGI_PORT", default=8000, dtype="L"),
        )
        cfg.middleware()
        self.monitor_identity(cfg)
        source = os.environ.get("GNR_ASGI_PATH") or ""
        debug_env = os.environ.get("GNR_ASGI_DEBUG")
        debug = True if debug_env is None else debug_env.strip().lower() not in DEBUG_OFF_WORDS
        debugger = bool(os.environ.get("GNR_ASGI_DEBUGGER"))
        site_key = os.path.basename(os.path.normpath(source)) or "site"
        frozen_users_path = os.environ.get("GNR_ASGI_FROZEN_USERS_PATH") or os.path.join(
            source, "data", "_frozen_users"
        )
        instance_dir = os.environ.get("GNR_ASGI_INSTANCE_DIR") or os.path.join(
            tempfile.gettempdir(), f"gnrasgi_{site_key}"
        )
        applications = cfg.applications()
        front = applications.application(
            code="site",
            mount="",
            app_class=GenropySpaApplication,
        )
        if os.environ.get("GNR_ASGI_CONSOLE"):
            applications.application(
                code="console",
                mount="_console",
                app_class=SpaConsoleMcpApplication,
            )
        commander_kwargs: dict[str, Any] = {
            "frozen_users_path": frozen_users_path,
            "instance_dir": instance_dir,
        }
        orders_path = os.environ.get("GNR_ASGI_ORCH_LOG")
        if orders_path:
            commander_kwargs["orchestration_log_path"] = orders_path
        commander = front.orchestration().commander(**commander_kwargs)
        group_kwargs: dict[str, Any] = {
            "name": "pool",
            "entry_module": "genro_asgi.spa.orchestration.worker_entry",
            "worker_class": "genropy_asgi.spa.genropy_worker:GenropyWorker",
            "worker_kwargs": {"source": source, "debug": debug, "debugger": debugger},
            "engine_factory": "genropy_asgi.spa.site_engine_factory:GenropySiteEngineFactory",
            "engine_kwargs": {"source": source, "debug": debug},
        }
        idle_minutes = os.environ.get("GNR_ASGI_IDLE_FREEZE_MINUTES")
        if idle_minutes:
            group_kwargs["user_idle_freeze_minutes"] = float(idle_minutes)
        max_users = os.environ.get("GNR_ASGI_WORKER_MAX_USERS")
        if max_users:
            group_kwargs["worker_max_users"] = int(max_users)
        for word, variable in GROUP_THRESHOLD_ENV.items():
            value = os.environ.get(variable)
            if value:
                group_kwargs[word] = float(value)
        commander.groups().group(**group_kwargs)
