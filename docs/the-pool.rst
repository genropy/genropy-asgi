The pool
========

A GenroPy site is synchronous Python: one process serves through one thread pool
and saturates at a few concurrent users. genropy-asgi runs the site in several
processes and pins each user to one of them.

There is nothing to choose and nothing to size. The pool always runs, and this
page explains what it does on its own.

Where a user lives
------------------

**All the pages of one user live in the same process as his state.** That is the
founding rule, and everything else follows from it. A grid filtered over six
hundred thousand rows, a document being composed, a tree of selections: that
context lives in the memory of the process serving him, so every request of his
must reach that process.

Routing is by identity, not by connection. The ``spa_connection_id`` cookie
carries the connection id **the site itself minted while serving** — the front
mints nothing and keeps no state of its own. The commander knows whose that id
is, which group he belongs to and which worker holds him.

A client that drops cookies is a new visitor at every request. That is the one
thing that breaks the pinning, and it breaks it the same way it breaks a session
under ``gnrwsgiserve``.

How the pool grows
------------------

It starts with **one** worker — the reception, which is a role and not a count.
After that:

* a newcomer is offered to the workers already running, **fullest first**, so
  what is already warm gets filled before anything new is started;
* a worker judges itself on its own last measurement and refuses when it is over
  its setpoint, or when it already holds as many users as it may;
* when nobody can take him, a new worker is born — and the newcomer waits for
  that birth rather than being turned away;
* when the group's memory quota is full and nobody can leave, the request is
  refused with ``503`` and a ``Retry-After``, which is a polite refusal and not
  an error.

When the load falls the pool shrinks: the emptiest worker is closed, but only if
what it holds fits on the others and the reserve for newcomers stays whole. A
worker still holding somebody is never dropped.

Workers are born by fork
------------------------

A ``GnrWsgiSite`` is expensive to build. So the group owns a **template
process** that builds it once, freezes its heap, and every worker of the group
is a ``fork`` of that template. Starting one more worker costs a fork, not a
cold start.

This is why ``PGGSSENCMODE=disable`` is needed on macOS: libpq negotiating
Kerberos inside a forked child crashes it.

When a user goes quiet
----------------------

A user who stops asking anything is **frozen**: his whole state is written to
the freezer and his worker gets the memory back. His next request wakes him,
wherever there is room — not necessarily on the worker he left.

How long the silence must last before that happens is
``GNR_ASGI_IDLE_FREEZE_MINUTES``. Unset, the worker reads the site's own
``<cleanup>`` section (``connection_max_age``, in seconds), and 7200 where the
site says nothing.

The freezer lives inside the site's ``data`` directory by default, because a
frozen user is kept for days and that directory is the one that survives.
``GNR_ASGI_FROZEN_USERS_PATH`` moves it.

Restarting does not log anybody out
-----------------------------------

On the way down every worker parks its users frozen and the commander writes its
own maps beside them. On the way up the maps come back and nobody is pre-warmed:
the first request of a person is what wakes him. The browser comes back with the
same cookie, the same identity, and no new login.

What is shared, and what is not
-------------------------------

* **Per user** — his pages, their live data, his own store. All in his worker.
* **Global** — one shared tree, whose master lives on the commander and nowhere
  else. A worker reads it with a call and writes it through a grant that is
  all-or-nothing. There is no replica to fall out of date.
* **Between users** — a change one page makes, or a table event, is delivered
  **addressed**: only to the pages that subscribed it, wherever they sit.

Watch it
--------

``/metrics`` gives the site-wide counters with no authentication.
``/_server/monitor/`` gives the live picture, behind the ``SERVER_ADMIN`` gate —
see :doc:`getting-started`.

For the questions nobody predicted there is the console: set ``GNR_ASGI_CONSOLE``
and the pool's debug door is mounted on ``/_console`` as MCP tools, evaluating an
expression inside the commander or inside a named worker. It reads the live
registers without going through the site, so looking leaves no trace in what it
observes.

.. warning::

   The console is full ``eval`` by construction — there is no read-only ``eval``
   in Python. Mounting **is** the gate: unset, the door does not exist. Never set
   it in production.
