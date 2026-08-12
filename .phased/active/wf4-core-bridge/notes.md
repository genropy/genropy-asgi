# Working notes — wf4-core-bridge

## Baseline (before Phase 1)

The suite is red at the run's start and the failure is fully attributed to
the rebase gap this workflow exists to close: genro-asgi is installed
editable from `../genro-asgi` at main (0.30), while the bridge source and
tests still import the 0.28/0.29 core module paths
(`genro_asgi.applications.spa_application`, `.asgi_application`,
`.openapi_application` — 33 ModuleNotFoundError, nothing else). This is the
documented starting condition (plan objective, phases 2/3/4/6 rewrite those
importers), not an unowned regression: execution proceeds, each phase gates
on its own `Done:` test files, Phase 6 gates on the full suite.

Observed during the gate check: `../genro-asgi/src/genro_asgi/spa/worker.py`
carries UNCOMMITTED modifications adding the three expiry-age constructor
kwargs (`page_max_age`/`guest_max_age`/`connection_max_age`) — the Phase 5
prerequisite commission is in flight. The gate is re-checked right before
Phase 5 against committed main.

## Phase 1

- **`src/genropy_asgi/spa/__init__.py` touched beyond the declared Files
  list.** The package init eagerly imported the two pre-rebase application
  modules, whose core imports no longer resolve — so `import
  genropy_asgi.spa.legacy_bag` (any import of the subpackage) died at
  collection. The broken re-exports were dropped (docstring updated,
  `__all__` emptied); Phase 4 rewrites this file anyway and restores the
  public face. Rejected alternatives: try/except around the imports
  (defensive code, banned), PEP 562 lazy `__getattr__` (magic for a
  transitional state).
- **Deferred `gnr` imports in `genropy_worker.py`** (`_create_site` body and
  the debug branch), transcribed from the pattern reference
  (`genropy_spa_application._create_site` did the same): importing
  `gnr.web.gnrwsgisite` at module top drags `siteregister_client`, which
  keeps its pre-rebase core import until Phase 2. `gnr.core.gnrbag` stays at
  top (safe, needed by `GenropyRegistry`); `legacy_bag.py` imports `gnr.*`
  at top by ratified design (the `::BAG` registration happens at import).
- **Datetime round-trip is the legacy wire's own semantics**: the legacy
  catalog serializes a naive datetime as aware LOCAL time and
  `parse_datetime` returns it aware — the historical `::BAG` behaviour,
  reproduced verbatim (serializer `toXml(catalog=...)`, parser `Bag(txt)`,
  both ratified). The unit test asserts wall-clock preservation and
  documents the awareness; consumers needing naive values normalize at
  their boundary (the global rail's `_decode_global` already does).
- **Legacy autocreate events are part of the capture**: writing `a.b` into
  an empty legacy Bag fires ins('a', reason='autocreate') then ins('a.b') —
  both captured when under a subscribed prefix, exactly as the legacy
  triggers report them. Tests assert the pair explicitly.
- **The worker-construction test SKIPS at this phase** (site fixture
  pattern, as the plan prescribes): building the GnrWsgiSite imports the
  register client, still broken until Phase 2. Phase 2's `Done:` re-runs
  this file with the client rewired, turning the test live.
