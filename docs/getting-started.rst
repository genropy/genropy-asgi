Getting started
===============

Prerequisites
-------------

* **Python** >= 3.11.
* **GenroPy installed and configured** — a working ``~/.gnr/environment.xml``
  and an existing site (a directory with ``root.py``). genropy-asgi runs your
  site; it does not create one. The same site you serve with ``gnrwsgiserve``.
* ``genro-asgi`` — installed automatically as a dependency.

GenroPy is a **runtime** requirement: the worker runs a ``GnrWsgiSite``.
genropy-asgi imports ``gnr.*`` only at runtime, never as a build dependency.

Installation
------------

.. code-block:: console

   $ pip install genropy-asgi

This installs the ``gnrasgiserve`` command and registers the ``gnr.web:daemon``
entry point that provides the in-process (daemonless) register. Nothing else to
configure: there is no daemon to start.

The latest development version, straight from GitHub:

.. code-block:: console

   $ pip install git+https://github.com/genropy/genropy-asgi.git

From a checkout, for development:

.. code-block:: console

   $ pip install -e .[dev]

Quickstart
----------

**Single process** — the drop-in for ``gnrwsgiserve``:

.. code-block:: console

   $ gnrasgiserve mysite
   # site on http://0.0.0.0:8080/index

``mysite`` is the GenroPy instance name (the same you pass to ``gnrwsgiserve``),
or a path to a site directory. Common options:

.. code-block:: console

   $ gnrasgiserve mysite -p 9000              # a different port
   $ gnrasgiserve mysite -H 127.0.0.1 -p 9000 # host + port
   $ gnrasgiserve mysite --reload             # auto-restart on file changes
   $ gnrasgiserve mysite --nodebug            # debug off

**Pool** — one commander supervising N workers, sticky per user:

.. code-block:: console

   $ gnrasgiserve mysite --workers 2 -p 8080

With ``--workers N`` the same command runs the commander/worker model: the front
server routes each user to a stable worker and grows the pool under load. See
:doc:`single-vs-multi` for how it scales.

Verifying it runs
-----------------

Open ``http://<host>:<port>/index`` in a browser — the site should behave
exactly as it does under ``gnrwsgiserve``.

For the pool, the server ships a live monitor (provided by genro-asgi). Open it
in a browser for a per-worker view — occupancy, users, pages:

.. code-block:: text

   http://127.0.0.1:8080/_server/monitor

Or read the same state as JSON, e.g. from a script:

.. code-block:: console

   $ curl -s http://127.0.0.1:8080/_server/monitor_state | python3 -m json.tool

Prometheus metrics for the whole pool are on the commander at ``/metrics``.

Next steps
----------

* :doc:`single-vs-multi` — pick a mode and understand how the pool grows.
* :doc:`architecture` — how a request flows and why there is no daemon.
* :doc:`cli-reference` — every ``gnrasgiserve`` option.
* :doc:`configuration` — tuning the occupancy thresholds and pool bounds with a
  config file.
