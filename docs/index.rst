genropy-asgi
============

**genropy-asgi** serves legacy (synchronous) GenroPy sites on an ASGI server,
with no register daemon. It is the GenroPy-specific bridge on top of
`genro-asgi <https://github.com/genropy/genro-asgi>`_: it hosts an unmodified
``GnrWsgiSite`` behind uvicorn and, when you ask for it, spreads the load over a
supervised pool of worker processes.

It replaces two things at once:

* ``gnrwsgiserve`` (the werkzeug/WSGI launcher) — with ``gnrasgiserve``;
* the register daemon (Pyro4, then ``genro-nodaemon``) — with an in-process
  register. There is no daemon to start.

.. rubric:: Two shapes, one command

``gnrasgiserve <site>`` runs the site in a **single** process — the drop-in
replacement for ``gnrwsgiserve``. Add ``--workers N`` and the same command runs
a **commander** that supervises N worker processes and routes each user to a
stable worker (sticky per user). Same site, same code, unmodified.

.. toctree::
   :maxdepth: 2
   :caption: Guide

   getting-started
   single-vs-multi
   architecture
   cli-reference
   configuration
   faq
   troubleshooting

.. toctree::
   :maxdepth: 1
   :caption: Reference

   api

Status
------

* **Development status**: Alpha
* **Package**: ``genropy-asgi`` (PyPI) · **import**: ``genropy_asgi``
* **Python**: >= 3.11
* **License**: Apache-2.0
* **Source**: https://github.com/genropy/genropy-asgi

Indices
-------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
