genropy-asgi
============

**genropy-asgi** serves legacy (synchronous) GenroPy sites on an ASGI server,
with no register daemon. It is the GenroPy-specific bridge on top of
`genro-asgi <https://github.com/genropy/genro-asgi>`_: it hosts an unmodified
``GnrWsgiSite`` and spreads its users over a supervised pool of worker
processes.

It replaces two things at once:

* ``gnrwsgiserve`` (the werkzeug/WSGI launcher) — with ``gnrasgiserve``;
* the register daemon (Pyro4, then ``genro-nodaemon``) — with an in-process
  register. There is no daemon to start.

.. rubric:: One command, one shape

.. code-block:: console

   $ gnrasgiserve mysite

That is the whole launch. There is no worker count to declare and no
single/pool selector: the pool always runs, starts with one worker and grows
when the workers it has are full. Each user is pinned to one worker and all his
pages live there, so his session state stays coherent.

Your site does not change: same code, same configuration, same pages.

.. toctree::
   :maxdepth: 2
   :caption: Guide

   getting-started
   the-pool
   composition
   cli-reference
   configuration
   faq
   troubleshooting

.. toctree::
   :maxdepth: 1
   :caption: Reference

   api
   status

Where the state of the work is written
--------------------------------------

These pages describe the bridge in its finished shape. What is already built,
what is built with reservations and what is only designed lives in one place —
:doc:`status` — so a page here does not have to be re-read at every release.
