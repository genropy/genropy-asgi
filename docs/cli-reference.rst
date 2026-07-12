CLI reference — ``gnrasgiserve``
================================

``gnrasgiserve`` is the ASGI replacement for ``gnrwsgiserve``. It resolves a
GenroPy instance name to its path and starts a genro-asgi ``AsgiServer`` hosting
the site. Single process by default; a commander with a worker pool with
``--workers``.

Synopsis
--------

.. code-block:: console

   gnrasgiserve <instance> [-H HOST] [-p PORT] [--reload] [--nodebug]
                           [--workers N] [--config CONFIG]

Options
-------

.. list-table::
   :header-rows: 1
   :widths: 26 12 62

   * - Option
     - Default
     - Description
   * - ``instance``
     - *(required)*
     - GenroPy instance/site name, or a path to a site directory. A name is
       resolved through the GenroPy ``PathResolver``; an existing directory is
       used as-is.
   * - ``-H``, ``--host``
     - ``0.0.0.0``
     - Bind host.
   * - ``-p``, ``--port``
     - ``8080``
     - Listening port.
   * - ``--reload``
     - off
     - Auto-restart when files change (development).
   * - ``--nodebug``
     - off
     - Disable debug mode.
   * - ``--workers N``
     - ``0``
     - Serve through a commander with N worker subprocesses. ``0`` = single
       process. Ignored when ``--config`` is given (the config owns the pool
       shape).
   * - ``--config CONFIG``
     - *(built-in)*
     - Path to a server ``config.py`` (a ``ServerConfiguration``) to use instead
       of the built-in recipe. The config carries the pool shape (worker count
       and occupancy thresholds); the CLI ``instance`` still wins.

.. note::

   The port and host defaults shown are the values the built-in recipe falls
   back to when the CLI does not pass them. ``--config`` recipes are free to
   choose their own defaults.

Single vs. pool
---------------

Without ``--config``, the mode is chosen by ``--workers``:

.. code-block:: console

   $ gnrasgiserve mysite              # single process
   $ gnrasgiserve mysite --workers 3  # commander + 3 workers

The built-in ``--workers`` recipe starts the pool with the framework's default
occupancy thresholds. To tune them (``reception_threshold`` /
``admission_threshold``) or set the worker-count bounds (``min_workers`` /
``max_workers``), use a config file — see :doc:`configuration`.

Running from a config file
---------------------------

Two equivalent launches, both starting the same pool from a config:

.. code-block:: console

   # through gnrasgiserve — the CLI instance/host/port win, the config brings the shape
   $ gnrasgiserve mysite --config path/to/pool_config.py -p 8080

   # through the genro-asgi core CLI — the config supplies everything
   $ python -m genro_asgi serve path/to/pool_config.py

How the instance wins
---------------------

The CLI writes the resolved instance path (and any host/port/debug you pass)
into the environment **before** building the server, so a ``--config`` recipe
that reads ``GNR_ASGI_PATH`` serves the instance you named on the command line.
With ``--config`` the CLI leaves ``GNR_ASGI_WORKERS`` untouched — the config
owns the pool shape. See :doc:`configuration` for the environment variables.

Remote database, SSL, and the rest
-----------------------------------

Site-level launch concerns handled by GenroPy itself (a remote database over an
SSH tunnel, SSL certificates, data restore) are configured the same way as with
``gnrwsgiserve`` — through the site's own configuration and the GenroPy
environment, not through genropy-asgi. genropy-asgi changes *how* the site is
served (ASGI, no daemon), not *what* the site is.
