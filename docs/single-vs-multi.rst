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
under load**. Placement and scaling follow two rules:

* **Placement** — a just-logged user is placed on the first worker with room.
  "Room" means strictly under the worker's user cap; a worker at cap is full.
  Idle workers fill before the pool grows.
* **Scaling** — the pool grows only when the group *as a whole* has no room:
  every routable worker is at or above 80% of its cap. One hot worker with idle
  capacity elsewhere never triggers a spawn. A spawn already in flight is waited
  for, not stacked.

Per-worker caps are configurable. The **first** worker also serves guests
(not-yet-logged-in visitors), so it usually carries a lower logged-user cap
(``max_users_first``) than the others (``max_users_other``).

.. rubric:: A worked example

With a cap of 6 users per worker (80% threshold = 4.8, so a worker has room to
grow the pool while it holds 4 or fewer users), 15 users logging in one after
another settle onto **three** workers:

.. code-block:: text

   users 1..5   → worker 1 fills, crosses 80% at 5 → spawn worker 2
   users 6..10  → worker 2 fills the idle slots (no new spawn)
   users 11     → both at/over 80% → spawn worker 3
   users 12..15 → worker 3 fills

   result: 3 workers holding 5 / 6 / 4 users

The spawn of a fresh worker takes a few seconds (it boots a full
``GnrWsgiSite``). Under a genuine login burst — many users faster than a worker
can boot — the extra logins pile onto the last full worker until the new one
announces, then routing resumes normally. This is expected: the commander never
stacks a second spawn while one is in flight.

Guests and the welcome worker
-----------------------------

Not-yet-logged-in visitors ("guests") are always served by the first worker of
the default group — the welcome worker. It mints the ``sticky_cid`` cookie on a
visitor's first connection. Because it also carries guests, its logged-user cap
is set lower than the other workers'.

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
