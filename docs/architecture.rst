Architecture
============

This chapter explains how genropy-asgi runs a legacy GenroPy site — the moving
parts, why they exist, and how a request flows through them. For the practical
"which mode do I pick" question see :doc:`single-vs-multi`.

The three layers
----------------

.. code-block:: text

   ┌─────────────────────────────────────────────┐
   │  Legacy GenroPy site (GnrWsgiSite)           │  synchronous, WSGI, unmodified
   │  auth · session · pages · datachanges        │
   ├─────────────────────────────────────────────┤
   │  genropy-asgi   (the bridge)                 │  hosts the site, serves its
   │  GenropySpaApplication / WorkerApplication   │  register in-process
   │  GenropyRegisterClient (in-process register) │
   ├─────────────────────────────────────────────┤
   │  genro-asgi     (the framework)              │  AsgiServer, SpaApplication,
   │  commander / worker / sticky routing         │  the commander/worker model
   ├─────────────────────────────────────────────┤
   │  ASGI (uvicorn)                              │
   └─────────────────────────────────────────────┘

The commander/worker model itself is generic and lives in **genro-asgi core**
(``genro_asgi.applications.multi_worker_application``). genropy-asgi is the thin,
GenroPy-aware layer on top: it is the only code that imports ``gnr.*``.

Everything runs behind uvicorn. A GenroPy site is a **synchronous WSGI**
application; genropy-asgi converts each ASGI request into a PEP 3333 environ and
runs the site inside the worker's thread executor, so the async server is never
blocked by synchronous site code.

The site is never modified. The same ``root.py``, the same auth, the same
sessions that run under ``gnrwsgiserve`` run here unchanged.

The GenroPy site host
---------------------

A GenroPy site talks to its **register** (``site.register``) for every piece of
shared state: which connections and pages exist, who is logged in, the pending
datachanges to push to the browser, the page/user/connection/global stores.
Historically that register was a **daemon** — a separate process reached over a
wire (Pyro4, later the ``genro-nodaemon`` TCP daemon). Every command was
serialised onto that wire.

genropy-asgi removes the wire. The register is served **entirely in-process** by
``GenropyRegisterClient``: every ``site.register`` command is answered from the
hosting application's own registries, surface and stores. There is no daemon to
start, connect to, or keep alive.

Two application classes host the site, sharing one mixin
(``GnrSiteHostingMixin``):

``GenropySpaApplication`` (the *single*)
   Hosts the site **and** is the commander of itself: it owns the surface, the
   mailbox and the global store. This is what ``gnrasgiserve <site>`` runs.

``GenropyWorkerApplication`` (the *pool child*)
   Hosts the same site as one worker inside a commander's pool. Nothing
   legacy-specific changes; the role differences (events ride the pool channel,
   datachanges stay local) are inherited from genro-asgi.

The daemonless register
-----------------------

``GenropyRegisterClient`` is a standalone in-process register — no daemon-client
base class, no wire funnel. Every command the legacy calls is an **explicit
public method** with its own body; there is no ``__getattr__`` dispatch magic.

A mutating command does two things:

#. ``_fold(op, args, kwargs)`` — hand the command to the SPA worker so the local
   registries update. On a pool child the lifecycle/POST event also rides up to
   the commander on the pool **channel**. Reads never fold.
#. its own in-process body — read the local registries / stores / pending lists
   and return what the legacy expects.

What the register serves:

* **Lifecycle** — new/change/drop of connection, page and user, plus refresh.
  Folded into the worker's registries; the read side answers from them.
* **Datachanges** — the events pushed to a browser (a record changed, a store
  key updated). They live on the page's **own** worker (see the switch model
  below).
* **Stores** — page/user/connection stores; each item's ``data`` is a real
  in-process GenroPy ``Bag``, locked per item and read/written in place.
* **Global store** — one stable in-process ``Bag``, written by reference.

The register is wired in through the ``gnr.web:daemon`` entry point: the legacy
``gnr.web.daemon`` switcher imports the module named by that entry point and
installs it as ``gnr.web.daemon``, so the legacy imports resolve here — with no
daemon behind them. This is what **replaces genro-nodaemon**.

The commander and the workers
-----------------------------

With ``--workers N`` (or a pool config), the front server is a **commander**
(``GenropyCommanderApplication``, a subclass of the generic
``SpaMultiWorkerApplication`` from genro-asgi core) that:

* spawns and supervises N **worker** subprocesses, each hosting a
  ``GenropyWorkerApplication`` on the same site;
* forwards every request to the right worker — it is an application-level
  reverse proxy, transparent to cookies and headers;
* holds the **affinity registries** and routes by a sticky cookie so a user
  always returns to the same worker.

The commander is ``GenropyCommanderApplication``, a subclass of the generic
``SpaMultiWorkerApplication`` that adds one GenroPy-specific route: the
``/metrics`` endpoint.

The ``/metrics`` endpoint
~~~~~~~~~~~~~~~~~~~~~~~~~~~

``GenropyCommanderApplication`` exposes a Prometheus ``/metrics`` endpoint. Being
an ``@route`` on the commander, ``/metrics`` roots one of the app's **internal
roots** and is served locally by the commander — not forwarded to a single worker
— so the numbers are the whole pool's. It reports the site-wide counters as the exact ``len()`` of the
commander's aggregated registries: ``users`` (``user_registry``), ``pages``
(``pages_index``) and ``connections`` (``cid_to_user``), under the metric
``genropy_site_counters{counter="..."}``. This emulates the legacy webtool
(``genropy/webtools/prometheus.py``), which read the daemon-central siteregister.
One counter the legacy exposed, ``stale_connections_5min``, is **not** available
here: it needs a per-connection ``last_refresh_ts`` the commander does not keep
(its surface is keys and locations only).

Native routes: replacing site paths one at a time
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``/metrics`` is one instance of a general capability. A host application (the
single, a worker, or the commander) demultiplexes every request in **two stages**
(``SpaApplication.handle_request`` in genro-asgi ≥ 0.13):

#. **First segment.** The first path segment is matched against the app's
   ``internal_roots`` — the roots of the subtrees the app routes natively (every
   ``@route`` method it defines, plus attached routing classes). A segment that is
   *not* an internal root belongs to the hosted GenroPy site: it goes straight to
   ``serve_app`` (ASGI → WSGI, the legacy page).
#. **Full path.** When the first segment *is* an internal root, the demux resolves
   the **full path** in the app's own router. If a native node exists, it is served
   natively. If it does **not** (a structural miss inside a claimed root), the path
   falls through to the hosted site after all.

The consequence is the useful part: **claiming a root does not claim its whole
subtree.** A native ``@route`` can shadow a single legacy path while every sibling
path under the same root keeps flowing to the site. This is how a legacy surface is
migrated *incrementally* — one path at a time, no big-bang rewrite:

.. code-block:: python

   from genro_asgi import route
   from genropy_asgi.spa.genropy_commander_application import GenropyCommanderApplication

   class MyCommander(GenropyCommanderApplication):
       @route(media_type="application/json")
       def sys_health(self):            # serves /sys/health natively…
           return {"status": "ok"}
       # …every other /sys/* path still resolves to the legacy site.

Point ``app_class`` at the subclass in the config recipe. The site is never touched;
the native route simply wins for the exact path it defines, and the fallback keeps
serving everything else. Two notes on correctness:

* The structural check (``resolves_natively``) runs **without** auth/channel
  filters — so a native route that *exists* but the request is not allowed to use
  answers with its own 401/403 and does **not** silently fall through to the site.
  Fallback happens on non-existence only.
* A native route must reproduce whatever contract clients expected from the legacy
  path it replaces (``/metrics`` keeps the ``genropy_site_counters`` shape). Paths
  that depend on the legacy page context (session, avatar, rendered page state) are
  the last to migrate; stateless service endpoints are the natural first movers.

Sticky routing
~~~~~~~~~~~~~~~

Routing reads an opaque cookie, ``sticky_cid``, that the commander mints on the
first connection (cleartext, ``HttpOnly; SameSite=Lax``). The registries map
``cid -> user`` and ``user -> {connections, worker}``; a request is forwarded to
the worker that holds its user. The GenroPy session cookie is never decoded for
routing — the ``sticky_cid`` cookie is the only routing key.

One worker never sees another worker's in-process register. That is the point:
each worker's site state is local, and load scales with the number of workers.

The switch model (datachanges)
------------------------------

GenroPy pushes **datachanges** to a browser: a record edited by one user must
reach every page subscribed to that table, possibly on a different worker.

genropy-asgi uses the **switch model**: a datachange queue lives **locally** on
the worker that owns the page. A page drains its own pending list on its own
worker; there is no pull RPC back to a central daemon. When a change must cross
to a page on another worker, the commander forwards it to that worker's
``/datachange_in`` endpoint, where it lands on the local queue like any other.

The commit gate that decides whether a change is worth notifying differs by
role: the single filters against its own local subscriptions; a pool child
passes the change through and the commander fans it out to the workers that
actually subscribe. Over-notifying a worker that does not subscribe is harmless
— it is dropped at the fan-out.

Request flow, end to end
------------------------

**Single**

#. uvicorn hands the ASGI request to ``GenropySpaApplication``.
#. The app converts it to a WSGI environ and runs the ``GnrWsgiSite`` in the
   thread executor.
#. The site calls its register — served in-process by ``GenropyRegisterClient``.
#. The response (with any ``sticky_cid`` birth cookie) goes back through uvicorn.

**Pool**

#. uvicorn hands the request to the commander (``GenropyCommanderApplication``).
#. The commander reads ``sticky_cid``, looks up the user's worker, and forwards
   the request there. No cookie? The welcome worker mints one.
#. The worker runs the site exactly as in the single case; its register is
   in-process and local.
#. The response is relayed back untouched. Lifecycle events the worker produced
   ride the pool channel up to the commander to keep the affinity registries in
   sync.
