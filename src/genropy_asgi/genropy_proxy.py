# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyProxy — ASGI proxy for a GenroPy WSGI site.

Extends AsgiApplication so that AsgiServer sees a normal ASGI app.
HTTP requests are converted to PEP 3333 environ and delegated to the
GnrWsgiSite callable in a thread; the WSGI response is sent back
through the ASGI send channel.

Two construction modes:
    1. Pre-built site (from CLI): GenropyProxy(gnr_site=site)
    2. By site name (from config): GenropyProxy(site_name="fatturazione")
"""

from __future__ import annotations

import atexit
import io
import json
import logging
import os
import sys
from types import SimpleNamespace
from typing import Any

from genro_asgi.applications import AsgiApplication

from .registry_handler import RegistryHandler
from .worker_services import WorkerCommands, WorkerMetrics

# Key under which the per-request lifecycle event list is shared with the site
# register client (kept in sync with genro_nodaemon.siteregister_client). The
# worker seeds the list on the environ; the register client appends to it.
LIFECYCLE_EVENTS_KEY = "genro.lifecycle_events"

# Response header carrying the commander-facing lifecycle events back on the
# forward (worker -> commander piggyback). The commander reads and strips it
# before relaying the response to the browser: it is never client-facing.
LIFECYCLE_HEADER = b"x-genro-lifecycle"

log = logging.getLogger("genropy_asgi")


class GenropyProxy(AsgiApplication):
    """ASGI application that proxies requests to a GnrWsgiSite.

    For the server this is a normal AsgiApplication. Internally it
    converts ASGI scope/receive/send to WSGI environ/start_response,
    runs the GnrWsgiSite in a thread, and sends the response back.
    """

    app_protocol = "wsgi"

    def on_init(
        self,
        gnr_site: Any = None,
        site_name: str | None = None,
        debug: bool = True,
        noclean: bool = False,
        **kwargs: Any,
    ) -> None:
        if gnr_site is not None:
            self._gnr_site = gnr_site
        elif site_name is not None:
            self._gnr_site = self._create_site(site_name, debug, noclean)
        else:
            raise ValueError("Either gnr_site or site_name is required")
        # In debug mode, wrap the site in the Werkzeug debugger (same as the
        # legacy gnrwsgiserve): a WSGI middleware that serves the interactive
        # traceback page on errors. The bare site is kept for its methods.
        self._wsgi_app = self._gnr_site
        if debug:
            from gnr.web.serverwsgi import GnrDebuggedApplication

            self._wsgi_app = GnrDebuggedApplication(self._gnr_site, evalex=True, pin_security=False)
        # The worker's local registries and lifecycle-event processor (this proxy
        # IS the worker_server). Fed at the end of each request with the events the
        # register client appended to the request environ.
        self.registry_handler = RegistryHandler(self)
        # The worker is a container of services: each family of internal endpoints
        # is its own RoutingClass mounted under its namespace, served by the
        # canonical dispatch. Everything else falls through to the GenroPy site
        # (WSGI). Adding a service is mounting a class, not growing a dispatcher.
        self.attach_instance(WorkerCommands(self), name="_commands")
        self.attach_instance(WorkerMetrics(self), name="_metrics")
        # First path segments owned by a mounted service (vs the GenroPy site).
        # index is inherited from AsgiApplication; exclude it so /index/... stays a
        # GenroPy path instead of hitting the splash handler.
        nodes = self.main.nodes(lazy=True)
        self._service_segments = (
            set(nodes.get("entries", {})) | set(nodes.get("routers", {}))
        ) - {"index"}

    @AsgiApplication.mount_name.setter  # type: ignore[attr-defined]
    def mount_name(self, value: str) -> None:
        """Override setter to sync default_uri on the GnrWsgiSite.

        An empty mount (the main app on root) maps to ``/``; a named mount
        maps to ``/<name>/``.
        """
        self._mount_name = value
        if hasattr(self, "_gnr_site") and self._gnr_site is not None:
            self._gnr_site.default_uri = f"/{value}/" if value else "/"

    @property
    def gnr_site(self) -> Any:
        """The wrapped GnrWsgiSite instance."""
        return self._gnr_site

    async def handle_request(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Route a request: mounted services to the canonical dispatch, the rest to
        the GenroPy site (WSGI fallback).

        The first path segment decides: a segment owned by a mounted service
        (``_commands``, ``_metrics``, ...) goes to the canonical ASGI dispatch, which
        resolves the service's @route, hydrates the body and serialises by mime;
        otherwise the path belongs to the GenroPy site and is proxied over WSGI.
        """
        path_in_app = self.path_in_app(scope["path"])
        first_segment = path_in_app.strip("/").split("/")[0]
        if first_segment in self._service_segments:
            await super().handle_request(scope, receive, send)
        else:
            await self._serve_wsgi(scope, receive, send)

    async def _serve_wsgi(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Convert ASGI request to WSGI and proxy to GnrWsgiSite.

        Seeds the per-request lifecycle event list on the environ; the register
        client appends to it while serving. At the end the worker_server feeds
        those events to its RegistryHandler (local registries + commander).
        """
        body = await _read_body(receive)
        environ = _build_environ(scope, body, self.mount_name)
        environ[LIFECYCLE_EVENTS_KEY] = []

        status_code, response_headers, response_body = await self.server.executor.submit(
            _run_wsgi, self._wsgi_app, environ
        )

        commander_events = self.registry_handler.process(
            environ.get(LIFECYCLE_EVENTS_KEY) or []
        )
        if commander_events:
            # Piggyback the lifecycle events for the commander on the response, as
            # a header the commander reads and strips before relaying to the
            # browser (an internal worker->commander channel, never client-facing).
            response_headers = list(response_headers) + [
                (LIFECYCLE_HEADER, json.dumps(commander_events).encode("latin-1"))
            ]

        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": response_headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": response_body,
            }
        )

    def on_shutdown(self) -> None:
        """Cleanup GnrWsgiSite on server stop."""
        if hasattr(self, "_gnr_site") and self._gnr_site is not None:
            self._gnr_site.on_site_stop()

    def _create_site(self, site_name: str, debug: bool, noclean: bool) -> Any:
        """Create GnrWsgiSite from site name (config-driven mode)."""
        from gnr.core.gnrconfig import getGnrConfig
        from gnr.app.pathresolver import PathResolver
        from gnr.web.gnrwsgisite import GnrWsgiSite

        gnr_config = getGnrConfig(set_environment=True)
        site_path = PathResolver().site_name_to_path(site_name)
        script_path = os.path.join(site_path, "root.py")
        if not os.path.isfile(script_path):
            script_path = os.path.join(site_path, "..", "root.py")
            if not os.path.isfile(script_path):
                raise FileNotFoundError(
                    f"no root.py found for site '{site_name}' " f"in {site_path} or its parent"
                )

        options = SimpleNamespace(
            debug=debug,
            noclean=noclean,
            reload=False,
            remote_edit=None,
            source_instance=None,
            restore=None,
        )

        log.info("Creating GnrWsgiSite for '%s' at %s", site_name, site_path)
        site = GnrWsgiSite(
            script_path,
            site_name=site_name,
            _gnrconfig=gnr_config,
            options=options,
        )
        site._local_mode = True
        atexit.register(site.on_site_stop)
        log.info("GnrWsgiSite '%s' ready", site_name)
        return site


async def _read_body(receive: Any) -> bytes:
    """Read full request body from ASGI receive."""
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    return body


def _build_environ(scope: dict[str, Any], body: bytes, mount_name: str) -> dict[str, Any]:
    """Build PEP 3333 environ dict from ASGI scope and body."""
    path = scope.get("path", "/")

    if mount_name:
        prefix = f"/{mount_name}"
        if path.startswith(prefix):
            script_name = prefix
            path_info = path[len(prefix) :] or "/"
        else:
            script_name = ""
            path_info = path
    else:
        # Main app on root: no prefix to strip, the path is the PATH_INFO.
        script_name = ""
        path_info = path

    server = scope.get("server") or ("localhost", 80)
    client = scope.get("client") or ("", 0)

    environ: dict[str, Any] = {
        "REQUEST_METHOD": scope.get("method", "GET"),
        "SCRIPT_NAME": script_name,
        "PATH_INFO": path_info,
        "QUERY_STRING": scope.get("query_string", b"").decode("latin-1"),
        "SERVER_NAME": server[0],
        "SERVER_PORT": str(server[1]),
        "SERVER_PROTOCOL": f"HTTP/{scope.get('http_version', '1.1')}",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scope.get("scheme", "http"),
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }

    if client[0]:
        environ["REMOTE_ADDR"] = client[0]

    for name, value in scope.get("headers", []):
        header_name = name.decode("latin-1")
        header_value = value.decode("latin-1")

        if header_name == "content-type":
            environ["CONTENT_TYPE"] = header_value
        elif header_name == "content-length":
            environ["CONTENT_LENGTH"] = header_value
        else:
            key = f"HTTP_{header_name.upper().replace('-', '_')}"
            environ[key] = header_value

    return environ


def _run_wsgi(
    wsgi_callable: Any, environ: dict[str, Any]
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """Execute WSGI callable synchronously. Runs in a thread via the worker's executor."""
    status_holder: list[str] = []
    headers_holder: list[list[tuple[str, str]]] = []

    def start_response(
        status: str,
        response_headers: list[tuple[str, str]],
        exc_info: Any = None,
    ) -> None:
        status_holder.append(status)
        headers_holder.append(response_headers)

    result_iter = wsgi_callable(environ, start_response)

    try:
        body_parts = list(result_iter)
    finally:
        if hasattr(result_iter, "close"):
            result_iter.close()

    status_code = int(status_holder[0].split(" ", 1)[0])

    asgi_headers: list[tuple[bytes, bytes]] = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in headers_holder[0]
    ]

    body = b"".join(body_parts)

    return status_code, asgi_headers, body
