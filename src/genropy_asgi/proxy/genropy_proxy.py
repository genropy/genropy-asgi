# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyProxyMixin — a GenroPy legacy db behind an OpenApiApplication.

An ``OpenApiApplication`` mounts a RoutingClass and exposes it as REST (and, via
``McpOpenApiApplication``, as MCP). When those handlers need the GenroPy legacy
db, someone must own its lifecycle: the connection is thread-local and must be
closed on the thread that opened it — the executor thread where the handler ran,
not the loop. genro-asgi provides exactly that hook (``route_cleanup``, run in
the executor thread by ``make_callable``); this mixin fills it.

The mixin instantiates a ``GnrApp`` and exposes it as ``self.gnr_app`` (the only
channel the mounted RoutingClass reads, e.g. through its parent). It overrides
only what it owns — ``route_cleanup`` (close the current thread's connection) —
and never the ``OpenApiApplication`` machinery. Direction of dependency is
contrib -> genro-asgi: this imports ``gnr.*``; genro-asgi never imports GenroPy.

Compose it with the base to get a mountable app::

    class GenropyProxyOpenApiApplication(GenropyProxyMixin, OpenApiApplication):
        ...

NOTE (collaudo): this package currently lives in genro-asgi/contrib for
end-to-end testing against the real GenroPy legacy. Once proven it will move to
the genropy-asgi repository.
"""

from __future__ import annotations

import logging
from typing import Any

from genro_asgi.applications.openapi_application import OpenApiApplication

log = logging.getLogger("genropy_asgi.proxy")

__all__ = ["GenropyProxyMixin", "GenropyProxyOpenApiApplication"]


class GenropyProxyMixin:
    """Owns a ``GnrApp`` and closes its db connection in the executor thread.

    Mixed before an ``OpenApiApplication``: it builds the GnrApp in ``on_init``
    (before delegating to the base, which mounts the RoutingClass), exposes it as
    ``gnr_app``, and fills ``route_cleanup`` to release the thread-local db
    connection after each handler — where it is thread-correct.
    """

    def on_init(self, instance: str | None = None, debug: bool = False, **kwargs: Any) -> None:
        """Build the GnrApp, then let the OpenApiApplication base mount the API.

        Args:
            instance: GenroPy instance name (or path) resolved by GnrApp.
            debug: Passed through to GnrApp.
            **kwargs: Forwarded to ``OpenApiApplication.on_init`` (routing_class,
                module, docs, api_name, ...).
        """
        from gnr.app.gnrapp import GnrApp

        if not instance:
            raise ValueError("GenropyProxyMixin requires an 'instance'")
        log.info("Creating GnrApp for instance '%s'", instance)
        self._gnr_app = GnrApp(instance, debug=debug)
        log.info("GnrApp '%s' ready", instance)
        super().on_init(**kwargs)  # type: ignore[misc]

    @property
    def gnr_app(self) -> Any:
        """The hosted GnrApp — the only channel the mounted RoutingClass reads."""
        return self._gnr_app

    def route_cleanup(self) -> None:
        """Close the current thread's db connection after the handler.

        Runs in the executor thread (via ``make_callable``), which is where the
        GnrApp opened its thread-local connection, so this is where it must be
        closed. The request-level cleanup runs on the loop and cannot do it.
        """
        db = getattr(self._gnr_app, "db", None)
        if db is not None:
            db.closeConnection()

    def on_shutdown(self) -> None:
        """Release the GnrApp on server stop, then the base."""
        db = getattr(self._gnr_app, "db", None)
        if db is not None:
            db.closeConnection()
        super().on_shutdown()  # type: ignore[misc]


class GenropyProxyOpenApiApplication(GenropyProxyMixin, OpenApiApplication):
    """OpenApiApplication hosting a GnrApp, mountable on an AsgiServer.

    MRO: the mixin owns ``on_init``/``route_cleanup``/``on_shutdown``; the base
    owns the REST + OpenAPI machinery. Mount it like any OpenApiApplication and
    give it an ``instance`` plus a ``routing_class`` (or ``module``).
    """


if __name__ == "__main__":
    pass
