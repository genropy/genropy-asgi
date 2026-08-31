# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Recipe of the bridge leg of the population run: the freeze, turned on.

Sister of ``l120_comparison/bridge_w4_recipe.py``. Two differences, and both are
the point of this scenario:

- the group receives ``user_idle_freeze_minutes`` from the environment. Unset,
  the core's default stands and it is ``math.inf``: NOBODY IS EVER FROZEN, and a
  run that expected a freeze would measure a population that simply stayed. The
  runner therefore requires the variable, and the driver certifies the live value
  from ``/_orchestration/status`` before populating.
- ``worker_max_users`` is NOT fixed. The topology is not the variable here: the
  pool sizes itself for two thousand users, and how it sizes itself is one of the
  things the run reports.

``worker_max_number`` is pinned at eight, the same ceiling the worker sensitivity
used, so the per-worker memory quota (the quota divided by the ceiling) is the
same in both campaigns and their figures can be put side by side.

``control_enabled=True`` is not decoration: without it ``/_orchestration`` is not
mounted at all, the freeze setpoint cannot be read back, and that path would be
forwarded to the hosted site instead.

The freeze deposit's own directory is declared explicitly through
``GNR_ASGI_FROZEN_USERS_PATH``, because the run measures its size and must know
where to look. Note that the commander wipes the deposit at every server start,
so a measure of its growth begins from empty by construction.

Absent, and none of them takes part in the measure: authentication, storage,
monitor identity, encryption keys, configuration profiles, console.

Used by ``gnrasgiserve <instance> --config <this file>``.
"""

import os
import tempfile
from typing import Any

from genro_bag.resolvers import EnvResolver

from genro_asgi.config import AsgiConfigBuilder

from genropy_asgi.spa.genropy_spa_application import GenropySpaApplication

DEBUG_OFF_WORDS = frozenset({"", "0", "false", "no", "off"})

# Lo stesso tetto della sensitivity, cosi' la quota per worker e' la stessa.
WORKER_MAX_NUMBER = 8


class ServerConfiguration(AsgiConfigBuilder):
    def main(self, root: Any) -> None:
        """The listener, the middleware, the site, and a pool that freezes."""
        cfg = root.configuration()
        cfg.server(
            host=EnvResolver("GNR_ASGI_HOST", default="127.0.0.1"),
            port=EnvResolver("GNR_ASGI_PORT", default=8000, dtype="L"),
        )
        cfg.middleware()
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
        commander = front.orchestration(control_enabled=True).commander(
            frozen_users_path=frozen_users_path,
            instance_dir=instance_dir,
            orchestration_log_path=os.environ["GNR_ASGI_ORCH_LOG"],
        )
        group_kwargs: dict[str, Any] = {
            "name": "pool",
            "entry_module": "genro_asgi.spa.orchestration.worker_entry",
            "worker_class": "genropy_asgi.spa.genropy_worker:GenropyWorker",
            "engine_factory": "genropy_asgi.spa.site_engine_factory:GenropySiteEngineFactory",
            "worker_kwargs": {"source": source, "debug": debug, "debugger": debugger},
            "engine_kwargs": {"source": source, "debug": debug},
            "worker_max_number": WORKER_MAX_NUMBER,
            # La policy CPU resta spenta: il popolamento non deve chiudere
            # l'ammissione mentre duemila utenti entrano.
            "cpu_grow_percent": None,
            "occupancy_max_percent": 80.0,
            "reception_reserved_percent": 0.0,
            "cpu_retirement_quiet_seconds": 60.0,
            "restart_occupancy_max_percent": 95.0,
        }
        # La chiave non viene passata affatto se la variabile e' assente, cosi'
        # vale il default del core. Il runner la rende obbligatoria.
        freeze_minutes = os.environ.get("GNR_ASGI_IDLE_FREEZE_MINUTES")
        if freeze_minutes:
            group_kwargs["user_idle_freeze_minutes"] = float(freeze_minutes)
        commander.groups().group(**group_kwargs)
