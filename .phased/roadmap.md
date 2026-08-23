# Roadmap — the legacy/bridge comparison bench

**Version**: 1.0 · **Date**: 2026-08-23 · **Status**: 🔴 DA REVISIONARE
**Owner decision (2026-08-23)**: three macro-phases, in this order. The first
two are about FIDELITY, not speed: instrumentation may be as heavy as it likes,
timings are not read. Performance is measured only when fidelity is settled.

## Macro 1 — Data collection on the legacy stack

Start the classic GenroPy site (its own daemon, gunicorn single process with
16 threads) and record two things from it: the HTTP exchanges (what the
browser asks, what we answer) and the Python calls the site makes to its
site register (verb, arguments, answer).

- Mini-scope: `legacy_venv` + twin instance `test_invoice_pg_legacy` on the
  same db; an HTTP recorder and a register interceptor, both installed from
  the gunicorn `post_worker_init` hook; genropy itself untouched.
- Ends at: a browser session driven by hand produces two aligned traces —
  every register call attributable to the HTTP exchange that caused it.

## Macro 2 — Data collection on the bridge, and the comparison

The same two recorders on genropy-asgi, then the structural diff of the two
traces: tags and attributes for XML, keys and shape for JSON, leaf values
normalised. Divergences land in one of two lists — emulation defect, or
licensed divergence.

- Mini-scope: the recorders' install point on the bridge (no gunicorn there);
  the diff tool; the report.
- Ends at: the traces of the collaudo sequence on both stacks, diffed, with
  the known divergences (S1/S2/S3/S5) recognised and no unknown ones left
  unexplained.

## Macro 3 — Performance

Data collection stops. Memory first (RSS and USS over the whole stack, at
parity of workers, at rest and under load), then latencies and load.

- Mini-scope: the memory sampler, the load driver, the percentiles; the
  declared conditions of every run.
- Ends at: a report comparing the two stacks on memory and speed under
  identical, declared conditions.

## Must not break (in transit across the macros)

- The two recorders of Macro 1 are the same instruments Macro 2 installs on
  the bridge: their event format is versioned from the start, or Macro 2
  cannot read what Macro 1 wrote.
- Macro 3 reads the same traces: a format that carries no timing field at all
  would force Macro 3 to re-instrument from scratch.
