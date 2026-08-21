Get started
===========

By the end of this page you will have your existing GenroPy site served over
ASGI — first as a single process (the drop-in for ``gnrwsgiserve``), then as a
supervised worker pool — and you will know how to verify each is running.

Check the prerequisites
-----------------------

Before you install, confirm each of these:

* **Python** >= 3.11.
* **A working GenroPy environment** — ``~/.gnr/environment.xml`` exists and
  points at your GenroPy setup (the same file ``gnrwsgiserve`` needs).
* **An existing site** — a directory with a ``root.py`` (the same site you serve
  with ``gnrwsgiserve``). genropy-asgi runs your site; it does not create one.
* **psycopg2**, if the site is on PostgreSQL (GenroPy's ``pgsql`` extra, or
  ``psycopg2-binary``).
* **A dedicated virtualenv** — installing genropy-asgi replaces the legacy
  ``gnr.web.daemon`` module for every program in that environment (that is the
  daemonless register, see below), so do not install it where you also run the
  classic daemon-based stack.

.. note::

   ``PGGSSENCMODE=disable`` and ``OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`` are
   **not** needed here, on macOS or anywhere else. Both guard against ``fork()``,
   and nothing in this stack forks: a worker is a fresh interpreter, spawned with
   ``subprocess.Popen([executable, "-m", ...])``, and ``os.fork`` appears nowhere
   in genro-asgi or genropy-asgi. They belong to the classic GenroPy stack under
   gunicorn, which does fork its workers.

.. note::

   GenroPy is a **runtime** requirement: the worker runs a ``GnrWsgiSite`` and
   imports ``gnr.*`` only at runtime, never as a build dependency. The only
   Python build dependency is ``genro-asgi``, installed automatically.

Install it
----------

.. code-block:: console

   $ pip install genropy-asgi

→ installs the ``gnrasgiserve`` command and registers the ``gnr.web:daemon``
entry point (the in-process, daemonless register). Nothing else to configure —
there is no daemon to start.

To follow current development, take **both** packages from GitHub:

.. code-block:: console

   $ pip install git+https://github.com/genropy/genro-asgi.git
   $ pip install git+https://github.com/genropy/genropy-asgi.git

.. warning::

   The declared floor is ``genro-asgi>=0.33.0``, so installing genropy-asgi alone
   resolves the published genro-asgi release. That release predates the live
   monitor and the fix that makes a protected route lead to the login (both
   answered ``403`` before). The bridge runs on it — but the monitor described
   below is not there.

From a checkout, for development:

.. code-block:: console

   $ pip install -e .[dev]

Serve your site (single process)
--------------------------------

.. code-block:: console

   $ gnrasgiserve mysite
   → site on http://127.0.0.1:8000/index

``mysite`` is the GenroPy instance name (the same you pass to ``gnrwsgiserve``),
or a path to a site directory. This is the exact drop-in for ``gnrwsgiserve``:
one process, no daemon.

Change host and port:

.. code-block:: console

   $ gnrasgiserve mysite -p 9000              # a different port
   $ gnrasgiserve mysite -H 127.0.0.1 -p 9000 # host + port

Turn debug off:

.. code-block:: console

   $ gnrasgiserve mysite --nodebug            # debug off

.. note::

   ``--reload`` is accepted for surface compatibility with ``gnrwsgiserve`` and
   then ignored — the core server has no reloader, and it prints a line saying
   so. Restart the process to pick up code changes.

Run it as a pool
----------------

.. code-block:: console

   $ gnrasgiserve mysite --workers 2 -p 8080
   → commander on http://0.0.0.0:8080/ routing users to 2 workers

With ``--workers N`` the same command runs the commander/worker model: a front
server routes each user to a stable worker (sticky per user) and grows the pool
under load. See :doc:`single-vs-multi` to choose between the two shapes.

Verify it runs
--------------

**Single or pool** — open ``http://<host>:<port>/index`` in a browser. The site
behaves exactly as it does under ``gnrwsgiserve``.

**Single or pool** — read the site-wide counters, no authentication needed:

.. code-block:: console

   $ curl -s http://127.0.0.1:8000/metrics
   genropy_site_counters{counter="users"} 2
   genropy_site_counters{counter="pages"} 2
   genropy_site_counters{counter="connections"} 2

**The live monitor** is served by genro-asgi at ``/_server/monitor/`` (the JSON
it polls is ``/_server/monitor/snapshot``). Every route is gated
``SERVER_ADMIN``, and the built-in recipe declares no administrator, so out of
the box both answer ``401``. To open it, launch with a config that declares an
``authentication.admin_password`` — plus a ``storage_key``, since the user store
encrypts at rest — then sign in at ``/_server/login_page`` as ``admin``.

.. note::

   The site application renders the monitor's *generic* panel: you see the
   server, its sections and the mounted app, not a per-worker breakdown. The
   placement map — which user sits on which worker — lives in the front and is
   not published over HTTP yet.

Next steps
----------

* :doc:`single-vs-multi` — choose a mode and watch the pool grow.
* :doc:`cli-reference` — every ``gnrasgiserve`` option.
* :doc:`configuration` — tune the occupancy thresholds and pool bounds with a
  config file.
* :doc:`composition` — add a REST API, an MCP endpoint, or an async app beside
  the site.
