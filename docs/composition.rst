Composition — more apps beside the site
=======================================

The legacy site is not the only thing the server can host. There are **two ways**
to grow native ASGI surface next to it:

* **Beside** — mount separate apps, each on its own path prefix (``/api/``,
  ``/mcp/``, …). The site keeps ``/``; the new apps live under paths it never used.
* **In place** — add native routes to the host application itself, replacing
  individual site paths one at a time (a ``/sys/health`` served natively while the
  rest of ``/sys/*`` stays legacy). This is the incremental-migration path.

Both share the same foundation: it is a **multi-app ASGI server** — one process,
one port, **one origin** — and every app can talk to the *same GenroPy database*
the site uses.

The typical *beside* shape:

* ``/`` — the legacy ``GnrWsgiSite`` (the UI),
* ``/api/`` — a REST/OpenAPI surface,
* ``/mcp/`` — an MCP endpoint for AI agents,
* ``/live/`` — a native async app (e.g. WebSocket).

A REST / OpenAPI endpoint on the site's database
------------------------------------------------

``GenropyProxyOpenApiApplication`` (from ``genropy_asgi.proxy``) hosts a
``GnrApp`` behind an ``OpenApiApplication``. Your routing class exposes plain
methods as REST; the mixin closes the db connection on the executor thread after
each call. Point it at the same instance the site serves — same database,
different surface:

.. code-block:: python

   from genropy_asgi.proxy import GenropyProxyOpenApiApplication

   api = GenropyProxyOpenApiApplication(
       instance="mysite",        # the same GenroPy instance the site serves
       routing_class=MyApi(),    # your @route-decorated methods
       docs="swagger",           # swagger | redoc | off
   )
   server.mount("api", api)      # now on /api/

The same class as MCP
---------------------

The same routing class can be exposed as an **MCP** endpoint with
``McpOpenApiApplication`` (from ``genro_asgi.applications.openapi_application``):
the MCP engine points at the same router, so one set of methods serves both a
REST client and an AI agent — no second implementation.

.. code-block:: python

   from genro_asgi.applications.openapi_application import McpOpenApiApplication

   server.mount("mcp", McpOpenApiApplication(routing_class=MyApi(), api_name="tools"))

In place — replacing site paths one at a time
----------------------------------------------

The *beside* apps above live under their own prefixes. The **in-place** pattern is
different: it adds native routes to the host application itself, on paths the site
already owns, and lets them shadow the legacy handler **one path at a time**. This
is the strangler-fig migration — the native surface grows while the site keeps
serving everything not yet moved, with no second deployment and no cut-over.

It works because the host demultiplexes in two stages (see
:doc:`architecture`): the first path segment selects an **internal root**, then the
**full path** is resolved in the app's own router; a structural miss inside a
claimed root falls through to the site. Claiming a root therefore does *not* claim
its whole subtree — a single native ``@route`` shadows exactly its own path.

Subclass the host application and add the route:

.. code-block:: python

   from genro_asgi import route
   from genropy_asgi.spa.genropy_commander_application import GenropyCommanderApplication

   class MyCommander(GenropyCommanderApplication):
       @route(media_type="application/json")
       def sys_health(self):        # /sys/health is now native…
           return {"status": "ok"}
       # …/sys/customer, /sys/order, … still render on the legacy site.

Then point ``app_class`` at ``MyCommander`` in the config recipe (the same seam the
built-in ``GenropyCommanderApplication`` uses for ``/metrics``). Migrate the
stateless service paths first (health, metrics, small JSON APIs); paths that need
the legacy page context — session, avatar, rendered state — are the last to move.

Why "same origin" matters
-------------------------

Because every app lives under one host and port, the browser sees **one
origin**: no CORS to configure, and the legacy session cookie is sent to every
path. A legacy GenroPy page can therefore reach a new ASGI endpoint directly —
a ``fetch("/api/…")`` from page code, or an ``<iframe src="/live/…">`` embedding
a modern async view inside the classic UI — with the user's session already
authenticated. New surfaces grow next to the old pages without a second
deployment, a second domain, or a token exchange.

.. note::

   "Same origin" is what makes embedding *easy* (shared cookies, no CORS) — the
   framework does not inject ASGI markup into legacy pages for you. You embed
   from the legacy side (a ``fetch``, an ``iframe``, a script tag) pointing at
   the mounted path.

In the pool
-----------

In a pool, mount the extra apps on the **same server as the commander**. The
commander forwards only the site's own traffic to the workers (every path whose
first segment is not one of its own **internal roots**); apps mounted beside it are
served locally, on the commander's process. Each keeps its own ``GnrApp`` and closes
its db connection on the right thread — independent of the workers hosting the
site.
