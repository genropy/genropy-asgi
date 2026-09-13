# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Laboratory profile with CPU admission hysteresis and a dynamic worker pool.

Admission closes at 50% CPU and reopens at 30%. CPU offload is disabled so
paused users remain resident during this population cycle. Memory admission
uses 80%, retirement quiet time is 60 seconds, and restart occupancy is 95%.
There is no counted user cap. The worker ceiling defaults to sixteen and is a
limit, not a target; runs reaching it must be interpreted as capped.

This explicit experiment profile is not a claim about production defaults or
historical software equivalence. The fixed recipe supplies the controlled
comparison. Idle freeze and minimum worker life retain current core defaults.
The optional console exposes observations needed by cache certification.
"""

import os
import tempfile
from typing import Any

from genro_bag.resolvers import EnvResolver

from genro_asgi_multiworker_spa.spa_console import SpaConsoleMcpApplication
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
            entry_module="genro_asgi_multiworker_spa.orchestration.worker_entry",
            worker_class="genropy_asgi.spa.genropy_worker:GenropyWorker",
            engine_factory="genropy_asgi.spa.site_engine_factory:GenropySiteEngineFactory",
            worker_kwargs={"source": source, "debug": debug, "debugger": debugger},
            engine_kwargs={"source": source, "debug": debug},
            # LA POLICY REALE, accesa.
            cpu_admission_close_percent=50.0,
            cpu_admission_reopen_percent=30.0,
            worker_memory_admission_percent=80.0,
            cpu_offload_percent=None,
            cpu_retirement_quiet_seconds=60.0,
            restart_occupancy_max_percent=95.0,
            # Il tetto. worker_max_users e worker_min_life_seconds NON si
            # dichiarano: l'infinito e i sessanta secondi del core sono cio' che
            # la produzione usa, e il numero dei worker deve restare un risultato.
            worker_max_number=ceiling,
        )
