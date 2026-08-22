# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""SUPERSEDED (2026-08-22): this example configures the pre-rebase front
(workers=, memory_limit_mb) whose API no longer exists. The pool is now
configured by the recipe in genropy_asgi/spa/config.py. Kept only as a
historical reference until removal is decided.

Benchmark config: the elastic pool with its occupancy knobs exposed to the environment.

The shipped recipe (``genropy_asgi.spa.config``) reads only the worker count; this one
exposes the pool's decision knobs as well, each overridable from the environment, so a
benchmark driver can retune a scenario without editing the file. Nothing counts heads:
admission, scale-up and compaction all decide on a worker's measured occupancy (cpu +
executor + optional memory).

Knobs (constructor kwargs of the front, peeled by it onto the pool):
    workers              initial pool size (env WORKERS, default 1; 0 = the single)
    max_workers          scale-up ceiling, None = unbounded (env MAX_WORKERS)
    min_workers          compaction floor (env MIN_WORKERS, default 1 = reception only)
    reception_threshold  the reception keeps logins under this occupancy (env, default 0.5)
    admission_threshold  other workers stop receiving logins over this (env, default 0.8)
    compaction_margin    compact when headroom H > margin * admission_threshold (env, default 1.5)
    memory_limit_mb      the per-worker memory budget of the occupancy's memory component
                         (env; None lets the front derive it from the host RAM)

Run either way:
    gnrasgiserve <instance> --config examples/multiworker_config.py -p 8081
    python -m genro_asgi serve examples/multiworker_config.py

Through the ``gnrasgiserve`` CLI the instance/host/port come from the CLI (they win over the
defaults below, read from the environment); run directly it falls back to the defaults. The
front mounts itself on the site root — a GenroPy site owns its absolute URLs — and every
worker hosts the site with its register in-process, no daemon.
"""

import os

from genro_asgi.config import AsgiConfigBuilder

from genropy_asgi.spa import GenropySpaApplication

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


# Pool knobs. Defaults match the core's own; the benchmark driver overrides via env.
WORKERS = _int_env("WORKERS", 1)
MAX_WORKERS = _int_env("MAX_WORKERS", None)          # None = unbounded scale-up
MIN_WORKERS = _int_env("MIN_WORKERS", 1)
RECEPTION_THRESHOLD = _float_env("RECEPTION_THRESHOLD", 0.5)
ADMISSION_THRESHOLD = _float_env("ADMISSION_THRESHOLD", 0.8)
COMPACTION_MARGIN = _float_env("COMPACTION_MARGIN", 1.5)
MEMORY_LIMIT_MB = _int_env("MEMORY_LIMIT_MB", None)  # None = derived from the host RAM


class ServerConfiguration(AsgiConfigBuilder):
    def main(self, root):
        cfg = root.configuration()
        cfg.server(host="127.0.0.1", port=PORT)
        cfg.middleware()
        cfg.applications().application(
            code="site",
            app_class=GenropySpaApplication,
            source=SITE,
            debug=False,
            workers=WORKERS,
            local_worker=(WORKERS == 0),  # workers=0 is the in-process single
            max_workers=MAX_WORKERS,
            min_workers=MIN_WORKERS,
            reception_threshold=RECEPTION_THRESHOLD,
            admission_threshold=ADMISSION_THRESHOLD,
            compaction_margin=COMPACTION_MARGIN,
            memory_limit_mb=MEMORY_LIMIT_MB,
        )
