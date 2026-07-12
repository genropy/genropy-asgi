# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Benchmark config: the elastic multi-worker pool with occupancy-based knobs (v1.1).

The pool no longer counts heads: admission, scale-up and compaction all decide on the
worker's measured occupancy (cpu + executor + optional memory). The old per-worker user
caps (``max_users_first`` / ``max_users_other``) are gone from the app constructor; passing
them would be silently swallowed. This config exposes the v1.1 knobs instead, each overridable
from the environment so a benchmark driver can retune a scenario without editing the file.

Knobs (all attributes of the ``application`` element, forwarded to the app ``on_init``):
    workers              initial pool size (env WORKERS, default 1)
    max_workers          scale-up ceiling, None = unbounded (env MAX_WORKERS)
    min_workers          compaction floor (env MIN_WORKERS, default 1 = reception only)
    reception_threshold  the reception keeps logins under this occupancy (env, default 0.5)
    admission_threshold  other workers stop receiving logins over this (env, default 0.8)
    compaction_margin    compact when headroom H > margin * admission_threshold (env, default 1.5)
    memory_limit_mb      enables the memory occupancy component (env; None = off, correct on macOS
                         where there is no /proc rss)

Run either way:
    gnrasgiserve <instance> --config examples/multiworker_config.py -p 8081
    python -m genro_asgi serve examples/multiworker_config.py

Through the ``gnrasgiserve`` CLI the instance/host/port come from the CLI (they win over the
defaults below, read from the environment); run directly it falls back to the defaults. Serves
through a commander with an elastic pool, no register daemon.
"""

import os

from genro_asgi.config import AsgiConfigBuilder
from genro_asgi.applications.multi_worker_application import SpaMultiWorkerApplication

# The CLI writes these to the environment before loading the config, so the CLI instance
# and port win; run directly (python -m genro_asgi serve) they fall back to the defaults.
SITE = os.environ.get("GNR_ASGI_PATH") or "test_invoice_pg"
PORT = int(os.environ.get("GNR_ASGI_PORT") or 8081)


def _int_env(name, default):
    value = os.environ.get(name)
    return default if value in (None, "") else int(value)


def _float_env(name, default):
    value = os.environ.get(name)
    return default if value in (None, "") else float(value)


# Elastic-pool knobs (v1.1). Defaults match the contract; the benchmark driver overrides via env.
WORKERS = _int_env("WORKERS", 1)
MAX_WORKERS = _int_env("MAX_WORKERS", None)          # None = unbounded scale-up
MIN_WORKERS = _int_env("MIN_WORKERS", 1)
RECEPTION_THRESHOLD = _float_env("RECEPTION_THRESHOLD", 0.5)
ADMISSION_THRESHOLD = _float_env("ADMISSION_THRESHOLD", 0.8)
COMPACTION_MARGIN = _float_env("COMPACTION_MARGIN", 1.5)
MEMORY_LIMIT_MB = _int_env("MEMORY_LIMIT_MB", None)  # None = memory component off (macOS: no /proc)


class ServerConfiguration(AsgiConfigBuilder):
    def main(self, root):
        root.server(host="127.0.0.1", port=PORT)
        root.middleware()
        apps = root.applications(default="site")
        apps.application(
            code="site",
            app_class=SpaMultiWorkerApplication,
            worker_app_class=(
                "genropy_asgi.spa.genropy_worker_application:GenropyWorkerApplication"
            ),
            app_args={"source": SITE, "debug": ""},
            workers=WORKERS,
            max_workers=MAX_WORKERS,
            min_workers=MIN_WORKERS,
            reception_threshold=RECEPTION_THRESHOLD,
            admission_threshold=ADMISSION_THRESHOLD,
            compaction_margin=COMPACTION_MARGIN,
            memory_limit_mb=MEMORY_LIMIT_MB,
            commander_url=f"http://127.0.0.1:{PORT}",
        )
