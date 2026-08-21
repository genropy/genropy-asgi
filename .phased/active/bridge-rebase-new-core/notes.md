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

Gate record (2026-08-21, all five owner decisions closed in the phase chat):

1. Test site: the shell `sites/test_invoice_pg` (a regenerable `_static`
   cache that intercepted resolver route 1) was moved out; the resolver now
   answers `instances/test_invoice_pg/site` (route 2, root.py marker). The
   `.local_backup` stays untouched. "Restored" in the Done: is satisfied by
   resolvability — nothing was copied back.
2. Global reads: the owner's design — a lock-less read PAYS one RPC. The
   core gained `store_get` (genro-asgi 3dcdeff; an mmap published-view
   attempt 780fa13 was reverted for sub-commander topologies: no shared
   disk guarantee). Bridge: `globalStore().getItem(path)` →
   `worker.store_get`; write rail unchanged; e2e half of
   test_global_store_rail.py rewritten on the read-through.
3. Disk cleanup stays on the bridge: overrides of the three PUBLIC drop
   verbs (`drop_page`/`drop_connection`/`drop_user`) remove the connection
   folders after the core mutation. Freeze/transfer paths use internal
   removers and are deliberately NOT hooked: a frozen or moved user's
   folders must survive. Frozen-then-expired users leave folders behind —
   the declared debt that replaces the retired orphan sweep.
4. Sweep ages: `connection_max_age` (site `<cleanup>` or 7200s) →
   `user_idle_freeze_minutes` = 120 min, env-driven in the recipe (the core
   default is infinity: without it the valve never fires and the Phase 3
   freeze check is unrunnable). `page_max_age`/`guest_max_age`: no
   equivalent — a silent tab's row lives until site drop or user freeze;
   guests are distinguished only at the commander expiry (24h frozen).
5. Memory: core `worker_max_number` (group word, default 6, size divisor,
   explicit `worker_memory_max_percent` wins; genro-asgi 8af3c46). The
   front's `derive_memory_limit_mb` + `RAM_SHARE` are removed: their job
   (auto-sizing a worker) moved into the core; their product
   (`memory_limit_mb` + a declared worker count) no longer exists in the
   architecture.

Surfaced scope folded into the phase (grew out of the genropy develop
alignment, 2026-08-21): genropy PR #1070 gates the `gnr.web.daemon`
entry-point override on `GNR_DAEMON_PROVIDER` — the CLI and the test
conftest must set `genropy-asgi`, or the classic Pyro client loads and the
in-process register never engages (invisible until today only because the
site tests were skipping). `_create_site` builds the site by name/path with
no root.py hunt — GnrWsgiSite accepts a name; the root.py requirement was
the bridge's own error (genropy-asgi#4's accidental-path-resolution is fixed
by the same rework; closes genropy-asgi#2 too; genropy#1077/PR#1080 were
based on that false premise, PR closed 2026-08-21 with explanation).

test_expiry_and_disk.py retirement — behaviour map (decision 2 of the
foreman round): knob defaults/site `<cleanup>` reading → replaced by the
`user_idle_freeze_minutes` mapping (asserted in the adapted worker units);
guest-before-logged expiry and page-vs-connection ages → no equivalent
(commander expiries are contract-tested in genro-asgi); disk cleanup on
drop_page/drop_connection/logout → NEW dedicated tests on the drop-verb
overrides (successor in this phase); orphan-folder sweep + pool-child
restraint (`sole_registry_owner`) → retired with the sweep, replaced by the
declared debt above; the two demolition branches equivalence → moot (the
core has one demolition road).
