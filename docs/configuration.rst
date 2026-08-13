Configuration
=============

Reach for a config file when you need to tune the pool — the occupancy
thresholds and the worker-count bounds the CLI does not expose — or to run more
than one version of the site at once (groups). For everything else, the launch
options and a few environment variables cover single-process and a basic pool
with no config at all.

Write a pool config file
------------------------

A config file is a ``ServerConfiguration`` — a subclass of genro-asgi's
``AsgiConfigBuilder``. You override ``main(self, root)`` to declare the server,
the middleware and the application(s). This is the only place to set the pool's
occupancy thresholds and worker-count bounds.

Save this recipe, then launch it with
``gnrasgiserve <site> --config <file> -p 8081``:

.. code-block:: python

   import os

   from genro_asgi.config import AsgiConfigBuilder
   from genropy_asgi.spa import GenropySpaApplication

   # The CLI writes these to the environment before loading the config, so the
   # CLI instance and port win; run directly they fall back to the defaults.
   SITE = os.environ.get("GNR_ASGI_PATH") or "mysite"
   PORT = int(os.environ.get("GNR_ASGI_PORT") or 8081)


   class ServerConfiguration(AsgiConfigBuilder):
       def main(self, root):
           cfg = root.configuration()
           cfg.server(host="127.0.0.1", port=PORT)
           cfg.middleware()
           cfg.applications().application(
               code="site",
               app_class=GenropySpaApplication,
               source=SITE,                  # the site every worker hosts
               debug=False,
               workers=1,                    # initial pool size; grows under load
               min_workers=1,                # compaction floor (the reception)
               max_workers=None,             # scale-up ceiling; None = unbounded
               reception_threshold=0.5,      # reception keeps logins under this occupancy
               admission_threshold=0.8,      # other workers stop taking logins over this
           )

``application(...)`` parameters
-------------------------------

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - Parameter
     - Meaning
   * - ``code``
     - The application code (``"site"``); the class mounts itself on the root —
       a GenroPy site owns its absolute URLs, so no other mount is accepted.
   * - ``app_class``
     - ``GenropySpaApplication`` — the one front for both shapes: it owns the
       user-sticky pool and the site-wide ``/metrics`` endpoint. Its
       ``worker_class`` default points at
       ``genropy_asgi.spa.genropy_worker:GenropyWorker``, the worker that hosts
       the site (in this process for the single, in each child for a pool).
   * - ``source``, ``debug``
     - The site (name or path) every worker hosts, and its debug flag. They
       travel to each worker as its constructor kwargs.
   * - ``workers``
     - Initial pool size. The pool grows from here on measured pressure.
   * - ``min_workers``
     - Compaction floor: the pool is never drained below this (default 1, the
       reception).
   * - ``max_workers``
     - Scale-up ceiling (omit or ``None`` for unbounded).
   * - ``reception_threshold``
     - Occupancy under which the reception (first worker) keeps a login instead
       of passing it on. Default 0.5.
   * - ``admission_threshold``
     - Occupancy over which a non-reception worker stops accepting logins;
       reaching it on every worker triggers a scale-up. Default 0.8.
   * - ``compaction_margin``
     - Scale-down trigger: the group is compacted (its least-occupied
       non-reception worker drained and retired) when its spare occupancy exceeds
       this many workers' worth of ``admission_threshold``. Default 1.5 — the
       margin gives hysteresis, so scale-up and scale-down never chase each other.
   * - ``memory_limit_mb``
     - The per-worker memory budget the occupancy's memory component is measured
       against. Left out, a pool derives it from the host RAM (80% split over the
       worker slots) and the single passes none at all.

Tune the thresholds
-------------------

Decisions are made on **occupancy** — a 0..1 measure of a worker's real pressure
(cpu, executor saturation, optional memory), not on a user count. Lower thresholds
spread users over more workers sooner (they pass and spawn at lighter load);
higher thresholds pack more work per process before growing. There are no per-user
caps: an idle session costs almost nothing, so the pool grows on measured work,
not head count. See :doc:`single-vs-multi` for the full placement / scale-up /
compaction walk-through.

Run several versions at once (groups)
-------------------------------------

Not available in this model: one front owns one pool, and every worker of it
belongs to the same group — the group name is a constructor value of the pool,
not a collection the recipe declares. Running two versions of the site side by
side therefore means two servers, each with its own front, port and interpreter,
behind whatever routes between them.

The per-group interpreter, the avatar's ``xgroup`` routing key and the live
migration between groups belong to the pool bridge, which is stage two.

Set the environment variables
-----------------------------

The CLI writes these before building the server; the built-in recipe reads them.
Set them yourself when driving the server directly.

.. list-table::
   :header-rows: 1
   :widths: 26 16 58

   * - Variable
     - Default
     - Controls
   * - ``GNR_ASGI_PATH``
     - *(required)*
     - The GenroPy site path (the CLI sets this from the resolved instance).
   * - ``GNR_ASGI_HOST``
     - ``127.0.0.1``
     - Bind host.
   * - ``GNR_ASGI_PORT``
     - ``8000``
     - Listening port.
   * - ``GNR_ASGI_DEBUG``
     - ``true``
     - Debug mode; empty string turns it off.
   * - ``GNR_ASGI_WORKERS``
     - ``0``
     - ``0`` = single process; ``N > 0`` = a commander with N pool workers.

.. note::

   The built-in recipe's defaults (host ``127.0.0.1``, port ``8000``) apply when
   nothing overrides them. The ``gnrasgiserve`` CLI passes its own defaults (host
   ``0.0.0.0``, port ``8080``) when you do not specify them, and those win.
