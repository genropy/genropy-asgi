CLI reference — ``gnrasgiserve``
================================

``gnrasgiserve`` is the ASGI replacement for ``gnrwsgiserve``. It resolves a
GenroPy instance name to its path and starts a genro-asgi ``AsgiServer`` hosting
the site, with the pool already running.

Synopsis
--------

.. code-block:: console

   gnrasgiserve <instance> [-H HOST] [-p PORT] [--nodebug] [--fulldebug]
                           [--reload] [--config CONFIG]

Options
-------

.. list-table::
   :header-rows: 1
   :widths: 24 14 62

   * - Option
     - Default
     - Description
   * - ``instance``
     - *(required)*
     - GenroPy instance/site name, or a path to a site directory. A name is
       resolved through the GenroPy ``PathResolver``; an existing directory is
       used as-is.
   * - ``-H``, ``--host``
     - ``127.0.0.1``
     - Bind host.
   * - ``-p``, ``--port``
     - ``8000``
     - Listening port.
   * - ``--nodebug``
     - off
     - Turn debug off. Debug is **on** unless you say otherwise: it brings the
       SQL counters and the developer's extras.
   * - ``--fulldebug``
     - off
     - Debug **and** the werkzeug debugger. See the warning below.
   * - ``--reload``
     - off
     - Accepted for surface compatibility with ``gnrwsgiserve``, then ignored
       with a printed line. Restart to pick up code changes.
   * - ``--config CONFIG``
     - *(built-in)*
     - A server ``config.py`` (a ``ServerConfiguration``) instead of the
       built-in recipe. The config carries the shape; the CLI ``instance``,
       host and port still win.

.. warning::

   ``--fulldebug`` adds the werkzeug debugger, whose error page **evaluates
   Python in the process**. Debug alone no longer brings it, so that page can
   never appear by accident. Development only.

What is not there any more
--------------------------

``--workers`` is gone, and so is the single/pool selector. The pool always runs
and sizes itself: the number of processes is a reading, never a setting. A
``GNR_ASGI_WORKERS`` still set in the environment is reported on startup and
ignored.

Run from a config file
----------------------

.. code-block:: console

   # through gnrasgiserve — the CLI instance/host/port win, the config brings the shape
   $ gnrasgiserve mysite --config path/to/pool_config.py -p 8080

   # through the genro-asgi core CLI — the config supplies everything
   $ genroasgi serve path/to/pool_config.py

The CLI writes the resolved instance path, and any host/port/debug you pass,
into the environment **before** building the server. So a ``--config`` recipe
that reads ``GNR_ASGI_PATH`` serves the instance you named on the command line.
See :doc:`configuration`.

Remote database, SSL, and the rest
----------------------------------

Site-level launch concerns handled by GenroPy itself — a remote database over an
SSH tunnel, SSL certificates, a data restore — are configured exactly as with
``gnrwsgiserve``, through the site's own configuration and the GenroPy
environment. genropy-asgi changes *how* the site is served, not *what* the site
is.
