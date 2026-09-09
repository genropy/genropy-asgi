# Benchmark reorganization status — 2026-09-09

Worktree: `worktrees/benchmark-reorganization`.
Branch: `codex/benchmark-reorganization`.

## Scope agreed with the owner

Keep a small useful historical baseline, establish the maintained code layout,
and remove obsolete tools. New benchmark campaigns, additional scenarios and
presentation improvements are deferred. Do not bulk-import old trials or add
backup copies/schedules as part of cleanup.

## Implemented

- Canonical recording, replay, execution, measurement, scenario and analysis
  modules. Existing comparison commands use compatibility imports, not duplicate
  recorder implementations.
- Current-core recorded bridge recipe/launcher and population-cycle fixed/dynamic
  recipes, plans, drivers, guards and shared laboratory lifecycle.
- Load-engine fixes for in-flight accounting, drain failures, serialized CSV
  output and chronological start-delay drift. Old results remain unchanged.
- Read-only genro-asgi portal and PostgreSQL on Hetzner, DNS/TLS, online Sphinx and
  internal documentation. Five historical runs are imported: see
  [the import record](historical-import-20260909.md).
- Selected original data is verified in private object storage. PostgreSQL has
  the existing pgBackRest/WAL schedule, already restore-tested. Cleanup adds no
  backup mechanisms or manual backup runs.
- Ten unchanged obsolete scripts removed from the active tree, recoverable from
  Git; see [retired tools](retired-tools.md). Retained HTTP probes and compatibility
  entry points still have callers.
- Twin-proxy isolation fixture no longer starts live archive consumers without
  launched stacks. Identity checks explicitly verify one start per new browser.

## Verification

- Commit hook: Ruff passes. Advisory mypy reports six issues in two unchanged
  runtime source files (`siteregister_client.py`, `genropy_spa_application.py`);
  benchmark cleanup does not change those files.
- Full repository suite after cleanup: 270 passed, 2 optional tests skipped;
  one existing GenroPy deprecation warning. The suite requires local sockets.
- Twin-proxy isolation check passes without background-thread exceptions; reports
  go to a disposable external directory.
- Linux dynamic-profile runner: 14 checks pass, covering stop-on-failure,
  unavailable census, plan mismatch, ordering and cleanup. Census unavailability
  is simulated; this is not a live dynamic-capacity measurement.
- Earlier validation: real recorded bridge smoke, PostgreSQL import/read-only
  contracts, 247 population-cycle checks and 148 measurement checks passed.
- Production import: 5 runs / 30 windows, immutable retry and original JSON
  equivalence verified. HTTPS detail/API pages include historical limitations.

## Deferred work

- Integrate the Git branch after review. CI is prepared but deployment credentials and
  opt-in activation are not configured; documentation snapshots remain manual.
- New controlled runs and reduced functional lab configuration on Hetzner base.
  Stress measurements belong on the dedicated machine.
- More readable comparative tables/graphs and generic orchestration extraction
  into genro-asgi.
- Existing external preservation archives and other development worktrees have
  not been deleted. They contain mixed evidence and code; removal is distinct
  from retiring the tracked obsolete scripts in this branch.
