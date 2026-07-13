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

The latest development version, straight from GitHub:

.. code-block:: console

   $ pip install git+https://github.com/genropy/genropy-asgi.git

From a checkout, for development:

.. code-block:: console

   $ pip install -e .[dev]

Serve your site (single process)
--------------------------------

.. code-block:: console

   $ gnrasgiserve mysite
   → site on http://0.0.0.0:8080/index

``mysite`` is the GenroPy instance name (the same you pass to ``gnrwsgiserve``),
or a path to a site directory. This is the exact drop-in for ``gnrwsgiserve``:
one process, no daemon.

Change host and port:

.. code-block:: console

   $ gnrasgiserve mysite -p 9000              # a different port
   $ gnrasgiserve mysite -H 127.0.0.1 -p 9000 # host + port

Iterate while you edit, or turn debug off:

.. code-block:: console

   $ gnrasgiserve mysite --reload             # auto-restart on file changes
   $ gnrasgiserve mysite --nodebug            # debug off

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

**Pool** — watch the per-worker state. Open the live monitor in a browser:

.. code-block:: text

   http://127.0.0.1:8080/_server/monitor

Or read the same state as JSON, e.g. from a script:

.. code-block:: console

   $ curl -s http://127.0.0.1:8080/_server/monitor_state | python3 -m json.tool

Prometheus metrics for the whole pool are on the commander at ``/metrics``.

Next steps
----------

* :doc:`single-vs-multi` — choose a mode and watch the pool grow.
* :doc:`cli-reference` — every ``gnrasgiserve`` option.
* :doc:`configuration` — tune the occupancy thresholds and pool bounds with a
  config file.
* :doc:`composition` — add a REST API, an MCP endpoint, or an async app beside
  the site.
