Status — where the work stands
==============================

**Measured on 2026-08-26**, on branch ``wf/macro2-replica-convergence``:
**144 tests green, 78% coverage**, 1110 statements.

The other pages of this guide describe the bridge in its finished shape. This
one is the only place that says how much of it exists today, and the only one
that changes while the work proceeds. Every row carries its proof: a module with
its coverage, a test that exercises it, or the plain statement that no line of
code exists.

The scale
---------

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - Reading
     - What it means
   * - **Settled**
     - Code complete, dedicated tests, the decision behind it closed.
   * - **Working, with reservations**
     - It runs and is tested, but with points explicitly deferred.
   * - **Designed only**
     - Ratified or discussed, no line of code.
   * - **Open question**
     - No decision taken; it is still a question.

Serving a site
--------------

**Hosting an unmodified GnrWsgiSite** — *settled*. ``genropy_worker.py`` (103
statements, 95%), ``site_engine_factory.py`` (35, 89%), ``legacy_bag.py`` (68,
99%). Exercised end to end by ``test_legacy_e2e.py`` and
``test_genropy_worker_units.py``.

**The daemonless register** — *settled*. ``siteregister_client.py`` is the
largest module of the package by far — 724 statements, 82% — and it is what
GenroPy's own imports resolve to once ``GNR_DAEMON_PROVIDER`` names this
provider. Covered by ``test_register_client_units.py``.

**The front** — *settled*. ``genropy_spa_application.py`` (26 statements, 100%):
the two-stage demux, the ``spa_connection_id`` cookie, and the ``/metrics``
exposition keeping the legacy metric name.

**Workers born by fork** — *settled*. The group declares its ``engine_factory``,
so a template process builds the ``GnrWsgiSite`` once and every worker is a fork
of it. ``test_site_engine_factory.py``.

**The launch recipe** — *working, with reservations*. ``spa/config.py`` is the
recipe every launch uses, and it carries **0% coverage**: no test builds the
server from it. It is exercised by running the command, not by the suite.
``cli.py`` sits at 24% for the same reason.

**The old daemon surface** — *designed only*. ``siteregister/handler.py``,
``processes.py``, ``service.py`` and ``siteregister.py`` are the shapes the
entry point must present, and they carry no coverage: nothing in the daemonless
path calls them.

The comparison bench
--------------------

**The two recorders** — *settled*. The HTTP exchanges and the register calls,
recorded from both stacks, each run written into a SQLite archive of its own
outside the git tree.

**The replica** — *working, with reservations*. A recorded session is replayed
against the bridge and the run stops at the first divergence. The reference
session of the owner is reproduced; the known divergences are recognised rather
than discovered.

**The twin proxy** — *working, with reservations*. The owner browses through a
proxy and every request is performed on both stacks, the legacy answer being the
one the browser receives. One shadow per browser, told apart by the site's own
session cookie. Static assets are dispatched to both and deliberately not
compared — that exclusion is on the record.

**Performance** — *designed only*, and deliberately so. The programme has three
macro-phases: two on fidelity, one on speed. Fidelity work does not read
timings, so the instrumentation may be as heavy as it needs; the speed
comparison runs in the third, with collection off. **No performance comparison
between the two stacks has been produced**, and none should be inferred from
these pages. The measurement ladder exists — the framework floor, the
authenticated ping, one indexed record, the full session replay — each rung
isolating one layer so a slowdown can be attributed instead of guessed.

.. warning::

   Figures taken during macro-phase 2 up to its seventh phase were measured with
   the two stacks in different execution modes, which the bench declared but did
   not impose. They may not be quoted without that caveat. Both readings now come
   from the same source.

What is not there yet
---------------------

**The rebase on the current core** — the published bridge pins the frozen core
release, and this guide describes the bridge as it will be on the current one.
Until the migration lands there is no genropy-asgi release from this line.

**A per-worker view over HTTP** — the monitor renders the generic panel: the
server, its sections and the mounted application. Which user sits on which
worker lives in the commander and is readable through the console, not published
as a page.

**The dedicated batch processes** — the task model and its local execution are
complete in the core; distributing them reuses the same supervision and is not
built.
