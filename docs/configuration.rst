Configuration
=============

For an ordinary site there is nothing to configure. ``gnrasgiserve mysite``
builds a complete server from a fixed recipe, whose only variable elements come
from the environment.

This page lists those variables, then says when a config file of your own earns
its place.

Environment variables
---------------------

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Variable
     - Meaning
   * - ``GNR_ASGI_PATH``
     - The resolved site path. Written by the CLI from the ``instance``
       argument; a ``--config`` recipe reads it so the CLI instance wins.
   * - ``GNR_ASGI_HOST``, ``GNR_ASGI_PORT``
     - Listener. Written by ``-H`` and ``-p``; default ``127.0.0.1`` and
       ``8000``.
   * - ``GNR_ASGI_DEBUG``
     - Debug flag. **Unset means on.** A value is read as a word: ``""``,
       ``0``, ``false``, ``no`` and ``off`` mean off, anything else means on.
       ``--nodebug`` writes the empty string.
   * - ``GNR_ASGI_DEBUGGER``
     - Any value adds the werkzeug debugger, whose error page evaluates Python
       in the process. Written by ``--fulldebug``, and never on by accident.
   * - ``GNR_ASGI_FROZEN_USERS_PATH``
     - The freezer root. Defaults **inside** the site's own ``data`` directory,
       because a frozen user is kept for days and that directory survives.
   * - ``GNR_ASGI_INSTANCE_DIR``
     - Where the workers' sockets live. Ephemeral, so it defaults under the
       system temp directory — a unix socket path has to stay short.
   * - ``GNR_ASGI_IDLE_FREEZE_MINUTES``
     - The silence past which a user is parked in the freezer. Unset, the worker
       reads the site's own ``<cleanup>`` section (``connection_max_age``, in
       seconds), and 7200 where the site says nothing.
   * - ``GNR_ASGI_WORKER_MAX_USERS``
     - How many users one worker may hold before it refuses the next. Unset, the
       core default governs and one worker takes everybody, so a small site
       never grows a second one.
   * - ``GNR_ASGI_CONSOLE``
     - Any value mounts the pool's debug door on ``/_console`` as MCP tools.
       Full ``eval``: mounting **is** the gate. Never in production.
   * - ``GNR_ASGI_ORCHESTRATION_PROFILES``
     - Any value mounts the orchestration profile archive: a browser page and
       REST API on ``/_sysop/configuration/`` and MCP tools on ``/_sysop/mcp``.
       It **stores** named JSON profiles — it applies nothing to the running
       pool (applying a profile is a planned, separate command). The mount is
       unauthenticated in this first version: development and lab only, never
       in production. See :ref:`orchestration-profiles`.
   * - ``GNR_ASGI_ORCHESTRATION_PROFILES_PATH``
     - Where the profiles live. Defaults to
       ``<site>/data/_orchestration_profiles``.
   * - ``GNR_DAEMON_PROVIDER``
     - Set by the CLI to ``genropy-asgi`` before the site machinery is imported.
       It is what makes GenroPy resolve its daemon namespace to the in-process
       register. Setting it yourself is only needed when you build the server
       without the CLI.
   * - ``GNR_ASGI_WORKERS``
     - **No longer read.** Reported on startup and ignored.

.. note::

   ``GNR_ASGI_WORKER_MAX_USERS=1`` puts every user on a worker of his own. That
   is how the cross-worker paths — the register population, the stores, the
   changes travelling between users — get exercised at all, so it is the value a
   test bench wants and not one a production site needs.

The built-in recipe
-------------------

What ``gnrasgiserve`` builds, without a config file:

* one ``GenropySpaApplication`` mounted on the **root**. A GenroPy site owns its
  absolute URLs — ``/_rsrc``, ``/sys``, the dojo tree — so it cannot live under
  a prefix;
* one group, named ``pool``, whose workers host the site;
* that group's ``engine_factory``, which is what makes its workers **forks** of a
  template process that built the ``GnrWsgiSite`` once;
* the middleware chain, and the ``_server`` application genro-asgi mounts on
  every server.

The recipe is ``genropy_asgi/spa/config.py``, and reading it is the shortest
answer to any question this page does not cover.

.. _orchestration-profiles:

The orchestration profile archive
---------------------------------

Set ``GNR_ASGI_ORCHESTRATION_PROFILES=1`` and the recipe mounts genro-asgi's
``ConfigurationProfilesApplication`` on ``/_sysop``:

.. code-block:: console

   $ GNR_ASGI_ORCHESTRATION_PROFILES=1 gnrasgiserve mysite

The archive **stores** named JSON profiles in a directory of the site. It does
not touch the running pool: applying a stored profile to the live
``SpaApplication`` — with validation and an audit of the previous and new
configuration — is a planned second phase, not part of this mount.

Surfaces:

* browser page: ``http://localhost:8000/_sysop/configuration/``;
* REST: ``GET .../profiles``, ``GET .../read?name=foo``,
  ``POST .../save?name=foo`` (JSON object body),
  ``DELETE .../delete?name=foo`` — all below ``/_sysop/configuration/``;
* MCP: JSON-RPC on ``POST /_sysop/mcp`` with tools ``profiles``, ``read``,
  ``save`` and ``delete``;
* OpenAPI docs: ``/_sysop/_meta/docs``.

A profile is one ``<name>.json`` file. Names are 1–64 characters — letters,
digits, dot, dash, underscore, starting alphanumeric — and ``.json`` may be
given or omitted. The content must be a JSON object of at most 1 MiB; writes
are atomic. Example:

.. code-block:: json

   {
     "cpu_grow_percent": 50,
     "cpu_grow_rearm_percent": 40,
     "occupancy_max_percent": 80,
     "reception_reserved_percent": 0
   }

Profiles live in ``<site>/data/_orchestration_profiles`` unless
``GNR_ASGI_ORCHESTRATION_PROFILES_PATH`` points elsewhere.

.. warning::

   The mount is **unauthenticated** in this first version: whoever reaches the
   port can read and write profiles. It is opt-in precisely for that reason —
   development and lab use only. Before production the sysop surface must be
   gated.

When a config file earns its place
----------------------------------

Reach for ``--config`` when you need something the recipe does not declare: an
administrator for the ``_server`` surface, a second application mounted beside
the site (see :doc:`composition`), or group settings the environment does not
expose — the memory cascade, the occupancy setpoints, more than one group.

A config file is a ``ServerConfiguration``, a subclass of genro-asgi's
``AsgiConfigBuilder``. Start from ``genropy_asgi/spa/config.py`` and change what
you need: it is a working recipe, not an example.

.. code-block:: console

   $ gnrasgiserve mysite --config path/to/my_config.py -p 8081

The CLI writes the instance, host and port to the environment before the server
is built, so a recipe reading ``GNR_ASGI_PATH`` serves the instance you named.
