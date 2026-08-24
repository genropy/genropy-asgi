# Roadmap — the legacy/bridge comparison bench

**Version**: 2.0 · **Date**: 2026-08-23 · **Status**: 🔴 DA REVISIONARE
**Owner decision (2026-08-23)**: three macro-phases, in this order. The first
two are about FIDELITY, not speed: instrumentation may be as heavy as it likes,
timings are not read. Performance is measured only when fidelity is settled.

## Macro-phase 1 — Data collection on the legacy stack

Start the classic GenroPy site (its own daemon, gunicorn single process with
16 threads) and record two things from it: the HTTP exchanges (what the
browser asks, what we answer) and the Python calls the site makes to its
site register (verb, arguments, answer).

- Mini-scope: `legacy_venv` + twin instance `test_invoice_pg_legacy` on the
  same db; an HTTP recorder and a register interceptor, both installed from
  the gunicorn `post_worker_init` hook; genropy itself untouched.
- Ends at: a session performed by hand produces, on BOTH stacks, two traces in
  which every register call is attributable to the HTTP exchange that caused it,
  each run archived out of tree. (Extended on 2026-08-23: the bridge half and the
  archive were moved here from macro-phase 2 — see there for why.)

## Macro-phase 2 — The replica, and converging the two stacks

**Reshaped by the owner on 2026-08-23, superseding version 1.0's offline diff.**
Not: record the bridge, then diff the two traces afterwards. Instead: the owner
performs a session in a browser, a **replica** reproduces it — driving another
browser, or by network calls — and the run stops at the FIRST divergence. Then
genro-asgi, genropy-asgi or the replica itself is fixed until the two stacks
agree, and the run starts again.

The objection that killed the offline version dissolves here: one divergence
contaminating everything downstream costs nothing when nobody walks past the
first one.

- **Equal means equal by STRUCTURE**: same sequence of register verbs, same shape
  of arguments and answers. If the customer read and modified is Mario Rossi in
  one run and Luigi Bianchi in the other, that is not a divergence. Identifiers
  are adapted per stack — each has its own session, page ids, connection ids —
  which the owner accepts as iterative work.
- **The bridge side runs on a database copied on the fly**: a cycle begins by
  copying the db for the genropy-asgi server, so writes are allowed from the
  start on that side.
- **Both recorders matter MORE here, not less**: at each stop, the register trace
  is what says whether the bridge reached the same answer through the same calls,
  which the HTTP layer cannot see.
- **The recorders write straight into a per-run SQLite file** (owner,
  2026-08-23, reversing the JSONL-plus-loader shape decided earlier the same day).
  A truncated JSONL line is possible when a process dies mid-write; a half-written
  SQLite row is not. Lock contention between the bridge's worker processes is real
  as mechanics but harmless where it happens: WAL serialises writers, and the two
  fidelity macro-phases do not read timings — macro-phase 3 does, and runs with
  collection off. What decided it is that a separate load step can be forgotten,
  and the reference session of 2026-08-23 was lost exactly in the window between
  the run and its archiving. One file per run, outside the git tree, on a local
  filesystem (WAL does not work over network mounts), one connection per process.
- **The archive table is one JSON column plus a few promoted ones** (owner,
  2026-08-23), each promoted because it has a job: `exchange_id` and the run id
  to JOIN, the stack to SEPARATE, timestamp and thread to ORDER, the line kind
  and the verb or path and the status to FILTER. Everything else stays inside the
  JSON, so a line of a new shape breaks nothing and needs no migration — the
  no-versioning rule carried down to the storage layer. An occasional query on a
  field that is not a column reads inside the JSON; a field is promoted only once
  it is queried often. One invariant: a promoted column is a COPY of what the JSON
  holds, never the only place a value lives — otherwise the blob stops being the
  record and this is a schema again.
- **Why the archive is needed at all**: macro-phase 1 declared that no macro-phase
  depends on a stored reference trace, only on the ability to produce one. That
  holds for the RECIPE but not for the reference itself — a session performed by
  hand in a browser does not reproduce identically, and the clean-restart recipe
  deletes the traces at every run. Measured and unrecoverable a minute later was
  observed on 2026-08-23. So the reference is kept, outside git, never committed.
- **Two pieces moved OUT of here into macro-phase 1's workflow** (owner,
  2026-08-23): the recorders' install point on the bridge, and the archive with
  its loader. The reason outlives the convenience — the replica cannot be
  designed before the bridge's own trace exists, because only that trace shows
  how far the identifiers actually diverge and what adapting them means. So
  macro-phase 1 now ends with BOTH stacks recorded and both references archived,
  and this macro-phase starts with two traces in hand instead of one.
- Mini-scope: the replica; the structural comparison; the db copy.
- Ends at: the owner's reference session replicated on the bridge with no
  divergence left unexplained, the known ones (S1/S2/S3/S5) recognised rather
  than discovered.

## Macro-phase 3 — Performance

Data collection stops. Memory first (RSS and USS over the whole stack, at
parity of workers, at rest and under load), then latencies and load.

- Mini-scope: the memory sampler, the load driver, the percentiles; the
  declared conditions of every run.
- Ends at: a report comparing the two stacks on memory and speed under
  identical, declared conditions.

## Must not break (in transit across the macro-phases)

- The two recorders of Macro-phase 1 are the same instruments Macro-phase 2
  installs on the bridge, where there is no gunicorn hook: installation must be
  a plain call, never logic living in the hook.
- **No format versioning** (owner, 2026-08-23, superseding version 1.0): the
  traces are consumed immediately, and a format change means re-running the
  collection. No `schema_version` field, no reader negotiating shapes.
- Macro-phase 3 reads the same traces: the duration and the `X-Gnr*` breakdown
  are recorded from the start, or Macro-phase 3 has to re-instrument from
  scratch.
- **There are no "macros"** (owner, 2026-08-23, retiring the word he had
  ratified earlier the same day). The concept is a **replica** of a session the
  owner performs, and in that shape the recorded trace is itself what the replica
  reads — so no sequence is ever written down beside it. The three stages of this
  programme are **macro-phases**; the `macro1` in the first workflow's branch name
  is a historical address, not vocabulary.
- **The reference is a recipe, never an archive.** The traces carry whole bodies —
  the login exchange, the session cookies — and this repository is public, so they
  live in `temp/`, gitignored. No macro-phase may depend on a stored reference
  trace; each depends on the ability to produce one on demand.
