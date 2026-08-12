# Coherence review — wf4-core-bridge, Phase 7

Scope: only the files written by Phases 1–6 (their `Files:` fields).
Convergence loop: linter on the file set → auto-fix → linter → full suite.
Converged at cycle 2 (cycle 1 found zero lint errors; one mechanical
docstring fix triggered the second verification round).

## Auto-fixed

| File | What | Tool |
|------|------|------|
| src/genropy_asgi/spa/cli.py | Stale docstring: "the workers reach the commander back-channel at its own address" described the dead pre-rebase model; reworded to the pool-channel reality. | hand (mechanical, docstring only) |

No tool-fixable lint, unused imports or formatting issues existed: ruff was
already zero on the file set at cycle 1 (each phase gated on it).

## Flagged for human

- **`GenropyRegisterClient._add_data_to_register_item`
  (siteregister_client.py) is dead compat**: kept through the rewrite as
  "the daemon's RemoteStoreBag proxy replacement (compat name)", but a grep
  over the whole legacy `gnr.*` tree finds NO caller. Suggested action:
  delete it at the next touch — not auto-fixed because the compat name may
  have callers outside this checkout (customer instances, other packages).
- **The proxy's `route_cleanup` has no core seam in 0.30** (flagged already
  in Phase 6's notes, repeated here because it survives the run): the 0.2x
  `make_callable` hook that ran it per-dispatch is gone, so the per-request
  thread-local db cleanup never fires. Suggested action: commission the seam
  on genro-asgi (same liturgy as the expiry-kwargs issue) or redesign the
  proxy's cleanup; until then long-lived executor threads keep their legacy
  db connections open.
- **The pool bridge is stage two, on record**: `test_worker_application.py`
  and `test_cli_multiworker_e2e.py` skip at module level with that reason;
  the `--workers N` CLI shape boots the core spawn path but is not validated
  by this workflow.
- **`--reload` is accepted-and-ignored** (cli.py prints a notice): the core
  0.30 server has no reloader. Suggested action: none until the core grows
  one; the flag stays for surface compatibility.

## Final state

- Linter on the 19-file set: **zero errors** (`ruff check` clean).
- Full suite: **92 passed, 2 skipped** — the skips are exactly the two
  declared pool modules, nothing else.
- Files reviewed: legacy_bag.py, genropy_worker.py, spa/__init__.py,
  genropy_spa_application.py, config.py, cli.py, genropy_asgi/__init__.py,
  siteregister_client.py, siteregister.py, proxy/genropy_proxy.py,
  pyproject.toml, and the eight test modules of Phases 1–6.
- Cross-checks run: stale references to the deleted application modules
  (none left in src/tests outside the two declared skip modules), dead
  symbols (one flagged above), docstring-vs-behavior drift (one fixed,
  above), module-top import discipline (the `gnr.*` exceptions are the
  ratified ones, documented in notes.md Phases 1–2).
