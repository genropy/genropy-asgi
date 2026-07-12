Single vs. multi (pool)
=======================

genropy-asgi has two shapes, chosen at launch. Both serve the same site the
same way; the difference is how many processes run the site and who routes to
them.

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

When to use which
-----------------

**Single** is the default and the right choice for development, for a site with
a handful of concurrent users, and as the exact drop-in for ``gnrwsgiserve``. It
is one process: simple to run, simple to debug.

**Multi** is for serving many concurrent users on one host. A GenroPy site is
synchronous Python: a single process serves requests through one thread-pool and
saturates at a few concurrent users. The pool runs the site in several
processes, each with its own register and its own executor, so throughput scales
with the number of workers. Each user is pinned to one worker, so their session
state (which lives in that worker's in-process register) stays coherent.

How the pool scales
-------------------

The commander starts with the configured number of workers and **grows the pool
on measured pressure**, not on head counts. There are no per-user caps: an idle
user costs about a megabyte and no cpu, so counting users answers the wrong
question. Instead every worker reports its **occupancy** — a number in 0..1
derived from its cpu, executor saturation and (optionally) memory, smoothed over
the last few readings — and the commander decides from that:

* **Placement (reception-first)** — every user is born on the group's *reception*
  (its first routable worker, where guests already live). On login the reception
  **keeps** the user while its own occupancy is under ``reception_threshold``
  (default 0.5); over that it **passes** the user to the least-occupied of the
  other workers that is still under ``admission_threshold`` (default 0.8).
* **Scale-up** — the pool spawns a worker when the group cannot place well: no
  non-reception worker is under ``admission_threshold`` (or, in a group of one,
  the reception itself goes over its keep-threshold). One hot worker with idle
  capacity elsewhere never triggers a spawn; a spawn already in flight is waited
  for, not stacked.
* **Scale-down (compaction)** — when the group has more than about one and a half
  workers' worth of spare occupancy, the least-occupied non-reception worker is
  drained onto the survivors and retired. ``min_workers`` is the floor (default
  1, the reception); the reception is never compacted.

The knobs (``reception_threshold``, ``admission_threshold``, ``min_workers``,
``max_workers``, ``compaction_margin``) are set on the pool through a config file
— see :doc:`configuration`.

.. rubric:: Why occupancy and not counts

Because the site is CPU-bound under the GIL, the real limit is a worker's cpu and
executor saturation, not how many users are pinned to it. A worker holding many
idle sessions is not full; a worker with a few busy ones can be. Deciding on
measured occupancy grows the pool exactly when the work — not the head count —
demands it.

Guests and the reception worker
-------------------------------

Not-yet-logged-in visitors ("guests") are always served by the first worker of
the default group — the **reception**. It mints the ``sticky_cid`` cookie on a
visitor's first connection, and a login happens where the guest already is. The
reception is the one worker that is never compacted away, so guests always have a
home; ``reception_threshold`` (lower than ``admission_threshold``) keeps it from
filling with logged users before it starts passing them on.

Groups — several versions behind one commander
-----------------------------------------------

A pool need not be one uniform set of workers. The commander can run several
**groups**, and *a group is a runtime*: each group declares its own Python
interpreter, so different groups run different versions of the same site side by
side, on one address. This is the mechanism behind a canary rollout, a
blue/green deploy, or pinning a set of users to a specific build.

Groups are declared as a ``groups`` child of the application in a config file
(see :doc:`configuration`); each ``group`` has a ``code``, a ``workers`` count,
and an optional ``python`` — the interpreter path for that group's workers:

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
group, falls back to the ``default`` group (the welcome/base version). When a
user's ``xgroup`` differs from the worker holding them, the commander migrates
the session to the right group's worker, live.

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

Observing the pool
------------------

The server exposes a JSON snapshot at ``/_server/monitor_state`` — the workers,
their status and their user counts, plus the commander's surface totals. It is
the direct way to watch the pool grow:

.. code-block:: console

   $ curl -s http://127.0.0.1:8080/_server/monitor_state | python3 -m json.tool

The global store across workers
-------------------------------

The legacy ``globalStore()`` is coherent across the pool. Each leaf write rides
the framework's global-store rail: the worker ships it to the commander (the
single writer of the master), which pushes it down to every worker's replica; a
worker spawned late is seeded with the whole store at announce. Coherence is
**eventual** — a write on one worker becomes visible on another after one
channel round-trip, not synchronously. This suits the real uses (cache-
invalidation timestamps, flags). Per-user and per-page state is pinned to one
worker and is immediately coherent there.

Limitations (current)
----------------------

The pool is Alpha. One thing to know before relying on it:

* **Load metric is provisional.** Placement uses the user count as a stand-in
  for load; a real pressure metric (executor queue, CPU) is in progress.
