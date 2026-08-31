# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Recipe of the bridge leg of the eight-core cycle: eight workers, fifteen users each.

Sister of ``l120_comparison/bridge_w4_recipe.py``. Three decisions differ, and
all three are what the mandate of this run asks for:

- ``worker_max_number`` is EIGHT, and ``worker_max_users`` arrives from the
  environment with fifteen as its default. Fifteen times eight is the hundred and
  twenty users of the full run; the smoke sets two, so sixteen users occupy the
  same eight workers and the topology under measure stays the same.
- ``cpu_grow_percent`` is None for the WHOLE run, not only for the population.
  There is no hot apply here: growth by CPU pressure never engages, so a worker is
  born only when a placement has no room anywhere else. That is the one thing
  allowed to create a worker.
- the FREEZE IS ABSENT, which means never. The sixty-second pause of fifty users
  must leave them resident on their worker: a freeze would move them to disk and
  the pause would measure the freezer instead of the residency. Absence is the
  configuration — ``user_idle_freeze_minutes`` stays at the core's infinity, and
  the driver certifies that it reads back as null before measuring anything.

``worker_min_life_seconds`` is an EXPERIMENTAL CONTROL, not a production value:
retirement must not be able to close a worker in the middle of the cycle and
change the topology by itself.

Absent on purpose, and none of them takes part in the measure: authentication,
storage, monitor identity, encryption keys, configuration profiles.

THE CONSOLE IS MOUNTED, and only when ``GNR_ASGI_CONSOLE`` is set — mounting IS
the gate. It is the read door the page-class-cache certification uses, and it is
NOT in the measured path: the front's demux diverts ``_console`` on the first
path segment, before the hosted site.

Used by ``gnrasgiserve <instance> --config <this file>``.
"""

import os
import tempfile
from typing import Any

from genro_bag.resolvers import EnvResolver

from genro_asgi.applications.spa_console import SpaConsoleMcpApplication
from genro_asgi.config import AsgiConfigBuilder

from genropy_asgi.spa.genropy_spa_application import GenropySpaApplication

DEBUG_OFF_WORDS = frozenset({"", "0", "false", "no", "off"})

# Gli otto worker della corsa. worker_max_users arriva dall'ambiente, con la
# variabile che il laboratorio usa gia' — GNR_ASGI_WORKER_MAX_USERS, la stessa
# leva della worker sensitivity — perche' lo smoke mette due utenti per worker
# invece di quindici, sulla stessa topologia di otto processi.
WORKER_MAX_NUMBER = 8
DEFAULT_WORKER_MAX_USERS = 15


class ServerConfiguration(AsgiConfigBuilder):
    def main(self, root: Any) -> None:
        """The listener, the middleware, the site and its pool of eight."""
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
        max_users = int(os.environ.get("GNR_ASGI_WORKER_MAX_USERS")
                        or DEFAULT_WORKER_MAX_USERS)
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
        # La porta di lettura della certificazione. Montata solo se chiesta.
        if os.environ.get("GNR_ASGI_CONSOLE"):
            applications.application(
                code="console",
                mount="_console",
                app_class=SpaConsoleMcpApplication,
            )
        commander = front.orchestration(control_enabled=True).commander(
            frozen_users_path=frozen_users_path,
            instance_dir=instance_dir,
            orchestration_log_path=os.environ["GNR_ASGI_ORCH_LOG"],
        )
        commander.groups().group(
            name="pool",
            entry_module="genro_asgi.spa.orchestration.worker_entry",
            worker_class="genropy_asgi.spa.genropy_worker:GenropyWorker",
            engine_factory="genropy_asgi.spa.site_engine_factory:GenropySiteEngineFactory",
            worker_kwargs={"source": source, "debug": debug, "debugger": debugger},
            engine_kwargs={"source": source, "debug": debug},
            worker_max_number=WORKER_MAX_NUMBER,
            # SPENTA PER TUTTA LA CORSA: un worker nasce solo dalla domanda
            # concreta di un placement che non trova posto altrove.
            cpu_grow_percent=None,
            # CONTROLLO SPERIMENTALE: il retirement non deve poter chiudere un
            # worker a meta' ciclo e cambiare la topologia da se'.
            worker_min_life_seconds=3600.0,
            occupancy_max_percent=80.0,
            reception_reserved_percent=0.0,
            cpu_retirement_quiet_seconds=60.0,
            restart_occupancy_max_percent=95.0,
            worker_max_users=max_users,
        )
