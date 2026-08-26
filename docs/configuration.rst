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
