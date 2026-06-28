# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Configuration mounting the worker commander on the MAIN server.

The MAIN server is a standard multi-app AsgiServer; this config mounts the
``WorkerCommanderApplication`` on the empty (root) mount and the ``MonitorApp``
live dashboard on ``/_monitor``. The commander spawns and supervises N worker
processes (each serving the GenroPy site) and forwards every request to one of
them. Launch parameters (site, host, port, group counts) are read from
environment variables through resolvers, so this file is fixed while the launch
parameters vary.
"""

from typing import Any

from genro_bag.resolvers import EnvResolver

from genro_asgi.config import AsgiConfigBuilder
from genropy_asgi.monitor import MonitorApp
from genropy_asgi.worker_commander import WorkerCommanderApplication


class ServerConfiguration(AsgiConfigBuilder):
    def setup(self, data: Any) -> None:
        data["site"] = EnvResolver("GNR_ASGI_SITE")
        data["host"] = EnvResolver("GNR_ASGI_HOST", default="127.0.0.1")
        data["port"] = EnvResolver("GNR_ASGI_PORT", default=8080, dtype="L")
        # How many pool workers to start. The FIRST one also hosts the guests, so
        # it carries fewer logged users (max_users_first < max_users_other).
        data["workers"] = EnvResolver("GNR_ASGI_WORKERS", default=1, dtype="L")
        data["max_users_first"] = EnvResolver("GNR_ASGI_MAX_USERS_FIRST", default=20, dtype="L")
        data["max_users_other"] = EnvResolver("GNR_ASGI_MAX_USERS_OTHER", default=30, dtype="L")
        data["metrics_interval"] = EnvResolver(
            "GNR_ASGI_METRICS_INTERVAL", default=2.0, dtype="R"
        )

    def main(self, root: Any) -> None:
        root.server(host="^host", port="^port")
        root.middleware()
        apps = root.applications(default="commander")
        apps.application(
            code="commander",
            app_class=WorkerCommanderApplication,
            site="^site",
            host="127.0.0.1",
            workers="^workers",
            max_users_first="^max_users_first",
            max_users_other="^max_users_other",
            metrics_interval="^metrics_interval",
        )
        apps.application(code="_monitor", app_class=MonitorApp)
