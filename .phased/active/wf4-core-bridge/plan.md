# Context: wf/wf4-core-bridge
Parent: main | Issue: #1 (globalStore rail — closed by Phase 3)
Mode: autonomous

## Objective
Rebase the GenroPy legacy bridge from genro-asgi-legacy onto the genro-asgi
core (floor >=0.30): GenropyWorker subclass hosting the GnrWsgiSite behind the
core's `wsgi_app` seam, register client rewired onto the worker's op
vocabulary (login-stays contract), global rail with the REAL lock lease, front
on the core `SpaApplication`, expiry + disk cleanup. SINGLE-PROCESS SCOPE:
the pool bridge is stage two. Every decision below was ratified by the owner
on 2026-08-12 (record: `temp/design_WF4_ponte_2026-08-12.md`, all §7 items
closed).

## Work Plan
- [x] **Phase 1**: GenropyWorker + GenropyRegistry + the `::BAG` wire type
  > Done: legacy_bag.py (LegacyBagCollector + ::BAG tytx registration) and
    genropy_worker.py (GenropyWorker(UserStickyWorker) + GenropyRegistry)
    written; 15 unit tests green, 1 skip (worker construction skips until
    Phase 2 rewires the register client — site fixture pattern); ruff clean.
  > Files: src/genropy_asgi/spa/legacy_bag.py,
    src/genropy_asgi/spa/genropy_worker.py, src/genropy_asgi/spa/__init__.py,
    tests/test_genropy_worker_units.py
  > Review: spa/__init__.py touched beyond the declared Files (broken
    pre-rebase re-exports dropped so the subpackage imports; Phase 4
    rewrites it) — rationale in notes.md.
  - Pattern reference: `src/genropy_asgi/spa/genropy_spa_application.py:57-225`
    (site creation/debug wrapper/shutdown to transcribe);
    `../genro-asgi/src/genro_asgi/spa/register_registry.py:172-189` (the
    `new_store`/`new_collector` seams and the collector contract);
    `src/genropy_asgi/siteregister/siteregister_client.py:529-568` (legacy Bag
    trigger capture, the `_on_global_change` mechanics);
    `../genro-bag/src/genro_bag/__init__.py:20-33` (tytx `register_class`)
  - Files: src/genropy_asgi/spa/genropy_worker.py (new),
    src/genropy_asgi/spa/legacy_bag.py (new),
    tests/test_genropy_worker_units.py (new)
  - Decisions:
    - `GenropyWorker(UserStickyWorker)`: constructor kwargs `source`
      (site name or path) and `debug`; builds the GnrWsgiSite exactly as the
      current `_create_site` (PathResolver, root.py fallback in parent dir,
      `GnrDebuggedApplication(evalex=True, pin_security=False)` when debug,
      `site._local_mode = True`, atexit `on_site_stop`); assigns
      `self.wsgi_app` to the (possibly wrapped) site; settles the site's lazy
      per-process state right after creation, single-threaded
      (`site.resources_dirs; site.storage("gnr")` — genropy#984); writes
      `site.spa_worker = self` (ratified name); `build_registry()` returns
      `GenropyRegistry`; `shutdown()` calls `site.on_site_stop()` then super.
    - `GenropyRegistry(RegisterRegistry)`: `new_store()` returns a legacy
      `gnr.core.gnrbag.Bag`; `new_collector(store, paths)` returns
      `LegacyBagCollector` (in legacy_bag.py).
    - `LegacyBagCollector` implements the core collector contract consumed by
      the worker and the registry: `drain(reset=True)`, `append(change,
      replace=False)` (replace = drop pending change with equal `key` dict,
      fresh `change_idx`, tail), `reset()`, `drop(prefix)`, `subscribe_path`,
      `unsubscribe_path`, `detach()`, `changes` property, `pending` count.
      Change shape is genro-bag's plain dict: `{key: {path, reason, fired},
      value, attributes, delete, change_ts (aware UTC), change_idx}`. Capture
      subscribes the legacy Bag with `subscribe(id, any=callback)` (the
      legacy trigger signature: node/pathlist/evt/oldvalue — same mechanics
      as `_on_global_change`); ins/del rebuild the full path with the node
      label; prefixes match on segment boundaries (`a.b` captures `a.b.c`,
      never `a.bc`); `paths=None` captures everything, empty set captures
      nothing; no transaction rail (the legacy Bag has none).
    - `legacy_bag.py` also registers the legacy Bag with genro-tytx at import:
      code `BAG` — the legacy's own historical code (gnrclasses.py:346) —
      serializer `bag.toXml(catalog=<GnrClassCatalog>)`, parser `Bag(txt)`.
      The new genro_bag Bag stays `::X`; the two types are never converted
      into each other.
  - Details: write legacy_bag.py first (collector + tytx registration), then
    genropy_worker.py (worker + registry). Unit tests: capture/drain order,
    append with replace coalescing, prefix widening/narrowing on segment
    boundaries, detach keeps pending, `changes` peek, `::BAG` round-trip
    through `to_tytx`/`from_tytx` (scalar leaves incl. datetime inside the
    Bag), a gnr-Bag store survives `pickle.dumps`/`loads` whole, and the
    registry re-attach path (`change_connection_user` re-creates the
    `user_view` via `new_collector` and re-deposits its pending changes —
    build the state through registry calls, never by hand). Worker
    construction test reuses the site fixture pattern of
    `tests/test_legacy_e2e.py`.
  - Done: `pytest tests/test_genropy_worker_units.py` passes; `ruff check
    src/ tests/` zero errors.

- [x] **Phase 2**: register client rewired onto the worker ops (login-stays)
  > Done: siteregister_client.py rewritten command-by-command onto the worker
    op vocabulary (direct sync calls; _fold, the environ event sink and
    LIFECYCLE_EVENTS_KEY are gone); reads answer from the registers; the pull
    drains collect_page and dresses dbevents at the envelope;
    ServerStore.datachanges/subscribed_paths added (serverbatch heal). 37
    tests green (both files), ruff clean. The Phase 1 worker-construction
    test is now live (the site builds with the rewired client).
  > Files: src/genropy_asgi/siteregister/siteregister_client.py,
    src/genropy_asgi/spa/genropy_worker.py, tests/test_register_client_units.py
  > Review: two declared deviations, rationale in notes.md —
    GenropyWorker.apply_forwarded override (STATE writes need the legacy Bag
    API; genropy_worker.py is a Phase 3 file anyway) and _ship_global
    rerouted onto worker.store_set/store_del one phase early (_fold died
    under it; Phase 3 owns the rail tests). Legacy `data` seed becomes the
    row's live `store` (one-Bag decision, notes.md).
  - Pattern reference: `src/genropy_asgi/siteregister/siteregister_client.py`
    (the file being rewritten — every command keeps its docstring contract);
    op signatures in `../genro-asgi/src/genro_asgi/spa/worker.py` (the
    authority for names and kwargs)
  - Files: src/genropy_asgi/siteregister/siteregister_client.py,
    tests/test_register_client_units.py
  - Decisions:
    - The client reaches the worker as `site.spa_worker` and calls its op
      methods DIRECTLY (they are sync, take `dispatch_lock`, and the call
      runs on the worker's http_pool thread where the CALL sinks are open by
      context copy — verified on core pool.py:100-117). `_fold`, the event
      sink from the environ (`LIFECYCLE_EVENTS_KEY`) and `worker.dispatch`
      die.
    - Command map (verbale §3 b1-b20): `new_connection` →
      `worker.new_connection(cid, **scalars)` (keep `_conn_kwargs`);
      `new_page` → `worker.new_page(user, page_id, session_id=cid, ...)`
      (keep `_page_kwargs`; legacy `data` passes through); LOGIN
      `change_connection_user` → `worker.change_connection_user(cid,
      user=...)` — a LOCAL mutation, nothing ships, the WSGI request keeps
      finding its pages (login-stays, core 0.29); LOGOUT `drop_connection` →
      `worker.drop_connection(cid, session_id=cid)` (core 0.30 op);
      `drop_page` → `worker.drop_page(user, page_id=...)`.
    - Reads answer from the registry registers directly (`page_items.get`
      etc.); `page()` enriches `subscribed_tables` from the row's
      `table_subscriptions`; `pages(connection_id=)` uses
      `page_items.keys_by("session_id", cid)`, `pages(user=)` walks
      user→connections→pages edge sets; `_filter_items` grammar stays
      client-local (single sees everything).
    - `refresh`/ping timestamps: call `worker.refresh_chain(page_id)` for the
      server stamp; write the client-reported clocks as row fields
      `last_user_ts`/`last_rpc_ts` under `dispatch_lock` — NEVER touch
      `last_refresh_ts` with client values.
    - Datachange writes: `set_datachange`/`setInClientData` →
      `worker.set_datachange(identity, change=to_tytx(change_dict),
      kind='page'|'user_store'|..., target=..., filters=..., replace=...)`;
      gnr-Bag values ride `::BAG` (Phase 1). `reset_datachanges`/
      `drop_datachanges` → the homonymous ops. `subscribeTable`/
      `notifyDbEvents`/`setStoreSubscription` → the homonymous ops
      (`client_path` becomes `prefix`; user-store subscriptions =
      `storename='user'`).
    - The pull: `subscription_storechanges`/`handle_ping` drain via
      `worker.collect_page(page_id)` (under its own lock discipline) and
      rebuild the legacy envelope (`sc_%i` Bag, `childDataChanges.<id>`,
      runningBatch window); the `dbevents` species is DRESSED at delivery as
      datachanges on path `gnr.dbchanges.<table>` (the disguise is the
      bridge's, at the envelope — the core keeps them separate species).
    - `ServerStore.datachanges` property served from the collector peek
      (`drain(reset=False)` equivalent on LegacyBagCollector), returning
      legacy `ClientDataChange` objects — heals the latent serverbatch
      defect (verbale §2.0). `subscribed_paths` property beside it.
    - `filter_subscribed_tables`: single role answers from
      `worker.subscriptions.pages_for(table)`; a worker with a channel name
      in a pool passes the whole list through (unchanged cemented rule).
    - Item locks, `catalog`, maintenance/process-bus/dump no-ops, and the
      boot `siteregister` property stay as they are. Global-store methods are
      NOT touched here (Phase 3).
  - Files note: the rewrite keeps the module's "every command is an explicit
    method" discipline — no `__getattr__` dispatch.
  - Details: rewrite command by command following the map above; migrate
    tests/test_register_client_units.py to the new wiring (build state via
    lifecycle calls on a real GenropyWorker with a site fixture, public API
    only).
  - Done: `pytest tests/test_register_client_units.py
    tests/test_genropy_worker_units.py` passes; `ruff check src/ tests/`
    zero errors.

- [x] **Phase 3**: global rail on the core transport, with the REAL lease
  > Done: ascent on worker.store_set/store_del (landed in Phase 2, verified
    here); descent via GenropyWorker.handle_frame override materializing
    snapshot/changes into the legacy global_bag; the REAL lease on
    ServerStore('global') — sync with-form, grant materializes the master,
    thread-local collection, all-or-nothing release, GnrDaemonLocked on a
    dead channel. 20 rail tests green (incl. 5 on a REAL single via
    UserStickyCommander local_worker), full set 57, ruff clean.
  > Files: src/genropy_asgi/siteregister/siteregister_client.py,
    src/genropy_asgi/spa/genropy_worker.py, tests/test_global_store_rail.py
  > Review: the tytx-hop decode discovery (ascent text arrives DECODED at
    every descending edge; two materialization entry points, master content
    mixed but edge-convergent) — mechanism and rationale in notes.md.
  - Pattern reference:
    `src/genropy_asgi/siteregister/siteregister_client.py:529-653` (the
    legacy-side rail: leaf-write shipping, echo suppression, aware→naive
    decode); `src/genropy_asgi/spa/genropy_worker_application.py:45-64` (the
    old descending-push override, to transpose onto `handle_frame`);
    `../genro-asgi/src/genro_asgi/spa/worker.py:1756-1804` (the lease and
    `run_on_loop`)
  - Files: src/genropy_asgi/siteregister/siteregister_client.py,
    src/genropy_asgi/spa/genropy_worker.py,
    tests/test_global_store_rail.py
  - Decisions:
    - Ascent: `_ship_global` calls `worker.store_set(path, encoded)` /
      `worker.store_del(path)` directly (TYTX scalar encoding with suffix,
      `_encode_global` unchanged).
    - Descent: `GenropyWorker.handle_frame` override — `await super()` first,
      then on `GLOBAL_SNAPSHOT_PATH`/`GLOBAL_CHANGES_PATH` materialize into
      the legacy `global_bag` through the register client
      (`load_global_snapshot`/`apply_global_write` logic, `applying` flag,
      aware→naive datetime normalization kept).
    - THE LEASE (D4, ratified: real lock from the single on —
      develop≈deploy): `ServerStore('global').__enter__` acquires
      `worker.global_store_lock()` in its sync `with` form (WSGI thread);
      on grant, the master content is materialized into `global_bag` under
      the `applying` flag; a thread-local `leased` state makes
      `_on_global_change` collect the block's leaf writes into the lease's
      pending list INSTEAD of shipping them; `__exit__` applies the collected
      writes to the lease's working copy and releases — they travel once, on
      `store_unlock`, all-or-nothing (a body that raises releases with
      nothing applied). Lock-less writes (no `with`) keep the immediate
      leaf-write rail unchanged. `GnrDaemonLocked` maps to a lease that
      cannot be acquired (channel down).
  - Details: rewire ship/materialize first, then the lease; migrate
    tests/test_global_store_rail.py (write-through both directions on a real
    single via public API; lease tests: two sequential with-blocks see each
    other's writes; a raising body applies nothing; a plain write outside
    `with` still propagates).
  - Done: `pytest tests/test_global_store_rail.py` passes; `ruff check src/
    tests/` zero errors. Refs #1 in the phase commit (the issue closes at
    consolidation).

- [x] **Phase 4**: the front on the core SpaApplication + recipe + CLI + floor
  > Done: genropy_spa_application.py rewritten on SpaApplication (pool
    defaults -> GenropyWorker dotted path, /metrics native route, D6 memory
    derivation); config.py unified to ONE recipe (workers + local_worker);
    CLI on the 0.30 server API (surface unchanged, --reload
    accepted-and-ignored with a notice); the two pre-rebase application
    modules deleted; exports fixed; floor genro-asgi>=0.30.0. 12 front tests
    green (structural + real-single e2e: forward 200, cookie mint, /metrics
    demux); phases 1-4 set 69 green; `import genropy_asgi.spa` clean; ruff
    clean.
  > Files: src/genropy_asgi/spa/genropy_spa_application.py,
    src/genropy_asgi/spa/config.py, src/genropy_asgi/spa/cli.py,
    src/genropy_asgi/spa/genropy_commander_application.py (deleted),
    src/genropy_asgi/spa/genropy_worker_application.py (deleted),
    src/genropy_asgi/spa/__init__.py, src/genropy_asgi/__init__.py,
    pyproject.toml, tests/test_genropy_spa_application.py
  > Review: --reload is a no-op with a printed notice (core has no reloader)
    — rationale in notes.md.
  - Pattern reference:
    `../genro-asgi/src/genro_asgi/applications/spa_app.py` (the base being
    subclassed: kwarg peel, demux, forward);
    `src/genropy_asgi/spa/genropy_commander_application.py:36-58` (the
    /metrics exposition to transpose); `src/genropy_asgi/spa/config.py` (the
    recipe being unified)
  - Files: src/genropy_asgi/spa/genropy_spa_application.py (rewritten),
    src/genropy_asgi/spa/config.py, src/genropy_asgi/spa/cli.py,
    src/genropy_asgi/spa/genropy_commander_application.py (deleted),
    src/genropy_asgi/spa/genropy_worker_application.py (deleted),
    src/genropy_asgi/spa/__init__.py, src/genropy_asgi/__init__.py,
    pyproject.toml, tests/test_genropy_spa_application.py
  - Decisions:
    - `GenropySpaApplication(SpaApplication)` — ratified name, new base. Its
      `__init__` fixes the pool defaults: `worker_class =
      "genropy_asgi.spa.genropy_worker:GenropyWorker"`, `worker_kwargs`
      built from `source`/`debug`, then delegates to the base peel.
    - `/metrics` as a `@route(media_type="text/plain")` on the front (served
      natively by the demux): metric name `genropy_site_counters` unchanged;
      counters read the commander surface — users =
      `len(commander.user_worker_map)`, pages =
      `len(commander.page_connection)`, connections =
      `len(commander.connection_user)`.
    - `memory_limit_mb` auto-derivation (D6, ratified): when not given
      explicitly AND `workers > 0`: `int(total_ram_bytes * 0.8 / 2**20 /
      (max_workers or workers))`, total RAM from
      `os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')` (stdlib,
      never psutil); logged at INFO with the computed value; an explicit
      value always wins; `workers == 0` (single) passes nothing (the
      in-process worker is never recycled by construction).
    - Recipe (config.py): ONE shape — `apps.application(code="site",
      app_class=GenropySpaApplication, source="^path", debug="^debug",
      workers=<GNR_ASGI_WORKERS>, local_worker=(workers == 0))`. The CLI
      surface is unchanged (`--workers`, `--config` precedence rules as
      today).
    - pyproject.toml: `genro-asgi>=0.30.0`. Entry-point `gnr.web:daemon`
      untouched.
  - Details: rewrite the front module (the old mixin dies — WsgiSeam and
    GenropyWorker replaced it), unify the recipe, bump the floor, fix the
    exports; migrate tests/test_genropy_spa_application.py (mount, demux:
    /metrics native and site paths forwarded, cookie mint, a full GET served
    end-to-end on a single with a real site fixture).
  - Done: `pytest tests/test_genropy_spa_application.py` passes; `python -c
    "import genropy_asgi.spa"` clean; `ruff check src/ tests/` zero errors.

- [x] **Phase 5**: expiry armed + disk cleanup on drop
  > Done: gate re-checked on committed core main (c6b8b95, 0.31.0 — kwargs
    delivered); knobs defaulted on GenropyWorker (600/1800/86400, sweep
    armed at 60s); demolish_page/demolish_connection overrides remove the
    disk folders at the drop; orphan pass in sweep_expired; client cleanup
    trio neutered (claim_cleanup False, expire_* no-ops, shim aligned to
    600). 8 expiry/disk tests green; phases 1-5 set 77 green; ruff clean.
  > Files: src/genropy_asgi/spa/genropy_worker.py,
    src/genropy_asgi/siteregister/siteregister_client.py,
    src/genropy_asgi/siteregister/siteregister.py,
    tests/test_expiry_and_disk.py
  - Pattern reference: `../genro-asgi/src/genro_asgi/spa/worker.py` (the
    sweep being armed: `sweep_expired`/`sweep_loop`/`is_guest_connection`);
    `~/Sviluppo/genropy/genropy/gnrpy/gnr/web/gnrwsgisite.py:1755-1814`
    (`_runCleanup` — the disk half being transposed)
  - Files: src/genropy_asgi/spa/genropy_worker.py,
    src/genropy_asgi/siteregister/siteregister_client.py,
    src/genropy_asgi/siteregister/siteregister.py,
    tests/test_expiry_and_disk.py (new)
  - Decisions:
    - Knobs (ratified names/values): `guest_max_age=1800`,
      `connection_max_age=86400`, `page_max_age=600` — constructor kwargs of
      GenropyWorker, defaulted there, forwarded to the sweep. PREREQUISITE:
      the core must read the three ages from worker state instead of module
      constants (see Notes — small core issue, same liturgy as #11).
    - The sweep is ARMED by default on GenropyWorker (`sweep_interval=60`);
      the ping already stamps `refresh_chain` (Phase 2), so an idle-but-alive
      page is refreshed by its own polling.
    - Disk half, ratified: the folder of a dropped row is removed AT THE
      DROP — GenropyWorker hooks the demolition (override of
      `demolish_page`/`demolish_connection`: super() first, then
      `shutil.rmtree(<site connections folder>/<cid>[/<page_id>],
      ignore_errors=True)`) so expiry, logout and cascades all clean the
      same way; plus a periodic ORPHAN pass in the sweep loop (a folder
      whose connection is not in `connection_items` and older than
      `connection_max_age` by mtime is removed) — sufficient in the single,
      where the worker sees every connection.
    - Client side: `claim_cleanup` returns False always; `expire_pages`/
      `expire_connection` become documented no-ops (the worker sweeps);
      `DEFAULT_PAGE_MAX_AGE` in the shim aligns to 600.
  - Details: arm and parametrize the sweep, hook the demolitions, write the
    orphan pass, neuter the client cleanup trio; tests with tiny ages
    (seconds) through the public API: a guest connection expires before a
    logged one, a page folder disappears at drop_page, an orphan folder
    disappears at the pass, claim_cleanup is False.
  - Done: `pytest tests/test_expiry_and_disk.py` passes; `ruff check src/
    tests/` zero errors.

- [ ] **Phase 6**: single-process suite migrated
  - Pattern reference: the migrated tests of Phases 1-5 (same fixture style);
    `tests/test_legacy_e2e.py` (the e2e suite being migrated)
  - Files: tests/test_legacy_e2e.py, tests/test_genropy_proxy.py,
    tests/test_worker_application.py, tests/test_cli_multiworker_e2e.py,
    tests/__init__.py and shared fixtures as needed
  - Decisions:
    - test_legacy_e2e.py migrates to the new single (front + GenropyWorker
      via the recipe); an assertion whose MEANING changed with the core
      model (e.g. dbevents origin delivery is now local-first but the page
      outcome is equivalent — verbale a6) is rewritten to assert the
      equivalent observable and FLAGGED in notes.md, never silently flipped.
    - test_genropy_proxy.py: import fix only (`OpenApiApplication` from the
      core's `applications/openapi.py` path).
    - test_worker_application.py and test_cli_multiworker_e2e.py: pool-only —
      module-level `pytest.skip("pool bridge is stage two", 
      allow_module_level=True)` with the reason, listed in notes.md.
  - Details: migrate file by file; the full suite is the gate.
  - Done: `pytest tests/` passes (skips only the two declared pool modules);
    `ruff check src/ tests/` zero errors.

- [ ] **Phase 7**: Coherence review and auto-fix (final, mandatory)
  - Pattern reference: same as Phases 1..6 (cross-check against them)
  - Files: only the files written by Phases 1..6 (collect them from their
    `Files:` fields). Never touch a pre-existing file they did not modify.
  - Decisions:
    - Auto-fix directly: tool-fixable lint (ruff), unused imports,
      formatting, trivially mechanical fixes. Re-run the tests after each
      non-tooling fix; if one breaks a test, roll back that fix and flag it
      instead.
    - Never auto-fix: logic errors, design divergences from the pattern
      reference, missing edge cases, anything architectural. Those go to
      `review.md` only.
  - Details: convergence loop (max 3 cycles) of linter scoped to the file
    set → auto-fix → linter → test suite; stop early if a cycle makes no
    progress. Then write `.phased/active/wf4-core-bridge/review.md` with
    three sections: **Auto-fixed** (file, what, tool), **Flagged for human**
    (file, description, suggested action), **Final state** (linter output,
    suite result, files reviewed).
  - Done: `review.md` exists in the plan directory with the three sections,
    linter zero errors on the file set, full suite green.

## Notes
- Decision record: `temp/design_WF4_ponte_2026-08-12.md` (all §7 items 🟢)
  plus its ADDENDUM (login-stays supersedes the old §3.1 reading). The core
  authority is the genro-asgi source at >=0.30 — never the verbale's line
  numbers, which photograph 0.28/0.29.
- PREREQUISITE FOR PHASE 5 ONLY: a small core issue — the three expiry ages
  (`page_max_age`, `guest_max_age`, `connection_max_age`, the ratified
  names) become `UserStickyWorker` constructor kwargs defaulting to today's
  module constants, read by `sweep_expired`/`is_guest_connection`. Phases
  1-4 do not depend on it. To be commissioned on genro-asgi before Phase 5
  runs (same liturgy as #11).
- The pool bridge (GenropyWorker spawned via worker_entry, multi tests,
  orphan disk pass with the global picture, the `guest_inventory` core op,
  S1/S2/BENCH benchmarks) is STAGE TWO — a separate workflow after this one
  is consolidated.
- Cemented rules that bind every phase: Bag legacy everywhere in the bridge
  stores (B1); the two Bag types never convert (`::X` core / `::BAG`
  legacy); one explicit method per register command (no `__getattr__`
  dispatch); imports at module top; no module-level mutable state; tests
  build state through the public API only.
- The GnrWsgiSite requires a working GenroPy environment (the suite already
  does): sub-sessions run where `gnr.*` imports resolve.

## Suggested execution config
| Phase | Effort | Model |
|-------|--------|-------|
| Phase 1 | high | opus |
| Phase 2 | high | opus |
| Phase 3 | high | opus |
| Phase 4 | medium | opus |
| Phase 5 | medium | opus |
| Phase 6 | medium | opus |
| Phase 7 | xhigh | opus |
