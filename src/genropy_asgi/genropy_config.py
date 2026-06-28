# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Standard configuration for serving a single GenroPy site.

Used by the ``gnrasgiserve`` CLI: the site name, host, port, reload and debug
are read from environment variables through resolvers, so this file is fixed
while the launch parameters vary. The GenroPy site is mounted as the main
application (root) via GenropyProxy.
"""

from typing import Any

from genro_bag.resolvers import EnvResolver

from genro_asgi.config import AsgiConfigBuilder
from genropy_asgi.genropy_proxy import GenropyProxy


class ServerConfiguration(AsgiConfigBuilder):
    def setup(self, data: Any) -> None:
        data["site"] = EnvResolver("GNR_ASGI_SITE")
        data["host"] = EnvResolver("GNR_ASGI_HOST", default="0.0.0.0")
        data["port"] = EnvResolver("GNR_ASGI_PORT", default=8080, dtype="L")
        data["debug"] = EnvResolver("GNR_ASGI_DEBUG", default=True, dtype="B")

    def main(self, root: Any) -> None:
        root.server(host="^host", port="^port")
        root.middleware()
        apps = root.applications(default="site")
        apps.application(
            code="site",
            app_class=GenropyProxy,
            site_name="^site",
            debug="^debug",
        )
