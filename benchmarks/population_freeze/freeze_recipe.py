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

``worker_max_number`` is a CEILING read from the environment, not a target: the
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
# Il tetto di default. Il runner lo alza per la prova a duemila utenti, dove la
# rampa arriva a cinquecento attivi: a sedici utenti per worker per occupazione
# servirebbero circa trentadue worker, quindi un tetto di otto sarebbe il vincolo
# invece della capacita'. La corsa riporta il massimo raggiunto.
#
# Il tetto ha un secondo effetto, dichiarato perche' non e' ovvio: il core deriva
# `worker_memory_max_percent` come 100 / worker_max_number. A trentadue sono il
# 3,1% del limite del container — 768 MB su ventiquattro gibibyte, contro i 50-60
# MB che un worker teneva davvero. Il margine resta ampio.
DEFAULT_WORKER_MAX_NUMBER = 8


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
        worker_max_number = int(os.environ.get("GNR_ASGI_WORKER_MAX_NUMBER")
                                or DEFAULT_WORKER_MAX_NUMBER)
        group_kwargs: dict[str, Any] = {
            "name": "pool",
            "entry_module": "genro_asgi.spa.orchestration.worker_entry",
            "worker_class": "genropy_asgi.spa.genropy_worker:GenropyWorker",
            "engine_factory": "genropy_asgi.spa.site_engine_factory:GenropySiteEngineFactory",
            "worker_kwargs": {"source": source, "debug": debug, "debugger": debugger},
            "engine_kwargs": {"source": source, "debug": debug},
            # IL TETTO, non un obiettivo. Il default del core e' sei: lasciarlo
            # limiterebbe la crescita sotto la forma gia' misurata a otto. Il
            # runner lo alza per la prova a duemila utenti, e la corsa riporta il
            # massimo raggiunto: un risultato che tocca il tetto e' un risultato
            # limitato, e va letto come tale.
            "worker_max_number": worker_max_number,
            # LA POLICY REALE, accesa: e' la stessa del ciclo a otto core, dove il
            # pool ha scelto da se' otto worker senza che una sola nascita venisse
            # dalla scansione CPU. worker_max_users e worker_min_life_seconds NON
            # si dichiarano: l'infinito e i sessanta secondi del core sono cio' che
            # la produzione usa, e il numero dei worker deve restare un risultato.
            "cpu_grow_percent": 50.0,
            "cpu_grow_rearm_percent": 30.0,
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
