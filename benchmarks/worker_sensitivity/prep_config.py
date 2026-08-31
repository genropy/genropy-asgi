# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Recipe della sensitivity sul numero di worker. Sorella della recipe di memoria.

Rispetto a ``genropy_asgi/spa/config.py`` cambia in due punti soli:

- il commander riceve ``orchestration_log_path``, che apre il registro delle
  decisioni ``<stem>.decisions.jsonl`` accanto al log umano;
- il gruppo riceve le sei soglie della policy validata nella Prova 2, scritte
  come valori fissi;
- il gruppo riceve ``worker_max_number=8``, uguale in tutte le corse cosi' che
  il ceiling per worker (quota/8) non cambi, e ``worker_max_users`` dalla
  variabile GNR_ASGI_WORKER_MAX_USERS: e' l'UNICA leva che varia fra W1, W2,
  W4 e W8. Non impostata, vale il default del core (nessun limite): e' la
  configurazione W1.

Tutto il resto e' identico alla recipe standard. Non sono presenti
authentication, storage, monitor identity, chiavi di cifratura, profili di
configurazione, console e freeze: nessuna di queste funzioni partecipa alla
misura della memoria.
"""

import os
import tempfile
from typing import Any

from genro_bag.resolvers import EnvResolver

from genro_asgi.config import AsgiConfigBuilder

from genropy_asgi.spa.genropy_spa_application import GenropySpaApplication

DEBUG_OFF_WORDS = frozenset({"", "0", "false", "no", "off"})


class ServerConfiguration(AsgiConfigBuilder):
    def main(self, root: Any) -> None:
        """Il listener, la middleware, il sito e il suo pool. Nulla altro."""
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
            orchestration_log_path=os.environ.get("GNR_ASGI_ORCH_LOG")
            or "/lab/runtime/orders.log",
        )
        commander.groups().group(
            name="pool",
            entry_module="genro_asgi.spa.orchestration.worker_entry",
            worker_class="genropy_asgi.spa.genropy_worker:GenropyWorker",
            worker_kwargs={"source": source, "debug": debug, "debugger": debugger},
            engine_factory="genropy_asgi.spa.site_engine_factory:GenropySiteEngineFactory",
            engine_kwargs={"source": source, "debug": debug},
            worker_max_number=8,
            # POPOLAMENTO: la policy CPU e' spenta, cosi' l'ammissione non si
            # chiude durante i login e nessun worker nasce per placement oltre
            # quelli che worker_max_users impone. Non e' la configurazione della
            # misura: quella entra dopo, con POST /_orchestration/apply.
            cpu_grow_percent=None,
            # CONTROLLO SPERIMENTALE, non una proposta di produzione: il
            # retirement non deve poter chiudere un worker durante la corsa,
            # altrimenti la topologia — che qui e' la variabile controllata —
            # cambierebbe da sola. 3600 s copre l'intero esperimento. Non tocca
            # placement, ammissione CPU, crescita per domanda concreta,
            # accounting di memoria ne' il percorso delle richieste.
            worker_min_life_seconds=3600.0,
            occupancy_max_percent=80.0,
            reception_reserved_percent=0.0,
            cpu_retirement_quiet_seconds=60.0,
            restart_occupancy_max_percent=95.0,
            **(
                {"worker_max_users": int(os.environ["GNR_ASGI_WORKER_MAX_USERS"])}
                if os.environ.get("GNR_ASGI_WORKER_MAX_USERS")
                else {}
            ),
        )
