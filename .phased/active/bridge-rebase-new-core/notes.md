# Notes — bridge-rebase-new-core

## Phase 2

Clarify round 2026-08-20 — foreman decisions on the phase chat's five
questions (facts re-verified at the source before deciding: grammar words in
spa_app_new.py:120-215, pool born in `on_startup` from
`handler.commander_kwargs/group_kwargs`, lock-less reads land on the local
`global_bag` in siteregister_client.py:581/:248):

1. **Application rebase — false premise fixed, road (b) at zero deployer
   cost because the recipe is OURS.** spa_app_new.py:132-167 are recipe
   grammar words, not ctor keys. The pool is configured where recipes are
   written, and this package already writes one: `ServerConfiguration`
   (spa/config.py) declares `commander(frozen_users_path=...,
   instance_dir=...)` (both mandatory) and ONE
   `group(worker_class="genropy_asgi.spa.genropy_worker:GenropyWorker",
   worker_kwargs=<source/debug>, entry_module=<explicit — no default in the
   core>)`, env-driven exactly as today. The front shrinks to `mount=""` +
   `/metrics` + the `source` check. The single/pool selector dies —
   `workers=`, `local_worker=`, `--workers`, `GNR_ASGI_WORKERS`: the pool
   always runs and sizes itself (the count is a reading, never a setting;
   decision 0.2 already banned declared counts; dev-deploy parity says no
   dev-only single). A `GNR_ASGI_WORKERS` still set logs a warning naming
   the new behaviour. config.py and cli.py join `Files:`.

2. **test_expiry_and_disk.py is retired in this phase together with its
   subject** — every behaviour it asserts is deleted or switched off by the
   ratified design; the successors (freeze valve, commander expiries) are
   contract-tested in genro-asgi. The child lists here, at execution, each
   behaviour it covered and its successor test; any behaviour with NO
   successor anywhere (candidate: disk cleanup of connection folders on
   drop) is flagged at the gate, never silently dropped.
   **test_global_store_rail.py**: the stub half survives untouched; the e2e
   half (:320 on) follows the owner's call on point 5 — deleted with the
   replica if the debt is accepted, rewritten against the materialization
   otherwise.

3. **Done: strengthened** — `pytest tests/` green WITH the test site
   restored (`sites/test_invoice_pg`; its content sits in
   `test_invoice_pg.local_backup` since 2026-08-19): the site-gated skips
   must be gone in test_legacy_e2e, test_cli_multiworker_e2e,
   test_register_client_units and the worker construction tests. Restoring
   the directory is the user's filesystem — asked at the gate.

4. **/metrics keeps the legacy MEANING, not the new attribute's letter**:
   the three population counters read from `commander.user_map` /
   `connection_user_map` / `page_connection_map` (what
   `genropy_site_counters` meant to every existing scrape), plus the
   `SpaCommander.counters` event lines as ADDITIONS. Nothing the old scrape
   read disappears. Matches the standing metrics note: the residual gap was
   exposing population via HTTP.

5. **Lock-less global reads going stale: ask-user at the gate** — the
   governing rule (site-facing behaviour imitates pre_refactoring in full)
   is the owner's; only the owner licenses a divergence. Recommendation to
   present: accept as DECLARED DEBT for this phase — the source document
   itself ratified "no replica on the worker"; a synchronous CALL per
   lock-less read is a perf/design choice better made after the collaudo
   shows whether the site exercises that path — recorded here and revisited
   in the refinement pass. If the owner refuses the debt, the bridge
   materializes reads via the lease snapshot and the e2e half of
   test_global_store_rail.py is rewritten against it.

Unchanged and still the owner's at the gate, as the plan already says: the
sweep-ages remap (the child reports the map is NOT 1:1 — no per-page age in
the core, both expiries live on the commander and concern a frozen user)
and memory_limit_mb (no landing place — `worker_memory_max_percent` is a
share of a share, the D6 derivation dies).
