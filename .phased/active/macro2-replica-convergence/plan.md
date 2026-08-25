# Context: wf/macro2-replica-convergence
Parent: main
Mode: interactive
Must not break: the two recorders stay installable as a plain call on both stacks (macro-phase 3 reinstalls them; no logic may live in the gunicorn hook)
Must not break: every recorded line keeps `duration_ms`, and HTTP lines keep the `X-Gnr*` headers, from the start (macro-phase 3 reads the same traces or re-instruments from scratch)
Must not break: no format versioning — the `site_caller` field lands ONCE in this workflow and the reference sessions are re-produced (the reference is a recipe, never an archive); no `schema_version` field, no reader negotiating shapes
Must not break: a promoted column in the archive is a COPY of what the JSON line holds, never the only place a value lives
Must not break: every comparative run PROVES the two stacks run identical genropy source before starting, and refuses to start otherwise (owner, 2026-08-24; `genropy_parity_check.py` at cycle start — macro-phase 3 inherits the precondition unchanged)

## Objective

Build the replica: the owner performs a session in a browser on the legacy
stack, the replica reproduces it against the bridge by network calls and stops
at the FIRST divergence; then genro-asgi, genropy-asgi or the replica itself is
fixed until the two stacks agree. Ends at the roadmap border: the owner's
reference session replicated on the bridge with no divergence left unexplained,
the known ones (S1/S2/S3/S5, `temp/problemi_genro_asgi_dal_ponte_2026-08-22.md`)
recognised rather than discovered.

## Work Plan

- [x] **Phase 1**: `site_caller` field on every register line
  > Done: every register line of both stacks carries `site_caller`, the three
    outermost site frames joined by ` <- `, and the field is a promoted column
    of the archive so one query says which call path a run spends its calls and
    milliseconds on. The path is cut by the frame's dotted module name, which is
    what makes the same file read identically on the frozen copy legacy runs and
    on the editable checkout the bridge runs.
  > Files: benchmarks/compare/register_recorder.py,
    benchmarks/compare/register_recorder_check.py,
    benchmarks/compare/bridge_coverage_check.py,
    benchmarks/compare/run_archive.py,
    benchmarks/compare/run_archive_check.py,
    benchmarks/compare/drive_login.py
  > Verified: register_recorder_check.py 44 assertions green (legacy venv);
    run_archive_check.py 19 green; bridge_coverage_check.py green except the two
    recipe-drift assertions the foreman excepted (Phase 4 repairs them); a
    drive_login smoke on each stack, 384 register lines per stack, none without
    the field, and the caller sets differing only by the six-line offset between
    the two genropy trees — the reason Phase 2 now refuses to run without parity.
  > Review: the bench recipe has drifted from the shipped one since 7cd15de
    (engine_factory/engine_kwargs): the bench bridge spawns workers that build
    their own site while the shipped bridge forks them from a template. Found
    here, repaired by Phase 4.
  > Verify: now — done 2026-08-25: the owner read the sample lines and confirmed
    the three-frame chain is enough to work Phase 6 with.
  - Run: opus / medium
  - Pattern: `benchmarks/compare/register_recorder.py` (the line builder — the
    bridge inherits it, so the field lands once and appears on both stacks);
    check style: `benchmarks/compare/register_recorder_check.py`
  - Files: benchmarks/compare/register_recorder.py,
    benchmarks/compare/register_recorder_mixin.py (if the walk needs it),
    benchmarks/compare/register_recorder_check.py,
    benchmarks/compare/bridge_coverage_check.py,
    benchmarks/compare/run_archive.py,
    benchmarks/compare/run_archive_check.py,
    benchmarks/compare/drive_login.py
  - Decisions: field name `site_caller` (owner delegated naming for internal
    instrumentation, 2026-08-24); content `file:line function` of the THREE
    outermost-going frames OUTSIDE the register client and the recorder,
    innermost first, joined by ` <- ` (owner, 2026-08-24, after the first
    measurement showed one frame naming only genropy's service cache check on
    242 of the 384 calls a login makes); the field is ALSO a promoted column of
    the archive, a copy, so that `GROUP BY site_caller` answers which call path
    a run spends its calls and milliseconds on (owner, 2026-08-24 — a table of
    chains with an id was weighed and refused: it would make the chain live in
    one place only); stack inspection cost per call is accepted — the instrument
    may cost time while measuring fidelity, never while measuring performance,
    and that mode is macro-phase 3's to build as a declared run condition.
  - Details: extend the recorded register line with `site_caller`. Only the
    outermost site call is recorded (existing rule), so the walk stops at the
    first frame that belongs neither to the recorder nor to the register client
    module. Update the check script to assert the field's presence and shape on a real
    line from both stacks (legacy via `drive_login`, bridge via the same driver
    on 8098). The legacy wrapper path is asserted in register_recorder_check.py
    (legacy venv), the bridge mixin path in bridge_coverage_check.py (bridge
    interpreter): the mixin does not import on the legacy venv, measured
    2026-08-24.
  - Done: `python benchmarks/compare/register_recorder_check.py` passes;
    `python benchmarks/compare/bridge_coverage_check.py` passes on the bridge
    interpreter (its two recipe-drift assertions excepted — they fail for the
    drift Phase 4 repairs, out of this phase's scope, recorded in notes.md);
    a `drive_login` smoke on each stack produces register lines
    whose `site_caller` names a real `gnr/web` file and line.
  - Verify: now — read one sample line per stack: the named caller is the SITE
    code you expect (not the recorder, not the client), and the field reads
    well enough to diagnose Phase 6 with.

- [x] **Phase 2**: the replica — trace reader, network driver, identifier adaptation
  > Done: `replica.py` reads an archived run and performs it again against a live
    stack by network calls, adapting the identifiers the target mints; two
    declared rules say what it never replays (`/_ping`, statics) and one says
    which recorded status is a race of the reference session rather than a
    divergence of the stack. `genropy_parity_check.py` refuses every comparative
    run while the two stacks are not on the same genropy, and the bench now PINS
    that genropy on a detached worktree both stacks read.
  > Files: benchmarks/compare/replica.py,
    benchmarks/compare/replica_check.py,
    benchmarks/compare/genropy_parity_check.py,
    benchmarks/compare/serve_legacy.py,
    benchmarks/compare/serve_bridge.py,
    benchmarks/compare/README.md,
    .phased/active/macro2-replica-convergence/notes.md
  > Verified: replica_check.py 39 assertions green; genropy_parity_check.py exits
    0 on the pinned trees and 1 naming web/gnrdummysite.py on a difference
    introduced on purpose and then restored; the replay of
    legacy-20260823T232924.sqlite against the legacy stack answered 30 statuses
    exactly and 1 as a recognised race, exit 0, archived as
    legacy-20260825T080505.sqlite — 1379 register lines, 0 naming an absent
    exchange, all 31 exchanges carrying `X-Bench-Replica-Of`; ruff clean.
  > Verify: now — done 2026-08-25: the owner watched a replica run end to end and
    inspected the pin — the worktree detached at 6da02feda, temp/gnr/environment.xml
    naming it for gnrhome/packages/resources/webtools/static and NOT for projects,
    and the legacy venv's direct_url.json pointing at the worktree.
  - Run: opus / high
  - Pattern: `benchmarks/replay_a1.py:build_plan` (extracting an ordered call
    plan from a capture) and `benchmarks/scaling_probe.py:login_user` (replaying
    login calls on a keep-alive connection with the identity rewritten in both
    places)
  - Files: benchmarks/compare/replica.py (new),
    benchmarks/compare/replica_check.py (new),
    benchmarks/compare/genropy_parity_check.py (new)
  - Decisions: the replica drives by NETWORK CALLS, never a browser (owner
    accepted the recommendation, 2026-08-24); it reads the legacy trace straight
    from the per-run SQLite archive (the recorded trace IS what the replica
    reads — roadmap, "there are no macros"); identifiers (session cookie,
    connection id, page ids) are adapted per stack, iteratively, starting from
    the set `login_user` already rewrites; genropy PARITY is a precondition the
    replica enforces from birth (owner, 2026-08-24, on measured evidence: the
    frozen legacy venv and the editable checkout differed by 9 files / 181
    lines, and an uncommitted genropy edit was about to remove the very
    register calls the comparison counts) — `genropy_parity_check.py` diffs
    the two genropy source trees (source only, no `__pycache__`), exits
    non-zero NAMING the differing files and the remedy (re-freeze
    temp/legacy_venv from the checkout per the bench README), and the replica
    calls it FIRST at every cycle start: refusal, never a warning. What the
    replica reads is ANY archived run — the reference session is a role, not
    a format (foreman, 2026-08-25, answering this phase's clarify): Phase 2
    replays the archived browser session of 2026-08-23
    (~/genro_bench/runs/legacy-20260823T232924.sqlite, 266 HTTP exchanges),
    which exercises the whole identifier-adaptation surface at no owner cost;
    its register lines predate `site_caller`, which is irrelevant here — the
    replay reads HTTP lines only and records a fresh trace.
  - Details: read one run's HTTP exchanges from the archive in `ts` order,
    skip what a browser session carries that a replica must not replay
    (`/_ping` heartbeats stay OUT — decision at execution, recorded in notes),
    replay each exchange against a target host:port on a keep-alive session,
    substituting the identifiers minted by the target for those in the trace.
    The run writes its own recorded trace through the existing recorders (the
    target runs with recorders on), archived per run as today.
  - Done: `python benchmarks/compare/replica_check.py` passes;
    `python benchmarks/compare/genropy_parity_check.py` exits 0 on aligned
    trees, and non-zero naming the file on an artificially introduced
    difference; the replica refuses to start while the check fails; the replica
    replays the full archived browser session of 2026-08-23
    (legacy-20260823T232924.sqlite, 266 exchanges) against the LEGACY stack itself
    with zero HTTP-level failures, and the new run is archived. An HTTP-level
    failure is a failure the REPLICA caused: a transport error, or a status the
    target could not produce. An exchange whose recorded status only a race of
    the reference session could produce — the recorded reply says the
    connection was already rotated, AND the trace shows the exchange
    overlapping an earlier one on the same pre-rotation cookie — is a
    recognised race of the reference, not a divergence of the stack: declared
    in notes.md, reported by the replay, and promoted to a DECLARED rule in
    Phase 3's rules table (foreman, 2026-08-25, answering this phase's
    clarify; the replica replays in order, not in timing, by approved design).
  - Verify: now — watch one replica run end to end; the exchanges scroll in the
    order of your original session and the run lands in `~/genro_bench/runs/`.

- [x] **Phase 3**: structural comparison, stop at the first divergence
  > Done: `structural_diff.py` compares a reference run with the replica run
    reproducing it, exchange by exchange, joined by the `X-Bench-Replica-Of`
    header: same sequence of register calls, same SHAPE of arguments and answers,
    with the identifiers, timestamps and dates a second run legitimately changes
    masked, a Bag answer compared by its node paths and a register item by its key
    names. The replay asks after every exchange and STOPS at the first divergence
    nothing declares, printing the exchange, the register call number, the two
    lines side by side and the `site_caller` of both. The declared-rules table
    ships as a mechanism with the one rule Phase 2 measured, `reference-race`,
    moved here out of `TraceReader`; its S section is empty by decision, because
    S1/S2/S3/S5 produce no register line on a legacy-vs-legacy run and their
    signatures become observable at the first bridge cycle.
  > Files: benchmarks/compare/structural_diff.py,
    benchmarks/compare/structural_diff_check.py,
    benchmarks/compare/replica.py,
    benchmarks/compare/replica_check.py,
    benchmarks/compare/README.md,
    .phased/active/macro2-replica-convergence/notes.md
  > Verified: structural_diff_check.py 33 assertions green; replica_check.py,
    http_recorder_check.py, run_archive_check.py, register_recorder_check.py all
    green; ruff clean; pytest tests/ 133 passed. The self-check, re-run at the
    close: reference legacy-20260825T085605 (4 exchanges, 384 register lines)
    replayed against the legacy stack recording into legacy-20260825T085646 (4
    exchanges, 384 register lines, 0 unjoinable) — every status exact, zero
    divergences, exit 0. The provoked divergence was produced by replaying one
    reference twice against a stack whose register the first replay had populated:
    it stops on `getItem(CACHE_TS._mainpref_)` against
    `getItem(CACHE_TS.alexander.king_preference)`, the two `site_caller` chains
    naming different site code, and exits 1.
  > Verify: now — done 2026-08-25: the owner read the zero-divergence report of
    the self-check and the provoked divergence report, both without opening the
    code.
  - Run: opus / high
  - Pattern: the join used by the timing queries (record.exchange_id links
    register lines to their HTTP exchange); check style `*_check.py`
  - Files: benchmarks/compare/structural_diff.py (new),
    benchmarks/compare/structural_diff_check.py (new), replica.py (the stop)
  - Decisions: equal means equal by STRUCTURE (roadmap): same sequence of
    register verbs per exchange, same shape of arguments and answers — values
    that legitimately differ per run (ids, timestamps, the customer read) are
    not divergences; the declared-rules table ships as a real MECHANISM whose
    first entry is the reference-race rule Phase 2 measured — rules are
    written only from OBSERVED signatures, never from documents (the S1/S2/
    S3/S5 items are cross-worker facts of the bridge, unobservable on a
    legacy-vs-legacy run: Phases 5/7 add each S-rule the moment its divergence
    shows, with the owner's sign-off, as Phase 7's Decisions already require;
    foreman, 2026-08-25, answering this phase's clarify); a recognised rule is
    reported as "known" and does not stop the run; the self-check reference is
    a FRESH legacy run recorded with drive_login (the 2026-08-23 archive
    predates site_caller, the genropy pin and the page-register state — 21/31
    exchanges differ for the archive's age, measured, not for the stack).
  - Details: compare exchange by exchange, in order: for each replayed HTTP
    exchange, the register lines of the reference run and of the replica run
    must carry the same verb sequence and the same argument/answer shape.
    On the first unexplained divergence the replica STOPS and prints a
    readable report: the exchange (method, rpc_method), the position in the
    sequence, the two lines side by side, the `site_caller` of both.
  - Done: `python benchmarks/compare/structural_diff_check.py` passes;
    replica-vs-legacy on the reference session reports ZERO divergences
    (the self-check that validates comparison and adapter together).
  - Verify: now — read the zero-divergence report of the self-check and one
    artificially provoked divergence report: both must be readable without
    opening the code.

- [x] **Phase 4**: align the bench bridge recipe with the shipped template-fork recipe
  > Done: the bench recipe declares `engine_factory`/`engine_kwargs` exactly as the
    shipped one does, so the bench bridge's workers are born by FORK out of a
    template that builds the site once. The register recorder moved with the site
    construction, into a new `recording_engine_factory.py` (the shipped
    `GenropySiteEngineFactory` subclassed); `recording_worker.py` keeps the HTTP
    recorder and gives the inherited register recorder the run's archive, both in
    the forked child. The two recipe-drift assertions of
    `bridge_coverage_check.py` are green again and now license exactly two
    differences, worker class and engine factory, still comparing the whole
    rendered document.
  > Files: benchmarks/compare/recording_engine_factory.py (new),
    benchmarks/compare/recording_worker.py,
    benchmarks/compare/bridge_recipe.py,
    benchmarks/compare/bridge_coverage_check.py,
    benchmarks/compare/run_archive_check.py,
    benchmarks/compare/README.md,
    .phased/active/macro2-replica-convergence/notes.md
  > Verified: bridge_coverage_check.py 41 assertions green INCLUDING the two
    recipe-drift ones; run_archive_check.py 19 and register_recorder_check.py 44
    green on the legacy venv; http_recorder_check.py, replica_check.py,
    structural_diff_check.py green; ruff clean; pytest tests/ 133 passed. The
    `drive_login` smoke on the bench bridge: the template logs `forked pool_0001`,
    the worker presents at once, and the run archives **380 register lines
    carrying an exchange over 52 distinct `site_caller` chains — identical to the
    four previous spawn-recipe runs**, every one written by the forked worker's
    pid and none by the template's; 0 lines without a caller, 0 without an
    exchange, 0 unjoinable.
  > Review: the template must never open a sqlite connection, and the plan's
    stated reason (an inherited handle) is not the real one. Measured on the
    bridge interpreter (pyenv 3.12.9, sqlite 3.51.0): a forked child dies of
    SIGSEGV once its PARENT has opened any connection, on any file, WAL or not,
    closed or not — intermittently, two runs in three. The legacy venv's sqlite
    3.50.4 is clean, which is why the gunicorn stack needs no change. Anyone
    moving the bench to another interpreter has to re-measure this.
  > Verify: now — read one register line recorded by a forked worker and the
    README's updated run recipe: the install point (register recorder in the
    template through the engine factory, HTTP recorder in the child) is
    documented, and the line is indistinguishable in shape from Phase 1's sample.
  > Verify: now — done 2026-08-25: the owner ACCEPTED the loss of the four
    register calls the site makes while it is being built, with no helper process
    to keep them. The no-write template stands as the design.
  - Run: opus / high
  - Pattern: the shipped recipe `src/genropy_asgi/spa/config.py` as of commit
    7cd15de (engine_factory/engine_kwargs — workers fork from a template);
    the bench copy `benchmarks/compare/bridge_recipe.py`;
    `benchmarks/compare/recording_worker.py` (today's recorder install point)
  - Files: benchmarks/compare/bridge_recipe.py,
    benchmarks/compare/recording_worker.py,
    benchmarks/compare/bridge_coverage_check.py,
    benchmarks/compare/serve_bridge.py (if the wiring moves),
    benchmarks/compare/README.md
  - Decisions: the bench bridge exercises the SHIPPED protocol, never a
    bench-only variant (dev-deploy parity, the owner's standing principle):
    workers fork from the template exactly as the shipped recipe does; the
    recorders install in the TEMPLATE process through the engine factory, so
    every forked worker inherits them with the site — installing in the worker
    constructor is dead under fork, the site exists before the constructor runs.
  - Details: port engine_factory/engine_kwargs into bridge_recipe.py mirroring
    the shipped recipe; move the recorder installation from the worker
    constructor into the engine factory; the two recipe-drift assertions in
    bridge_coverage_check.py turn green again and STAY the guard against the
    next drift. The archive SQLite connection opens in the CHILD after the
    fork, never in the template — an inherited sqlite connection is the known
    segfault family (sqlite 3.51.0 + WAL + fork), and one-connection-per-process
    is the archive's standing rule. Each forked worker must also start with
    empty recorded state (the register-empty-per-run rule).
  - Done: `python benchmarks/compare/bridge_coverage_check.py` passes INCLUDING
    the two recipe-drift assertions; a `drive_login` smoke on the bench bridge
    shows workers born by fork from the template and register lines carrying
    `site_caller` exactly as Phase 1 shaped them.
  - Verify: now — read one register line recorded by a forked worker and the
    README's updated run recipe: the install point (template, via engine
    factory) is documented, and the line is indistinguishable in shape from
    Phase 1's sample.

- [x] **Phase 5**: db copied on the fly, first run against the bridge
  > Done: a replica cycle against the bridge runs end to end — the copy db
    dropped and recreated from the reference db, the bridge serving the twin
    instance `test_invoice_pg_replica` with both recorders, then the replay — and
    it stopped at the FIRST real divergence with the Phase 3 report. The copy is
    a README recipe step run by hand before the launcher; `replica.py` gained the
    refusal that makes the cycle safe: when the two archives declare different
    stacks and the same `database.dbname`, the replay never reaches the wire.
  > Files: benchmarks/compare/replica.py,
    benchmarks/compare/replica_check.py,
    benchmarks/compare/README.md,
    .phased/active/macro2-replica-convergence/notes.md
  > Verified: replica_check.py 28 assertions green including the 8 new ones (two
    stacks on one db refused, two stacks on two dbs allowed, same stack on one db
    allowed so the Phase 3 self-check survives, and the refusal at Replica level);
    structural_diff_check.py, http_recorder_check.py, run_archive_check.py,
    register_recorder_check.py, bridge_coverage_check.py all green; ruff clean;
    pytest tests/ 133 passed. The refusal proved on a real pair: reference
    legacy-20260825T085605 against bridge-20260825T092537 (bridge on
    test_invoice_pg) exits 1 naming both runs and printing the two copy commands.
    The cycle: reference legacy-20260825T085605 (4 exchanges, 384 register lines)
    against the bridge on the copy db, archived as bridge-20260825T113535 — the
    template forked pool_0001, the worker presented at once, 298 register lines
    carrying an exchange, 0 without a site_caller; exchange 1 answered 200 and the
    comparison stopped inside it at register call 5.
  > Review: the divergence is NOT the login one the plan expected. It stops in the
    FIRST exchange, on the key set of the connection register item answered by
    `client:new_connection` — the reference carries `datachanges`,
    `datachanges_idx`, `electron_static`, `register_name`, `subscribed_paths`, the
    bridge carries `avatar_extra`, `last_refresh_ts`, `last_rpc_ts`,
    `last_user_ts`, `store`; both `site_caller` chains name the same site code
    (`gnrwebpage.py:325 _register_new_page`). Phase 6's premise therefore changed:
    it opens on this, not on the login segment, and the login divergence sits
    behind it. Re-planning is the foreman's.
  > Review: the shape is defined in genropy-asgi's own
    `src/genropy_asgi/siteregister/siteregister_client.py` (`EPOCH_STAMPS`:90,
    `avatar_extra`:1380, `subscribed_paths`:159), so uniforming lands here and not
    in the core — no five-step routing to the core session for the shape itself.
  > Verify: now — done 2026-08-25: the owner read the divergence report, judged it
    precise enough to start Phase 6 from, and decided the direction is to UNIFORM
    the connection register item — a defect to fix, not a rule to declare.
  - Run: opus / medium
  - Pattern: the twin-instance recipe of macro-phase 1 (test_invoice_pg_legacy:
    same project, own instanceconfig); `benchmarks/compare/serve_bridge.py`
  - Files: benchmarks/compare/replica.py (cycle start), a new twin instance
    `test_invoice_pg_replica` under the test_invoice project (instanceconfig
    pointing at the copied db), benchmarks/compare/README.md
  - Decisions: the copy is `createdb -T test_invoice_pg test_invoice_pg_replica`
    (drop-and-recreate at each cycle start; writes are allowed on the bridge
    side from the start — roadmap); the bridge serves the twin instance
    `test_invoice_pg_replica` in replica cycles; instance naming follows the
    proven `_legacy` twin pattern; the COPY is a README recipe step run by hand
    BEFORE serve_bridge, like every other bench launcher — replica.py does not
    orchestrate the cycle — and replica.py's cycle-start gains a REFUSAL beside
    the parity check: on a CROSS-STACK run — reference and target declaring
    different `stack` values — the target archive's declared `database.dbname`
    must differ from the reference run's, so the bridge can never write into
    the db a legacy reference was recorded against (foreman, 2026-08-25,
    answering this phase's clarify, and narrowed the same day on the phase
    chat's objection: read literally the rule would refuse the Phase 3
    self-check, legacy against legacy on one db, which is the run that proves
    the comparison works. A same-stack replay DOES write into the db its
    reference was recorded against — harmless for a login-only reference,
    but Phase 7's session carries saveRecordCluster, so replaying it twice
    starts from two different db states: copy the db for repeat same-stack
    replays too, or expect a divergence the stacks did not cause);
    Phase 5 replays the drive_login legacy reference (the Phase 3 self-check
    reference: 4 exchanges, the login segment in full) — the owner's browser
    session is Phase 7's reference, performed at ITS cycle start, not now.
  - Details: a replica cycle against the bridge begins by dropping and
    recreating the copy db from the reference db, then starts the bridge on the
    twin instance with recorders on, then replays. Document the cycle in the
    bench README (start, hygiene, teardown — same register-empty rule as
    every run).
  - Done: one full cycle runs end to end (copy, serve, replay) and stops at
    the FIRST real divergence with the Phase 3 report; the run is archived.
  - Verify: now — the first divergence report against the bridge: expected at
    the login (the +28% register calls); confirm the report names it precisely
    enough to start Phase 6 from.

- [ ] **Phase 6**: uniform the connection register item
  - Run: opus / medium
  - Pattern: `src/genropy_asgi/siteregister/siteregister_client.py`, where the
    item's shape is defined today (`EPOCH_STAMPS:90`, `avatar_extra:1380`,
    `subscribed_paths:159`); the legacy answer recorded in the Phase 5 report
    is the target shape; contract tests under `tests/`, implementation tests
    under `tests/x/` (parent CLAUDE.md rule 10)
  - Files: src/genropy_asgi/siteregister/siteregister_client.py, tests/
  - Decisions: the divergence is a DEFECT of the bridge to fix, not an S rule
    to declare (owner, 2026-08-25, after reading the Phase 5 report): the two
    stacks reach the call the same way — both `site_caller` chains name
    `gnrwebpage.py:325 _register_new_page` — and answer it differently, and
    the project's standing rule is that site-facing semantics imitate
    pre_refactoring in full (project CLAUDE.md, cemented decisions). The shape
    is defined in OUR OWN source, so the fix lands in genropy-asgi and needs no
    five-step routing to the core session. The legacy key set is the target:
    the bridge's item must answer what the site expects, not what the bridge
    finds convenient.
  - Details: the first cycle against the bridge stops in the FIRST exchange, at
    register call 5, on the key set of the connection register item answered by
    `client:new_connection` — reference `datachanges`, `datachanges_idx`,
    `electron_static`, `register_name`, `subscribed_paths`; bridge
    `avatar_extra`, `last_refresh_ts`, `last_rpc_ts`, `last_user_ts`, `store`.
    Uniform the item so both stacks answer the same key set, decide per key
    whether the bridge-only ones are dropped or moved out of the answer, and
    re-run the cycle. The `+28%` login divergence (147 vs 115 register calls on
    the three login exchanges) sits BEHIND this one and does not disappear —
    it is Phase 7's, not this phase's.
  - Done: `pytest tests/` green and `ruff check .` clean; a cycle against the
    bridge replaying the drive_login reference passes exchange 1 register
    call 5 — the connection item answers the same key set on both stacks — and
    stops (or completes) beyond it; the run is archived.
  - Verify: now — read the new stop report: the connection item is gone from
    it, and whatever the replay stops on next is named precisely enough to
    work from.

- [ ] **Phase 7**: converge the drive_login reference end to end
  - Run: opus / high
  - Pattern: the Phase 5 cycle, repeated; the excluded hypotheses on the +28%
    are on record (bridge code does not call the register itself; the register
    does not answer differently) — do NOT re-test them,
    `temp/problemi_ponte_2026-08-22.md`
  - Files: unknown until each divergence shows — the fix lands where the fault
    is (genropy-asgi here, genro-asgi via its own session per the five-step
    rule, or the replica itself)
  - Decisions: iterate divergence by divergence, the owner judging where the
    fault lies at each stop; a fix in the genro-asgi core is NOT implemented
    here — it is written as problem→solution→prompt for the core session and
    this phase waits on it (`[~]` blocked is expected, not a failure); a
    divergence that is neither fixed nor fixable becomes a NAMED declared rule
    in the Phase 3 table with the owner's sign-off — nothing is silently
    tolerated. The `+28%` login divergence is expected among these stops and
    carries its own requirement: the cause is NAMED, never described by its
    symptom.
  - Details: repeat the cycle — copy db, replay the drive_login reference
    against the bridge, stop, judge, fix, restart — until the four exchanges
    replay with no unexplained divergence. Write one paragraph per closed
    divergence in notes.md: what it was, where it was fixed, why.
  - Done: a full cycle of the drive_login reference against the bridge
    completes with zero unexplained divergences; the register-call counts of
    the two stacks agree on the login segment; the run is archived.
  - Verify: now — read the clean report and the notes paragraphs: every closed
    divergence has a named cause, and every recognised one a declared rule you
    signed.

- [ ] **Phase 8**: full-session convergence
  - Run: opus / medium
  - Pattern: the Phase 7 loop, on the owner's own browser session
  - Files: unknown until the divergences show — same routing rule as Phase 7
  - Decisions: known divergences (S1/S2/S3/S5) are recognised by the Phase 3
    rules and reported, never "fixed" here — they are core work with their own
    track; every unexplained divergence is either fixed or becomes a named,
    declared rule with the owner's sign-off (nothing is silently tolerated);
    the db is copied for repeat replays of THIS reference even same-stack, as
    it carries `saveRecordCluster` (Phase 5's caveat).
  - Details: the owner performs a fresh reference session in the browser on the
    legacy stack, with the recorders as Phase 1 shaped them; then iterate the
    full cycle — copy db, replay, stop, judge, fix, restart — until the whole
    session replicates with no unexplained divergence.
  - Done: one full cycle of the owner's reference session against the bridge
    completes with zero unexplained divergences; the closing run is archived;
    the recognised-divergence list in the report matches the declared rules
    exactly.
  - Verify: now — the roadmap border: you performed the reference session, the
    replica reproduced it on the bridge, and the final report satisfies you
    that "no divergence left unexplained" is true in your own judgment.

## Notes

- Phases run strictly in order. Phase 1 changes the register line shape, so
  the reference sessions are re-produced AFTER it lands (the owner performs
  the reference session once, with recorders updated, at the start of the
  Phase 8 cycle; scripted `drive_login` references cover Phases 1-7 — amended
  by the foreman on 2026-08-25 answering Phase 5's clarify: the login segment
  Phases 5-7 work on is fully exhibited by the drive_login reference, so
  the owner's browser session is needed once, at the last phase).
- The `/_ping` heartbeat and other browser-idle traffic: what the replica
  skips is decided at Phase 2 execution and recorded in notes.md — the rule
  must be declared, never implicit.
- The two stacks share 127.0.0.1: cookie residue across ports is a known
  artefact, not a divergence (documented in the bench README).
- Phase 7 may block on the core session (five-step rule: the core is modified
  by its own session). A `[~]` there is expected, not a failure.
- Re-phased by the foreman on 2026-08-25, when Phase 5 measured that the first
  cycle against the bridge stops long BEFORE the login: at exchange 1, register
  call 5, on the connection register item's key set. The old Phase 6 assumed the
  `+28%` was the first thing to meet. Now: Phase 6 uniforms that item (a named,
  bounded defect the owner already ruled on), Phase 7 iterates the remaining
  divergences of the drive_login reference — the `+28%` among them — and Phase 8
  is the owner's own browser session. Phase 6 is the FIRST phase of this
  workflow to touch product code under `src/`: `pytest tests/` is a gate there,
  and a failing contract test is a STOP, never something to adapt.
- Two marker debts for `/quality-check` at the close: Phase 5 wrote no
  `wf:phase-5:new` markers on its new callables (recorded in its notes), and two
  Phase 4 markers still stand in `benchmarks/compare/recording_engine_factory.py`
  (`append_record`, `build_site`). Naming inside `benchmarks/compare` is
  delegated to the executing session by the owner (2026-08-25), so the sweep is
  a marker sweep, not a naming review.
- Genropy parity, the evidence behind the precondition (2026-08-24): at 07:13
  temp/legacy_venv (frozen 2026-08-23) and the editable checkout already
  differed in 9 source files / 181 lines, one shifting gnrwsgisite.py by six
  lines; at 07:17 an uncommitted edit to gnr/lib/services/__init__.py was
  skipping the register read for non-db-configured services — the source of
  242 of the 384 register calls a login makes. Either one would have read as
  a bridge divergence. Before the Phase 2 self-check runs, temp/legacy_venv
  must be re-frozen from the checkout (recipe step, not code).
- The template-fork POC landed in the shipped recipe at commit 7cd15de
  (engine_factory/engine_kwargs, workers fork from a template) while Phase 1
  was in flight; Phase 4 aligns the bench recipe, inserted by the foreman on
  2026-08-24 after the phase-1 chat reported the two recipe-drift assertions
  of bridge_coverage_check.py failing (proposal record:
  temp/proposta_core_template_fork_2026-08-24.md).
