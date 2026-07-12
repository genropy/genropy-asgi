Pool internals
==============

.. note::

   Internal / design notes — not part of the published (Read the Docs) tutorial.
   These describe *how* the pool works underneath, for developers working on
   genropy-asgi itself. For operational guidance see the published
   ``single-vs-multi`` and ``configuration`` pages.

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

Occupancy and compaction — implementation notes
-----------------------------------------------

* **Occupancy is cpu + executor first.** The memory component of occupancy is
  active only where the worker can read its RSS (Linux ``/proc``) and a
  ``memory_limit_mb`` is set; on macOS it is off, so occupancy there is driven by
  cpu and executor saturation alone. This is the intended behaviour, not a gap —
  cpu is the true limit under the GIL — but worth knowing when reading the numbers.
* **Compaction moves live users.** Scale-down drains a worker by migrating its
  users to the survivors (a quiesce–snapshot–switch handshake). It is designed to
  be safe, but it is the newest part of the pool; watch the monitor when a group
  compacts under real load.
