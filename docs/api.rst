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

.. autoclass:: genropy_asgi.spa.genropy_worker_application.GenropyWorkerApplication

.. autoclass:: genropy_asgi.spa.genropy_commander_application.GenropyCommanderApplication

The commander that supervises a pool of ``GenropyWorkerApplication`` workers is
``GenropyCommanderApplication`` — a subclass that adds the site-wide ``/metrics``
endpoint. Its generic base, ``SpaMultiWorkerApplication``, lives in genro-asgi
core (``genro_asgi.applications.multi_worker_application``).

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
