# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropySpaApplication — the GenroPy legacy bridge as a SpaApplication subclass.

The generic SPA frame — the registries, the worker services — lives in
``genro_asgi.applications.spa_application.SpaApplication``. This is the legacy specialization: it hosts a GenroPy
``GnrWsgiSite``, so its ``serve_app`` converts the ASGI request to a PEP 3333 environ and
runs the (synchronous) WSGI site in the worker's thread executor. The site's register is
served ENTIRELY in-process by ``GenropyRegisterClient``: every ``site.register`` command
is folded into this application's registries/surface/mailbox and answered from them —
no external register daemon.

The site-hosting behaviour is a MIXIN (``GnrSiteHostingMixin``) shared by the two roles:

- ``GenropySpaApplication`` (this module) — the SINGLE: hosts the site AND is commander
  of itself (surface + mailbox + global store master).
- ``GenropyWorkerApplication`` (genropy_worker_application.py) — the POOL CHILD: hosts
  the same site inside a multi's pool; its register commands ride the pool CHANNEL up
  to the commander, and its datachange queues live LOCAL (switch model, no pull RPC).

It is the ONLY part that imports ``gnr.*``: everything generic is inherited from genro_asgi.applications.spa_application.

``source`` (from SpaApplication, a path or OCI) is the GenroPy site: here it is a site path
or a site name resolved to a path. Two construction modes:
    1. Pre-built site:  GenropySpaApplication(gnr_site=site)
    2. By site name:    GenropySpaApplication(source="test_invoice_pg")
"""

from __future__ import annotations

import atexit
import inspect
import io
import logging
import os
import sys
from types import SimpleNamespace
from typing import Any

from genro_asgi.applications.asgi_application import AsgiApplication
from genro_asgi.applications.spa_application import (
    LIFECYCLE_EVENTS_KEY,
    SpaSingleWorkerApplication,
)

log = logging.getLogger("genropy_asgi.spa")


def _as_bool(value: Any) -> bool:
    """Coerce a constructor flag that may arrive as a string (worker --app-arg)."""
    if isinstance(value, str):
        return value.lower() in ("1", "true", "t", "y", "yes")
    return bool(value)


class GnrSiteHostingMixin:
    """Host a GnrWsgiSite inside a SPA role (the single or a pool child).

    Owns the site lifecycle (creation by name/path or adoption of a pre-built one, the
    optional Werkzeug debugger wrapper, the shutdown), the WSGI bridge (``serve_app``:
    ASGI -> environ -> site in the worker's executor -> response) and the register
    wiring (``site.spa_application`` + the in-process register client). Which role the
    host plays — commander-of-itself or pool child — is the composing class's business.
    """

    app_protocol = "wsgi"

    def on_init(
        self,
        gnr_site: Any = None,
        source: str | None = None,
        site_name: str | None = None,
        debug: Any = True,
        noclean: bool = False,
        **kwargs: Any,
    ) -> None:
        # ``source`` is the SpaApplication-level name for the app source (path|oci);
        # for GenroPy it is the site (name or path). ``site_name`` is kept as an alias.
        # ``debug`` may arrive as a string when spawned via --app-arg.
        debug = _as_bool(debug)
        site = source or site_name
        if gnr_site is not None:
            self._gnr_site = gnr_site
        elif site is not None:
            self._gnr_site = self._create_site(site, debug, noclean)
        else:
            raise ValueError("Either gnr_site or source/site_name is required")
        # In debug mode, wrap the site in the Werkzeug debugger (same as the legacy
        # gnrwsgiserve): a WSGI middleware that serves the interactive traceback page on
        # errors. The bare site is kept for its methods.
        self._wsgi_app = self._gnr_site
        if debug:
            from gnr.web.serverwsgi import GnrDebuggedApplication

            self._wsgi_app = GnrDebuggedApplication(self._gnr_site, evalex=True, pin_security=False)
        # Wire the generic SPA frame (worker + services); the role base sets up
        # everything that is not legacy-specific.
        super().on_init(source=site, **kwargs)
        self._wire_register()

    def _wire_register(self) -> None:
        """Point the site at this app so the register client can reach it.

        The register client is already the in-process ``GenropyRegisterClient`` (the
        ``genropy_asgi.siteregister`` submodule provides the ``gnr.web:daemon``
        entry-point, so the ``GnrWsgiSite`` builds it directly at ``site.register`` —
        no daemon, no rebind). It reaches the worker, the surface and the mailbox lazily
        through ``site.spa_application``, set here.
        """
        self._gnr_site.spa_application = self

    def warm_up(self) -> None:
        """Settle the site's lazy per-process state before the worker is announced.

        Overrides the base worker hook (called by the pool runner after the HTTP
        server is up and before ``/announce_http``). ``resources_dirs`` is published
        to the attribute and only then reversed in place, and the service factory scan
        it drives is uncached: the first concurrent ``GET /`` could otherwise iterate a
        torn list, find no implementation and call ``None`` (genropy#984). Touching
        both here, single-threaded, closes that window. Public site API only; the
        legacy site is untouched. ``storage('gnr')`` is exactly what the first ``GET /``
        resolves in ``build_arg_dict``.
        """
        site = self._gnr_site
        site.resources_dirs
        site.storage("gnr")

    @AsgiApplication.mount_name.setter  # type: ignore[attr-defined]
    def mount_name(self, value: str) -> None:
        """Override setter to sync default_uri on the GnrWsgiSite.

        An empty mount (the main app on root) maps to ``/``; a named mount maps to
        ``/<name>/``.
        """
        self._mount_name = value
        if hasattr(self, "_gnr_site") and self._gnr_site is not None:
            self._gnr_site.default_uri = f"/{value}/" if value else "/"

    @property
    def gnr_site(self) -> Any:
        """The wrapped GnrWsgiSite instance."""
        return self._gnr_site

    async def serve_app(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Run the GnrWsgiSite for a non-service request (the legacy serve_app hook).

        Converts ASGI to WSGI, runs the WSGI site in the worker's thread executor, then
        sends the response. The per-request event sink (when the role seeds one) rides
        into the environ, so the role can observe the request's lifecycle events for
        the response headers (sticky_cid birth cookie, the login sync header).
        """
        body = await _read_body(receive)
        environ = _build_environ(scope, body, self.mount_name)

        status_code, response_headers, response_body = await self.dispatch_executor.submit(
            _run_wsgi, self._wsgi_app, environ
        )

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

    async def on_shutdown(self) -> None:
        """Cleanup GnrWsgiSite on server stop, then the base (executor/channel tasks).

        The base hook is sync on the single and ASYNC on the pool child (it cancels
        the channel sender/occupancy tasks), so the base result is awaited when needed.
        """
        if hasattr(self, "_gnr_site") and self._gnr_site is not None:
            self._gnr_site.on_site_stop()
        result = super().on_shutdown()
        if inspect.isawaitable(result):
            await result

    def _create_site(self, site_name: str, debug: bool, noclean: bool) -> Any:
        """Create GnrWsgiSite from a site name (config-driven mode).

        ``site_name`` may also be a path: PathResolver returns it resolved, and the root.py
        lookup below works from whatever directory it points to.
        """
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
                    f"no root.py found for site '{site_name}' in {site_path} or its parent"
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


class GenropySpaApplication(GnrSiteHostingMixin, SpaSingleWorkerApplication):
    """Legacy-GenroPy SPA host, SINGLE role: runs a GnrWsgiSite as the hosted app.

    For the server this is a normal single-worker SPA. Authentication and session stay
    inside the GnrWsgiSite (its own legacy cookies): ``serve_app`` does not use the asgi
    auth/session layer. Being the single, it is commander of itself: the register client
    folds every command into its own registries/surface/mailbox and serves the pulls
    from them.
    """


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
    """Build PEP 3333 environ dict from ASGI scope and body.

    The per-request lifecycle sink (LIFECYCLE_EVENTS_KEY), when the role seeded one in
    the scope, is copied through: the register client reads it from the current
    request's environ so the events ride the synchronous rail of the response.
    """
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

    if scope.get(LIFECYCLE_EVENTS_KEY) is not None:
        environ[LIFECYCLE_EVENTS_KEY] = scope[LIFECYCLE_EVENTS_KEY]

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


if __name__ == "__main__":
    pass
