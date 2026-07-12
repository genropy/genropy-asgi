FAQ
===

What is genropy-asgi, in one sentence?
   The way to serve an existing GenroPy site on an ASGI server (uvicorn) instead
   of WSGI (werkzeug), with no register daemon — and, on demand, across a pool
   of worker processes.

Do I have to change my site?
   No. The ``GnrWsgiSite`` runs unmodified. Same ``root.py``, same auth, same
   sessions. genropy-asgi changes how the site is served, not what it is.

How is this different from ``gnrwsgiserve``?
   ``gnrwsgiserve`` runs the site under werkzeug (WSGI). ``gnrasgiserve`` runs it
   under uvicorn (ASGI), converting each request to WSGI in a thread executor so
   the site code stays synchronous. You gain native WebSocket support and the
   optional worker pool. The command-line experience is the same: a site name
   and a port.

Single or pool — which do I pick?
   Single (the default) for development and low concurrency; it is the exact
   drop-in for ``gnrwsgiserve``. Pool (``--workers N``) when many users hit one
   host at once — a synchronous GenroPy site saturates one process at a few
   concurrent users, and the pool spreads the load over N processes. See
   :doc:`single-vs-multi`.

What happened to the register daemon?
   It is gone. Historically the site register was a separate process reached
   over a wire (Pyro4, then ``genro-nodaemon``). genropy-asgi serves the register
   **in-process**; there is nothing to start or connect to. It provides the
   ``gnr.web:daemon`` entry point that the legacy resolves, so the legacy imports
   keep working with no daemon behind them. This replaces ``genro-nodaemon``.

Does a user always land on the same worker?
   Yes. In the pool, the commander mints an opaque ``sticky_cid`` cookie and
   routes every request from that user to the worker that holds their session.
   The pin is per user, so their in-process session state stays coherent.

How does the pool decide to grow?
   On **measured occupancy**, not user counts. Each worker reports its cpu,
   executor saturation and (optionally) memory; the commander turns that into an
   occupancy in 0..1. The pool grows when no non-reception worker is under the
   admission threshold (0.8) — the group as a whole is under pressure. A spawn
   already in flight is waited for rather than duplicated. See
   :doc:`single-vs-multi` for the full walk-through.

Can I tune when the pool grows?
   Yes, but not from the CLI — through a config file, by setting the occupancy
   thresholds (``reception_threshold`` / ``admission_threshold``) and the
   worker-count bounds (``min_workers`` / ``max_workers``) on the application.
   There are no per-user caps. See :doc:`configuration`.

Is shared global state consistent across workers?
   Yes, eventually. The legacy ``globalStore()`` rides the framework's
   global-store rail: a write on one worker reaches the others after one channel
   round-trip (the commander is the single writer of the master and pushes to
   every replica; a late worker is seeded at announce). It is eventual, not
   synchronous — which suits the real uses (cache-invalidation timestamps,
   flags). Per-user and per-page state is pinned to one worker and immediately
   coherent there.

Does it need GenroPy at build time?
   No. GenroPy is a **runtime** requirement (the worker runs a ``GnrWsgiSite``).
   The package imports ``gnr.*`` only at runtime. Its only Python build
   dependency is ``genro-asgi``.

Can I add other apps beside the site?
   Yes. The server is multi-app: mount a REST/OpenAPI surface, an MCP endpoint,
   or a native async app beside the site, each on its own path prefix, all on
   the same origin and the same GenroPy database. Because it is one origin, a
   legacy page can reach a new endpoint directly (shared cookies, no CORS). See
   :doc:`composition`.

Where do I see what the pool is doing?
   ``GET /_server/monitor_state`` returns a JSON snapshot: the workers, their
   status, their user counts, and the commander's surface totals.
