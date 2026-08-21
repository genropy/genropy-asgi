# Context: wf/bridge-rebase-new-core
Parent: main
Mode: interactive
Must not break: site-facing verb semantics stay pre_refactoring — the bridge changes BASE, not semantics (owner rule, 2026-08-19)
Must not break: genro-asgi pre_refactoring stack (spa/worker.py, spa/commander.py, applications/spa_app.py) and shared modules (register_registry.py, subscription_index.py, global_store.py) stay untouched — sentinel until Macro 6
Must not break: the refinement pass (inter-worker delivery, restart liturgy, recycle, observability) consumes this rebase — second pass, strictly after the collaudo

## Objective

Rebase the GenroPy bridge onto the new genro-asgi core (`spa/orchestration/` +
`SpaApplicationNew`, commit `43973eb`), then prove it in the browser. Covers
Fasi 5 and 6 of `temp/piano_accensione_genropy_2026-08-19.md`. Authority in
doubt: the archived artifacts in
`../genro-asgi/.phased/done/accensione-genropy-piano-dati/` (plan.md,
notes.md, review.md), then the pre_refactoring worker
(`../genro-asgi/src/genro_asgi/spa/worker.py`), then the genropy daemon.

## Work Plan

- [x] **Phase 1**: New core wired in
  > Done: requirement bumped to genro-asgi>=0.34.0; local checkout ../genro-asgi
    (HEAD 43973eb, version 0.34.0) reinstalled editable, genropy-asgi editable
    install refreshed. Import check `SpaWorker` + `SpaApplicationNew` exits 0;
    `pytest tests/` green on the old bridge (55 passed, 77 skipped — skips are
    pre-existing environmental e2e skips, no code touched).
  > Files: pyproject.toml
  - Run: opus / low
  - Pattern: library-standard
  - Files: pyproject.toml
  - Details: require genro-asgi >= 0.34.0 carrying the data plan (commit
    `43973eb`). Not on PyPI: install the local checkout editable
    (`../genro-asgi`) into this project's venv and record the requirement in
    pyproject.toml. The old bridge stays untouched in this phase.
  - Done: `python -c "from genro_asgi.spa.orchestration.spa_worker import SpaWorker; from genro_asgi.applications.spa_app_new import SpaApplicationNew"`
    exits 0, and `pytest tests/` is green (the existing suite still runs on
    the old bridge).

- [>] **Phase 2**: Rebase the bridge on the new core
  > In execution since 2026-08-20T18:49:17Z
  > WIP: done: source rebased — worker on SpaWorker (drop-verb disk cleanup, idle valve from <cleanup>, store_get read-through in the client, _create_site by name/path no root.py), front on SpaApplicationNew (recipe-driven pool, /metrics population+events), recipe+CLI reworked (GNR_DAEMON_PROVIDER, selector removed), conftest provider; lint green, imports green | missing: test suite adaptation (worker units fixtures freeze_handler/exit_process, register client units, spa app structural->recipe tests, global rail e2e rewrite on read-through, expiry file retirement, legacy_e2e + cli e2e rebase on spawned pool) then full-green Done | next: fix tests/test_genropy_worker_units.py fixture (FreezeHandler tmp, exit_process) | commit: (this)
  - Run: opus / high
  - Pattern: `../genro-asgi/src/genro_asgi/spa/orchestration/spa_worker.py`
    (new worker base, hooks at :492 `build_registry`, :505-:523 registers),
    `../genro-asgi/src/genro_asgi/applications/spa_app_new.py` (new app base;
    no pool ctor kwargs — the pool is born at `on_startup` from the recipe
    words `applications.<code>.commander[.groups.<name>]`, grammar elements
    `commander()`/`group()` in `SpaApplicationGrammarNew`), `../genro-asgi/src/genro_asgi/spa/orchestration/worker_entry.py`
    (:28, :146 — how the child is built from `worker_class`/`worker_kwargs`).
  - Files: src/genropy_asgi/spa/genropy_worker.py,
    src/genropy_asgi/spa/genropy_spa_application.py,
    src/genropy_asgi/siteregister/siteregister_client.py (:1115-1118),
    src/genropy_asgi/spa/config.py, src/genropy_asgi/spa/cli.py,
    tests/test_expiry_and_disk.py (retired with its subject, see notes.md),
    tests/test_global_store_rail.py (e2e half per the global-reads call),
    tests/test_genropy_worker_units.py, tests/test_genropy_spa_application.py,
    tests/test_register_client_units.py, tests/test_legacy_e2e.py,
    tests/test_cli_multiworker_e2e.py (adapt only where they photograph the
    old base; behavioural assertions survive verbatim)
  - Decisions: already ratified in the source document — `drop_page` keeps
    `cascade=` on the bridge and absorbs it (D7, 2026-08-20); core keeps the
    `*_register` names, the bridge exposes `user_items`/`connection_items`/
    `page_items` as translating properties (§7a, 2026-08-20);
    `worker.subscriptions.pages_for(table)` becomes
    `table in worker.subscribed_tables`; `/metrics` keeps the legacy meaning
    of `genropy_site_counters` — the three population counters read from
    `commander.user_map`/`connection_user_map`/`page_connection_map` — with
    the `SpaCommander.counters` event lines as additions;
    mount stays `""` (root); no `workers=1` — the default group with the
    reception suffices (decision 0.2 of the accensione plan); the pool is
    configured by OUR recipe — `ServerConfiguration` (config.py) declares
    `commander(frozen_users_path=..., instance_dir=...)` and one
    `group(worker_class="genropy_asgi.spa.genropy_worker:GenropyWorker",
    worker_kwargs=<source/debug>, entry_module=<explicit, no core default>)`,
    env-driven as today; the front shrinks to `mount=""` + `/metrics` + the
    `source` check; the single/pool selector dies (`workers=`,
    `local_worker=`, `--workers`, `GNR_ASGI_WORKERS` — a set env var logs a
    warning).
  - Decisions (gate, 2026-08-21, owner): sweep ages — `connection_max_age`
    (site `<cleanup>` or 7200s) maps onto `user_idle_freeze_minutes`
    (120 min, env-driven in the recipe); `page_max_age`/`guest_max_age`
    have no equivalent, their absence is documented; commander expiries
    stay at core defaults. Memory — the core gained `worker_max_number`
    (group word, default 6, size divisor, explicit percent wins; genro-asgi
    8af3c46): the front derives nothing, `derive_memory_limit_mb`/`RAM_SHARE`
    removed. Global reads — `store_get` on the lane (genro-asgi 3dcdeff):
    `globalStore().getItem(path)` pays one CALL, no stale local copy; the
    e2e half of test_global_store_rail.py is rewritten on the read-through.
    Daemon switch — `GNR_DAEMON_PROVIDER=genropy-asgi` set by CLI and tests
    (genropy #1070 gates the entry-point override on the explicit request).
    Site build — `_create_site` builds by name/path with no root.py (closes
    genropy-asgi#2/#4); the test site resolves via `instances/test_invoice_pg`
    (the shell under sites/ removed 2026-08-21). Disk — connection folders
    removed on the drop verbs (bridge overrides); frozen-expired folders are
    the declared debt (formerly the orphan sweep).
  - Decisions: OPEN, owner's call at this phase's gate —
    (a) `sweep_expired` / site `<cleanup>` ages must be remapped onto
    `user_idle_freeze_minutes` + `user_expiry_hours` + `guest_expiry_hours`;
    if the map is not 1:1 the owner decides;
    (b) `memory_limit_mb` auto-derivation (D6) vs the core's percentages
    (`memory_max_percent`, `worker_memory_max_percent`); if behaviour
    changes the owner decides.
  - Details: §2+§3+§4 of the source document in one phase — deliberately:
    rebasing the worker without the application leaves the e2e contract
    tests red mid-way, and a red contract test is a STOP, not an
    intermediate state. Worker: base `UserStickyWorker` → `SpaWorker`;
    `build_registry`/`new_store`/`new_collector`/`wsgi_app`/`apply_forwarded`
    carry over (verify `apply_forwarded(bag, change)` signature); DELETE
    `demolish_page`, `demolish_connection`, `wire_entry`/`offer_event`
    overrides, `_replica_global_leaves` (replica is dead, Phase 10);
    `sweep_orphan_folders`/`sole_registry_owner` switched off as declared
    debt; ctor accepts the `worker_entry` kwargs plus `source`/`debug`;
    `gnr_site` placement per Fase 5 — the site lives in the child.
    Application: base `SpaApplication` → `SpaApplicationNew`; `worker_class`
    dotted path + `worker_kwargs`; declare `entry_module` in the recipe — no default exists in the
    core; the entry loads `worker_class` by dotted path. Client: the
    `subscriptions` remap under the `pool_member` branch (:1115), verified
    unreachable in the validated scope but remapped, never left broken.
  - Done: `pytest tests/` fully green on the new base, with the test site
    restored (`sites/test_invoice_pg`) so the site-gated skips are gone in
    test_legacy_e2e, test_cli_multiworker_e2e, test_register_client_units
    and the worker construction tests.

- [ ] **Phase 3**: Browser collaudo — sticky_cid and the four steps
  - Run: opus / medium
  - Pattern: `../genro-asgi/src/genro_asgi/applications/spa_app_new.py`
    (:85 sticky_cid mint, :349 cookie write, :390 header guarantee);
    benchmarks/ for the live-server harness and the login trap.
  - Files: tests/ (one end-to-end scenario), benchmarks/ if the harness
    needs extending
  - Details: Fase 6. First check the doubt open since 2026-08-14 and never
    retried: through genropy-asgi the `sticky_cid` cookie did not reach the
    client and every request travelled anonymous — freeze worked, wake was
    unreachable from traffic. Open the site, inspect Set-Cookie in the
    response, reload, assert the cid stays the same; automate that as an
    end-to-end scenario. Then the manual collaudo below.
  - Done: the end-to-end scenario asserting Set-Cookie emission and cid
    persistence across reload passes.
  - Verify: now — the site opens and logs in
  - Verify: now — navigation updates data (datachanges via collect_page)
  - Verify: now — a commit on a subscribed table reaches the page (dbevents)
  - Verify: now — idle → freeze → a new request wakes the user and the page
    still receives user-store updates (Phase 12 fix; contract-tested in
    genro-asgi but seen from the browser here)

## Notes

- Source document: `temp/piano_rebase_ponte_core_nuovo_2026-08-20.md` —
  superseded by this plan, left in place.
- Imported as a fresh import (handoff, no phases in the source): the phase
  decomposition was derived at import time and approved by the user on
  2026-08-20.
- Contract tests at plan time: declined — the existing e2e suite
  (test_legacy_e2e, test_cli_multiworker_e2e) IS the behavioural contract
  that must survive the rebase.
- Do not touch (source §6): the genro-asgi pre_refactoring stack, the shared
  modules, the refinements — all listed in `Must not break:` above.
- Uncommitted changes predating the workflow ride the tree untouched:
  docs/genropy-asgi-for-dummies.html, docs/getting-started.rst, users/.
