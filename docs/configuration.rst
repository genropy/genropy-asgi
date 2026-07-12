Configuration
=============

For most uses ``gnrasgiserve`` needs no configuration file: the launch options
and a few environment variables cover single-process and a basic pool. A config
file is needed only to tune the pool — per-worker caps and the worker-count
shape.

Environment variables
----------------------

The CLI writes these before building the server; the built-in recipe reads
them. You can also set them yourself when driving the server directly.

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
   nothing overrides them. The ``gnrasgiserve`` CLI passes its own defaults
   (host ``0.0.0.0``, port ``8080``) when you do not specify them, and those win.

The config file
---------------

A config file is a ``ServerConfiguration`` — a subclass of genro-asgi's
``AsgiConfigBuilder``. You override ``main(self, root)`` to declare the server,
the middleware and the application(s). This is the only place to set the pool's
per-worker caps, which the CLI does not expose.

The pool recipe below is the shape used to run and benchmark the pool. Save it,
then launch with ``gnrasgiserve <site> --config <file> -p 8081``:

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
               workers=1,               # start with one worker; the pool grows under load
               max_users_first=6,       # the first worker also hosts guests
               max_users_other=6,       # every other worker
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
     - Initial pool size. The pool grows from here under load.
   * - ``max_users_first``
     - Logged-user cap of the first worker (it also serves guests, so keep it
       lower). Framework default: 20.
   * - ``max_users_other``
     - Logged-user cap of every other worker. Framework default: 30.
   * - ``max_workers``
     - Optional ceiling on the pool size (omit or ``None`` for unbounded).
   * - ``commander_url``
     - The commander's own public base URL, passed to each worker so it can
       reach the commander back-channel.

Tuning the caps
---------------

The caps decide both placement and the scale threshold (the pool grows when
every worker is at or over 80% of its cap). Lower caps spread users over more
workers sooner; higher caps pack more users per process before spawning. Match
the cap to how many concurrent users one process serves acceptably for your site
— for a synchronous GenroPy site that is typically a small number. See
:doc:`single-vs-multi` for the scaling walk-through.

Groups (several versions at once)
---------------------------------

To run more than one version of the site behind the commander, declare
``groups`` as a child of the application. Each ``group`` is a runtime with its
own interpreter, so each can serve a different version:

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

Users reach a group by the ``xgroup`` field of their avatar; see the Groups
section of :doc:`single-vs-multi` for routing, live migration, and how this maps
onto virtualenv / Podman / Docker isolation.
