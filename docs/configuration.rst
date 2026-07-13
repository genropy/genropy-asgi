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
   from genropy_asgi.spa.genropy_commander_application import GenropyCommanderApplication

   # The CLI writes these to the environment before loading the config, so the
   # CLI instance and port win; run directly they fall back to the defaults.
   SITE = os.environ.get("GNR_ASGI_PATH") or "mysite"
   PORT = int(os.environ.get("GNR_ASGI_PORT") or 8081)


   class ServerConfiguration(AsgiConfigBuilder):
       def main(self, root):
           root.server(host="127.0.0.1", port=PORT)
           root.middleware()
           apps = root.applications(default="site")
           apps.application(
               code="site",
               app_class=GenropyCommanderApplication,
               worker_app_class=(
                   "genropy_asgi.spa.genropy_worker_application:GenropyWorkerApplication"
               ),
               app_args={"source": SITE, "debug": ""},
               workers=1,                    # initial pool size; grows under load
               min_workers=1,                # compaction floor (the reception)
               max_workers=None,             # scale-up ceiling; None = unbounded
               reception_threshold=0.5,      # reception keeps logins under this occupancy
               admission_threshold=0.8,      # other workers stop taking logins over this
               commander_url=f"http://127.0.0.1:{PORT}",
           )

``apps.application(...)`` parameters
------------------------------------

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - Parameter
     - Meaning
   * - ``code``
     - The application code (``"site"``); it is mounted on the root.
   * - ``app_class``
     - The commander class, ``GenropyCommanderApplication`` (a subclass of
       genro-asgi core's ``SpaMultiWorkerApplication``, adding the ``/metrics``
       endpoint).
   * - ``worker_app_class``
     - Import path of the class each worker hosts —
       ``genropy_asgi.spa.genropy_worker_application:GenropyWorkerApplication``.
   * - ``app_args``
     - Constructor kwargs forwarded to each worker: ``source`` (the site),
       ``debug``.
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
   * - ``commander_url``
     - The commander's own public base URL, passed to each worker so it can
       reach the commander back-channel.

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

To run more than one version of the site behind the commander, declare ``groups``
as a child of the application. Each ``group`` is a runtime with its own
interpreter, so each can serve a different version:

.. code-block:: python

   app = apps.application(
       code="site",
       app_class=GenropyCommanderApplication,
       worker_app_class="genropy_asgi.spa.genropy_worker_application:GenropyWorkerApplication",
       app_args={"source": "mysite", "debug": ""},
       commander_url="http://127.0.0.1:8080",
   )
   groups = app.groups(default="green")
   groups.group(code="green",  workers=2, python="/venvs/stable/bin/python")
   groups.group(code="canary", workers=1, python="/venvs/next/bin/python")

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Parameter
     - Meaning
   * - ``groups(default=...)``
     - Names the welcome group: guests and unrecognized xgroups land here. Must
       be one of the declared groups.
   * - ``group(code=...)``
     - The group name — the routing key matched against the avatar's ``xgroup``.
       Worker names are prefixed with it (``green_01``, ``canary_01``).
   * - ``group(workers=...)``
     - The group's initial pool size (it grows under load, per group).
   * - ``group(python=...)``
     - The interpreter path for this group's worker processes. Point it at a
       virtualenv to run a different version. Omitted = the current interpreter.

Users reach a group by the ``xgroup`` field of their avatar; see the groups
section of :doc:`single-vs-multi` for routing, live migration, and how this maps
onto virtualenv / Podman / Docker isolation.

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
