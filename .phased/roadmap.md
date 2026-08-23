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
- Ends at: a browser session driven by hand produces two aligned traces —
  every register call attributable to the HTTP exchange that caused it.

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
- Mini-scope: the recorders' install point on the bridge (no gunicorn there, so
  the install is the plain call macro-phase 1 built); the replica; the structural
  comparison; the db copy.
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
