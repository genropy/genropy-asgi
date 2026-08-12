API reference
=============

genropy-asgi is normally driven through the ``gnrasgiserve`` command, not
imported. This page documents the classes for the cases where you embed the
bridge in your own server or extend it.

The root package exports only ``__version__``; the useful classes live in the
three submodules below.

The SPA bridge
--------------

The classes that host a GenroPy ``GnrWsgiSite``.

.. autoclass:: genropy_asgi.spa.genropy_spa_application.GenropySpaApplication

.. autoclass:: genropy_asgi.spa.genropy_worker.GenropyWorker

.. autoclass:: genropy_asgi.spa.genropy_worker.GenropyRegistry

``GenropySpaApplication`` is the single front for both shapes: it is the core
``SpaApplication`` (whose commander owns the user-sticky pool and the site-wide
``/metrics`` endpoint), and its ``worker_class`` points at ``GenropyWorker`` —
the worker that hosts the site, in this process for the single and in each
spawned child for a pool.

The OpenAPI bridge
------------------

For exposing a GenroPy database behind an ``OpenApiApplication`` (REST/MCP),
with thread-local db cleanup.

.. autoclass:: genropy_asgi.proxy.GenropyProxyMixin

.. autoclass:: genropy_asgi.proxy.GenropyProxyOpenApiApplication

The daemonless register
-----------------------

The in-process register the legacy imports as ``gnr.web.daemon``. You do not
instantiate this yourself — the ``GnrWsgiSite`` builds it at ``site.register``.

.. autoclass:: genropy_asgi.siteregister.GenropyRegisterClient

``genropy_asgi.siteregister.SiteRegisterClient`` is an alias of
``GenropyRegisterClient`` — the name the legacy imports.
