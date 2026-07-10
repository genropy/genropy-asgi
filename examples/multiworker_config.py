# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Benchmark config: the multi-worker pool with an explicit per-worker user cap.

The ``gnrasgiserve`` CLI only exposes ``--workers N`` (the initial pool size); the
per-worker caps (``max_users_first`` / ``max_users_other``) are set here, in a server
config the ``AsgiServer`` loads directly. Used to exercise the pool growing under load:
starting at one worker, ``check_capacity`` spawns another every time a worker crosses
80% of its cap.

Run either way:
    gnrasgiserve <instance> --config examples/multiworker_config.py -p 8081
    python -m genro_asgi serve examples/multiworker_config.py

Through the ``gnrasgiserve`` CLI the instance/host/port come from the CLI (they win over
the defaults below, read from the environment); run directly it falls back to the defaults.
Serves through a commander with a pool of workers (cap 6 logged users each), no register
daemon.
"""

import os

from genro_asgi.config import AsgiConfigBuilder
from genro_asgi.applications.multi_worker_application import SpaMultiWorkerApplication

# The CLI writes these to the environment before loading the config, so the CLI instance
# and port win; run directly (python -m genro_asgi serve) they fall back to the defaults.
SITE = os.environ.get("GNR_ASGI_PATH") or "test_invoice_pg"
PORT = int(os.environ.get("GNR_ASGI_PORT") or 8081)


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
            workers=1,               # start with one worker; the pool grows under load
            max_users_first=6,       # the first worker also hosts guests
            max_users_other=6,       # every other worker
            commander_url=f"http://127.0.0.1:{PORT}",
        )
