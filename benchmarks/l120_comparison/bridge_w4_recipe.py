# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Recipe of the bridge leg of the L120 comparison: four workers, twelve users each.

Sister of ``worker_sensitivity/prep_config.py``, and it differs from it in one
place only: ``worker_max_users`` is fixed at twelve instead of arriving from the
environment. This run has no variable to sweep — W4 is the configuration the
sensitivity chose, and the comparison is against Gunicorn, not against another
worker count.

Absent on purpose, and none of them takes part in the measure: authentication,
storage, monitor identity, encryption keys, configuration profiles, freeze.

THE CONSOLE IS MOUNTED, and only when ``GNR_ASGI_CONSOLE`` is set — mounting IS
the gate. It is the read door the page-class-cache certification uses, and it is
NOT in the measured path: the front's demux diverts ``_console`` on the first
path segment, before the hosted site, and the certification runs between the
warmup and the measured window. No counter and no wrapper is added anywhere.

Two knobs still come from the environment, because the runner must decide them
before the container exists:

- ``GNR_ASGI_ORCH_LOG`` — the journal's path. The runner gives it the run's own
  prefix, so the human log and its ``.decisions.jsonl`` are BORN with their final
  names. Nothing is ever renamed while the bridge holds it open.
- ``GNR_ASGI_PATH`` and the debug words — the site the pool serves, as in every
  recipe.

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

# I quattro worker della corsa: 48 utenti, 12 per worker.
WORKER_MAX_USERS = 12
WORKER_MAX_NUMBER = 8


class ServerConfiguration(AsgiConfigBuilder):
    def main(self, root: Any) -> None:
        """The listener, the middleware, the site and its pool of four."""
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
            # POPOLAMENTO: la policy CPU e' spenta, cosi' l'ammissione non si
            # chiude durante i login. La policy della misura entra dopo, a caldo,
            # con POST /_orchestration/apply.
            cpu_grow_percent=None,
            # CONTROLLO SPERIMENTALE, non una configurazione di produzione: il
            # retirement non deve poter chiudere un worker durante la corsa,
            # altrimenti la topologia cambierebbe da sola a meta' misura.
            worker_min_life_seconds=3600.0,
            occupancy_max_percent=80.0,
            reception_reserved_percent=0.0,
            cpu_retirement_quiet_seconds=60.0,
            restart_occupancy_max_percent=95.0,
            worker_max_users=WORKER_MAX_USERS,
        )
