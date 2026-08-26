Get started
===========

By the end of this page your existing GenroPy site is served over ASGI, with no
register daemon, and you know how to check that it is running.

Check the prerequisites
-----------------------

* **Python** >= 3.11.
* **A working GenroPy environment** — ``~/.gnr/environment.xml`` exists and
  points at your GenroPy setup (the same file ``gnrwsgiserve`` needs).
* **An existing site** — a directory with a ``root.py``, the same site you serve
  with ``gnrwsgiserve``. genropy-asgi runs your site; it does not create one.
* **psycopg2**, if the site is on PostgreSQL (GenroPy's ``pgsql`` extra, or
  ``psycopg2-binary``).

.. note::

   **On macOS, export** ``PGGSSENCMODE=disable``. The workers of a group are
   born by ``fork`` out of a template process, and libpq negotiating Kerberos
   inside a forked child crashes it.

.. note::

   GenroPy is a **runtime** requirement: the worker runs a ``GnrWsgiSite`` and
   imports ``gnr.*`` only at runtime, never as a build dependency. The only
   Python build dependency is ``genro-asgi``, installed automatically.

Install it
----------

.. code-block:: console

   $ pip install genropy-asgi

That installs the ``gnrasgiserve`` command and declares the ``gnr.web:daemon``
entry point — the in-process register. Nothing else to configure, and no daemon
to start.

The entry point is **not** picked up on its own. GenroPy replaces its daemon
namespace only when ``GNR_DAEMON_PROVIDER`` names a provider, and
``gnrasgiserve`` sets it for its own process before anything imports the site
machinery. Consequence worth knowing: the classic stack and this one can share
one virtualenv, because the choice is made per process and not per installation.

To follow current development, take **both** packages from GitHub:

.. code-block:: console

   $ pip install git+https://github.com/genropy/genro-asgi.git
   $ pip install git+https://github.com/genropy/genropy-asgi.git

From a checkout, for development:

.. code-block:: console

   $ pip install -e .[dev]

Serve your site
---------------

.. code-block:: console

   $ gnrasgiserve mysite
   → site on http://127.0.0.1:8000/index

``mysite`` is the GenroPy instance name — the same you pass to ``gnrwsgiserve``
— or a path to a site directory.

Change host and port:

.. code-block:: console

   $ gnrasgiserve mysite -p 9000                # a different port
   $ gnrasgiserve mysite -H 0.0.0.0 -p 9000     # host + port

Turn debug off:

.. code-block:: console

   $ gnrasgiserve mysite --nodebug

.. note::

   ``--reload`` is accepted for surface compatibility with ``gnrwsgiserve`` and
   then ignored — it prints a line saying so. Restart the process to pick up
   code changes.

There is no ``--workers``. The pool always runs and sizes itself: it starts with
one worker and adds another when no existing one has room for a newcomer. A
``GNR_ASGI_WORKERS`` still set in the environment is reported on startup and
ignored. See :doc:`the-pool`.

Verify it runs
--------------

Open ``http://<host>:<port>/index`` in a browser. The site behaves exactly as it
does under ``gnrwsgiserve``.

Read the site-wide counters — no authentication needed:

.. code-block:: console

   $ curl -s http://127.0.0.1:8000/metrics
   genropy_site_counters{counter="users"} 2
   genropy_site_counters{counter="pages"} 2
   genropy_site_counters{counter="connections"} 2

The metric name is the one the legacy ``/metrics`` webtool exposes, kept
identical so an existing collector keeps working.

**The live monitor** is served by genro-asgi at ``/_server/monitor/``. Every
route under ``/_server`` is gated ``SERVER_ADMIN``, and the built-in recipe
declares no administrator, so out of the box it answers ``401``. To open it,
launch with a ``--config`` recipe that declares an
``authentication.admin_password`` — plus a ``storage_key``, since the user store
encrypts at rest — then sign in at ``/_server/login_page`` as ``admin``.

Next steps
----------

* :doc:`the-pool` — how the pool grows, where a user lives, what happens when he
  goes quiet.
* :doc:`cli-reference` — every ``gnrasgiserve`` option.
* :doc:`configuration` — the environment variables, and when a config file earns
  its place.
* :doc:`composition` — add a REST API, an MCP endpoint, or an async app beside
  the site.
* :doc:`status` — what of all this is already built.
