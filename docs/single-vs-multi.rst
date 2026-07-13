Single vs. multi (pool)
=======================

genropy-asgi has two shapes, chosen at launch. Both serve the same site the same
way; the difference is how many processes run the site and who routes to them.
This page helps you pick one, launch it, watch it, and — for the pool — tune when
it grows.

Choose a mode
-------------

* **Single** — development, low concurrency, and the exact drop-in for
  ``gnrwsgiserve``. One process: simple to run, simple to debug.
* **Multi (pool)** — many concurrent users on one host. A GenroPy site is
  synchronous Python: one process serves through one thread-pool and saturates at
  a few concurrent users. The pool runs the site in several processes, each with
  its own register and executor, so throughput scales with the workers. Each user
  is pinned to one worker, so their session state stays coherent.

.. list-table::
   :header-rows: 1
   :widths: 22 39 39

   * -
     - Single
     - Multi (pool)
   * - Launch
     - ``gnrasgiserve <site>``
     - ``gnrasgiserve <site> --workers N``
   * - Processes
     - one
     - one commander + N workers
   * - Routing
     - none (one process)
     - sticky per user (``sticky_cid`` cookie)
   * - Site register
     - in-process, this process
     - in-process, per worker (local)
   * - Concurrency
     - one site, one executor thread pool
     - N sites, N executor thread pools
   * - Scaling
     - fixed
     - the pool grows under load
   * - Replaces
     - ``gnrwsgiserve``
     - a load-balanced multi-process deployment

Launch single
-------------

.. code-block:: console

   $ gnrasgiserve mysite
   → site on http://0.0.0.0:8080/index

Launch a pool
-------------

.. code-block:: console

   $ gnrasgiserve mysite --workers 2
   → commander + 2 workers; each user pinned to one worker

The commander starts with ``--workers N`` workers and grows the pool under load.
To set the thresholds and bounds, launch from a config file instead — see
:doc:`configuration`.

Watch the pool grow
-------------------

Read the live per-worker state — status, **occupancy** (the number that drives
placement and scaling), and user/connection/page counts for context:

.. code-block:: console

   $ curl -s http://127.0.0.1:8080/_server/monitor_state | python3 -m json.tool

For a live view rather than raw JSON, open ``/_server/monitor`` in a browser: a
dashboard (provided natively by genro-asgi) that polls this same state and renders
one panel per mounted app, including the pool's per-worker occupancy and its
population of users. For Prometheus, the commander serves site-wide counters at
``/metrics``.

Understand how the pool grows
-----------------------------

The pool grows on measured **pressure**, not head counts. There are no per-user
caps: an idle user costs about a megabyte and no cpu. Every worker reports its
**occupancy** — a number in 0..1 from its cpu, executor saturation and (optionally)
memory, smoothed over the last few readings — and the commander decides from that:

* **Placement (reception-first)** — every user is born on the group's *reception*
  (its first worker, where guests already live). On login the reception **keeps**
  the user while its own occupancy is under ``reception_threshold`` (default 0.5);
  over that it **passes** the user to the least-occupied of the other workers that
  is still under ``admission_threshold`` (default 0.8).
* **Scale-up** — the pool spawns a worker when no non-reception worker is under
  ``admission_threshold`` (or, in a group of one, the reception itself goes over
  its keep-threshold). One hot worker with idle capacity elsewhere never triggers
  a spawn; a spawn already in flight is waited for, not stacked.
* **Scale-down (compaction)** — when the group has more than about one and a half
  workers' worth of spare occupancy, the least-occupied non-reception worker is
  drained onto the survivors and retired. ``min_workers`` is the floor (default 1,
  the reception); the reception is never compacted.

.. note::

   The site is CPU-bound under the GIL, so a worker's real limit is its cpu and
   executor saturation, not how many users are pinned to it. A worker holding many
   idle sessions is not full; a worker with a few busy ones can be. Deciding on
   occupancy grows the pool when the work — not the head count — demands it.

Tune when it grows
------------------

The knobs live on the pool through a config file (:doc:`configuration`):

* ``reception_threshold`` (0.5) — occupancy under which the reception keeps a login.
* ``admission_threshold`` (0.8) — occupancy over which other workers stop taking
  logins; reaching it on every worker triggers a scale-up.
* ``min_workers`` (1) — compaction floor (the reception).
* ``max_workers`` — scale-up ceiling (``None`` = unbounded).
* ``compaction_margin`` (1.5) — spare occupancy, in workers' worth, that triggers
  a scale-down.

Lower thresholds spread users over more workers sooner; higher thresholds pack
more work per process before growing.

Serve guests
------------

Not-yet-logged-in visitors ("guests") are always served by the first worker of
the default group — the **reception**. It mints the ``sticky_cid`` cookie on a
visitor's first connection, and a login happens where the guest already is. The
reception is the one worker never compacted away, so guests always have a home;
``reception_threshold`` (lower than ``admission_threshold``) keeps it from filling
with logged users before it starts passing them on.

Run several versions at once (groups)
-------------------------------------

A pool need not be one uniform set of workers. The commander can run several
**groups**, and *a group is a runtime*: each group declares its own Python
interpreter, so different groups run different versions of the same site side by
side, on one address. This is the mechanism behind a canary rollout, a
blue/green deploy, or pinning a set of users to a specific build.

Declare ``groups`` as a child of the application in a config file
(:doc:`configuration`); each ``group`` has a ``code``, a ``workers`` count, and an
optional ``python`` — the interpreter path for that group's workers:

.. code-block:: python

   app = apps.application(
       code="site",
       app_class=GenropyCommanderApplication,
       worker_app_class="genropy_asgi.spa.genropy_worker_application:GenropyWorkerApplication",
       app_args={"source": "mysite", "debug": ""},
       commander_url="http://127.0.0.1:8080",
   )
   groups = app.groups(default="green")   # the welcome/base group
   groups.group(code="green",  workers=2, python="/venvs/stable/bin/python")
   groups.group(code="canary", workers=1, python="/venvs/next/bin/python")

A user is routed to a group by the ``xgroup`` field of their **avatar**, read by
the commander at login. An avatar with no ``xgroup``, or one naming an undeclared
group, falls back to the ``default`` group. When a user's ``xgroup`` differs from
the worker holding them, the commander migrates the session to the right group's
worker, live.

.. rubric:: virtualenv, Podman, Docker

The lever is one interpreter path per group (``python=``):

* **virtualenv** — native. Point ``python=`` at each venv's ``bin/python``; every
  group installs its own version of the site in its own venv. No extra tooling.
* **Podman / Docker** — a pattern, not a built-in. ``python=`` launches a local
  interpreter, not a container; the framework has no container spawner. To
  containerize, run the commander in one container and each group's workers in
  others, wired over the commander's HTTP back-channel — a worker only needs to
  reach ``commander_url``.

The avatar → ``xgroup`` mapping is the GenroPy site's responsibility; the
commander only reads the value.

Rely on the global store carefully
----------------------------------

The legacy ``globalStore()`` is coherent across the pool: a write on one worker
becomes visible on the others after a short delay (eventual coherence), while
per-user and per-page state is pinned to one worker and immediately coherent
there. Use it for cache-invalidation timestamps and flags, not for values that
must be read back synchronously from another worker.

Know the current limits
-----------------------

The pool is Alpha. A couple of things to know before relying on it:

* **Occupancy reflects cpu and executor load.** On macOS the memory component is
  off (there is no ``/proc`` RSS to read), so the pool grows on cpu and executor
  saturation alone — the true limit under the GIL.
* **Scale-down migrates live users.** Compacting a group drains a worker by moving
  its users to the survivors; it is safe by design but the newest part of the
  pool, so watch the monitor when a group compacts under real load.
