# Context: wf/macro2-replica-convergence
Parent: main
Mode: interactive
Must not break: the two recorders stay installable as a plain call on both stacks (macro-phase 3 reinstalls them; no logic may live in the gunicorn hook)
Must not break: every recorded line keeps `duration_ms`, and HTTP lines keep the `X-Gnr*` headers, from the start (macro-phase 3 reads the same traces or re-instruments from scratch)
Must not break: no format versioning — the `site_caller` field lands ONCE in this workflow and the reference sessions are re-produced (the reference is a recipe, never an archive); no `schema_version` field, no reader negotiating shapes
Must not break: a promoted column in the archive is a COPY of what the JSON line holds, never the only place a value lives

## Objective

Build the replica: the owner performs a session in a browser on the legacy
stack, the replica reproduces it against the bridge by network calls and stops
at the FIRST divergence; then genro-asgi, genropy-asgi or the replica itself is
fixed until the two stacks agree. Ends at the roadmap border: the owner's
reference session replicated on the bridge with no divergence left unexplained,
the known ones (S1/S2/S3/S5, `temp/problemi_genro_asgi_dal_ponte_2026-08-22.md`)
recognised rather than discovered.

## Work Plan

- [>] **Phase 1**: `site_caller` field on every register line
  > In execution since 2026-08-24T22:26:29+02:00
  > Testing: awaiting the human's `Verify: now` checks | commit: cb606d2
  - Run: opus / medium
  - Pattern: `benchmarks/compare/register_recorder.py` (the line builder — the
    bridge inherits it, so the field lands once and appears on both stacks);
    check style: `benchmarks/compare/register_recorder_check.py`
  - Files: benchmarks/compare/register_recorder.py,
    benchmarks/compare/register_recorder_mixin.py (if the walk needs it),
    benchmarks/compare/register_recorder_check.py,
    benchmarks/compare/bridge_coverage_check.py
  - Decisions: field name `site_caller` (owner delegated naming for internal
    instrumentation, 2026-08-24); content `file:line` plus function name of the
    first frame OUTSIDE the register client (and outside the recorder itself);
    stack inspection cost per call is accepted — fidelity phases do not read
    timings.
  - Details: extend the recorded register line with `site_caller`. Only the
    outermost site call is recorded (existing rule), so the walk stops at the
    first frame that belongs neither to the recorder nor to the register client
    module. Keep the field inside the JSON line — no new promoted column.
    Update the check script to assert the field's presence and shape on a real
    line from both stacks (legacy via `drive_login`, bridge via the same driver
    on 8098). The legacy wrapper path is asserted in register_recorder_check.py
    (legacy venv), the bridge mixin path in bridge_coverage_check.py (bridge
    interpreter): the mixin does not import on the legacy venv, measured
    2026-08-24.
  - Done: `python benchmarks/compare/register_recorder_check.py` passes;
    `python benchmarks/compare/bridge_coverage_check.py` passes on the bridge
    interpreter; a `drive_login` smoke on each stack produces register lines
    whose `site_caller` names a real `gnr/web` file and line.
  - Verify: now — read one sample line per stack: the named caller is the SITE
    code you expect (not the recorder, not the client), and the field reads
    well enough to diagnose Phase 5 with.

- [ ] **Phase 2**: the replica — trace reader, network driver, identifier adaptation
  - Run: opus / high
  - Pattern: `benchmarks/replay_a1.py:build_plan` (extracting an ordered call
    plan from a capture) and `benchmarks/scaling_probe.py:login_user` (replaying
    login calls on a keep-alive connection with the identity rewritten in both
    places)
  - Files: benchmarks/compare/replica.py (new),
    benchmarks/compare/replica_check.py (new)
  - Decisions: the replica drives by NETWORK CALLS, never a browser (owner
    accepted the recommendation, 2026-08-24); it reads the legacy trace straight
    from the per-run SQLite archive (the recorded trace IS what the replica
    reads — roadmap, "there are no macros"); identifiers (session cookie,
    connection id, page ids) are adapted per stack, iteratively, starting from
    the set `login_user` already rewrites.
  - Details: read one run's HTTP exchanges from the archive in `ts` order,
    skip what a browser session carries that a replica must not replay
    (`/_ping` heartbeats stay OUT — decision at execution, recorded in notes),
    replay each exchange against a target host:port on a keep-alive session,
    substituting the identifiers minted by the target for those in the trace.
    The run writes its own recorded trace through the existing recorders (the
    target runs with recorders on), archived per run as today.
  - Done: `python benchmarks/compare/replica_check.py` passes; the replica
    replays the full reference legacy session against the LEGACY stack itself
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

- [ ] **Phase 4**: db copied on the fly, first run against the bridge
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
    enough to start Phase 5 from.

- [ ] **Phase 5**: close the login divergence (+28% register calls)
  - Run: opus / high
  - Pattern: the divergence report of Phase 4; the excluded hypotheses are on
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

- [ ] **Phase 6**: full-session convergence
  - Run: opus / medium
  - Pattern: the Phase 4 cycle, repeated
  - Files: unknown until the divergences show — same routing rule as Phase 5
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
  Phase 4 cycle; scripted `drive_login` smokes cover Phases 1-3).
- The `/_ping` heartbeat and other browser-idle traffic: what the replica
  skips is decided at Phase 2 execution and recorded in notes.md — the rule
  must be declared, never implicit.
- The two stacks share 127.0.0.1: cookie residue across ports is a known
  artefact, not a divergence (documented in the bench README).
- Phase 5 may block on the core session (five-step rule: the core is modified
  by its own session). A `[~]` there is expected, not a failure.
- POC template-fork proceeds in the core session independently of this
  workflow (temp/proposta_core_template_fork_2026-08-24.md).
