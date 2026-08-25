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

- [>] **Phase 2**: the replica — trace reader, network driver, identifier adaptation
  > In execution since 2026-08-25T07:40:00+02:00
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
    with zero HTTP-level failures, and the new run is archived.
  - Verify: now — watch one replica run end to end; the exchanges scroll in the
    order of your original session and the run lands in `~/genro_bench/runs/`.

- [ ] **Phase 3**: structural comparison, stop at the first divergence
  - Run: opus / high
  - Pattern: the join used by the timing queries (record.exchange_id links
    register lines to their HTTP exchange); check style `*_check.py`
  - Files: benchmarks/compare/structural_diff.py (new),
    benchmarks/compare/structural_diff_check.py (new), replica.py (the stop)
  - Decisions: equal means equal by STRUCTURE (roadmap): same sequence of
    register verbs per exchange, same shape of arguments and answers — values
    that legitimately differ per run (ids, timestamps, the customer read) are
    not divergences; the known divergences S1/S2/S3/S5 are recognised from
    DECLARED rules (a small rules table in the module), reported as "known",
    and do not stop the run.
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

- [ ] **Phase 4**: align the bench bridge recipe with the shipped template-fork recipe
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

- [ ] **Phase 5**: db copied on the fly, first run against the bridge
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
    proven `_legacy` twin pattern.
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

- [ ] **Phase 6**: close the login divergence (+28% register calls)
  - Run: opus / high
  - Pattern: the divergence report of Phase 5; the excluded hypotheses are on
    record (bridge code does not call the register itself; the register does
    not answer differently) — do NOT re-test them, `temp/problemi_ponte_2026-08-22.md`
  - Files: unknown until diagnosed — the fix lands where the fault is
    (genropy-asgi, genro-asgi via its own session per the five-step rule, or
    the replica itself)
  - Decisions: what is already excluded stays excluded; the remaining lead is
    that the site is invoked one extra round on login; a fix in genro-asgi core
    is NOT implemented here — it is written as problem→solution→prompt for the
    core session, and this phase waits on it (mark `[~]` blocked if so).
  - Details: with `site_caller` on both traces, name the caller of every extra
    register call in the login exchanges (147 vs 115 on the same three HTTP
    exchanges); locate the extra site invocation; fix on the side that owns the
    fault; re-run the login segment until it converges.
  - Done: the replica run passes the three login exchanges with zero
    unexplained divergences; the register-call counts of the two stacks agree
    on the login segment.
  - Verify: now — read the convergence report of the login segment and the
    one-paragraph cause written in notes.md: the cause must be named, not
    described by its symptom.

- [ ] **Phase 7**: full-session convergence
  - Run: opus / medium
  - Pattern: the Phase 5 cycle, repeated
  - Files: unknown until the divergences show — same routing rule as Phase 6
  - Decisions: known divergences (S1/S2/S3/S5) are recognised by the Phase 3
    rules and reported, never "fixed" here — they are core work with their own
    track; every unexplained divergence is either fixed or becomes a named,
    declared rule with the owner's sign-off (nothing is silently tolerated).
  - Details: iterate the full cycle — copy db, replay the owner's reference
    session, stop, judge (owner decides where the fault is), fix, restart —
    until the whole session replicates with no unexplained divergence.
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
  Phase 5 cycle; scripted `drive_login` smokes cover Phases 1-4).
- The `/_ping` heartbeat and other browser-idle traffic: what the replica
  skips is decided at Phase 2 execution and recorded in notes.md — the rule
  must be declared, never implicit.
- The two stacks share 127.0.0.1: cookie residue across ports is a known
  artefact, not a divergence (documented in the bench README).
- Phase 6 may block on the core session (five-step rule: the core is modified
  by its own session). A `[~]` there is expected, not a failure.
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
