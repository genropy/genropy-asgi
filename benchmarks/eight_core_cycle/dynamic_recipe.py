# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Recipe of the bridge with its REAL orchestration: the pool decides its own size.

Sister of ``cycle_recipe.py``, and the difference is the whole point of the run.
``cycle_recipe.py`` describes a shape decided in advance — growth by CPU pressure
off, fifteen users per worker, exactly eight workers — and that measurement stays
valid as a controlled point. It is not, however, how the pool runs in production.

WHAT IS DECLARED HERE, and it is the production policy verbatim:

- ``cpu_grow_percent=50`` with ``cpu_grow_rearm_percent=30``: growth by CPU
  pressure is ON, with hysteresis;
- ``occupancy_max_percent=80``, ``reception_reserved_percent=0``;
- ``cpu_retirement_quiet_seconds=60``, ``restart_occupancy_max_percent=95``.

WHAT IS DELIBERATELY NOT DECLARED, because declaring it would decide the answer:

- ``worker_max_users``. The core's default is infinity, so a worker fills up by
  OCCUPANCY, not by a counted cap. With ``new_user_occupancy_percent`` at its
  default of five, a user costs five per cent of a worker and eighty per cent is
  full: sixteen users. The pool derives that; nobody writes it here.
- ``worker_min_life_seconds``. The fixed run set it to an hour to stop retirement
  from changing the topology mid-measure — an experimental control, never a
  production value. Here the core's own sixty seconds apply and retirement is part
  of what is being measured.

THE ONE NUMBER THAT HAD TO BE WRITTEN, and why it is a ceiling and not a target:
``worker_max_number``. The core's default is SIX, which is fewer than the eight
the fixed run used, so leaving it alone would cap the growth below the shape
already measured — the opposite of letting the pool choose. It is set to sixteen:
twice the fixed run's eight, and twice the eight that occupancy alone predicts for
a hundred and twenty users. The run reports the maximum reached, so a result that
touches sixteen is a result that hit the ceiling and must be read as capped.

The ceiling has a second effect worth knowing: the core derives
``worker_memory_max_percent`` as ``100 / worker_max_number`` unless told
otherwise. At sixteen that is 6.25% of four gibibytes, about 256 MB per worker,
against the fifty to sixty megabytes a worker actually held in the fixed run —
four times the room. Raising the ceiling further would shrink that allowance, so
sixteen is also where the two constraints sit comfortably apart.

Absent on purpose, none of them part of the measure: authentication, storage,
monitor identity, encryption keys, configuration profiles, freeze.

THE CONSOLE IS MOUNTED, and only when ``GNR_ASGI_CONSOLE`` is set — mounting IS
the gate. It is the read door the page-class-cache certification uses, and the
demux diverts ``_console`` before the hosted site.

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

# Il tetto, non un obiettivo: il default del core e' sei, piu' basso degli otto
# gia' misurati. Sedici lascia il doppio di margine e tiene l'allowance di memoria
# per worker a 256 MB, quattro volte l'impronta osservata.
WORKER_MAX_NUMBER_CEILING = 16


class ServerConfiguration(AsgiConfigBuilder):
    def main(self, root: Any) -> None:
        """The listener, the middleware, the site and a pool that sizes itself."""
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
        ceiling = int(os.environ.get("GNR_ASGI_WORKER_MAX_NUMBER")
                      or WORKER_MAX_NUMBER_CEILING)
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
            # LA POLICY REALE, accesa.
            cpu_grow_percent=50.0,
            cpu_grow_rearm_percent=30.0,
            occupancy_max_percent=80.0,
            reception_reserved_percent=0.0,
            cpu_retirement_quiet_seconds=60.0,
            restart_occupancy_max_percent=95.0,
            # Il tetto. worker_max_users e worker_min_life_seconds NON si
            # dichiarano: l'infinito e i sessanta secondi del core sono cio' che
            # la produzione usa, e il numero dei worker deve restare un risultato.
            worker_max_number=ceiling,
        )
