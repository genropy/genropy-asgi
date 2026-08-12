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

## Phase 2

- **The legacy `data` seed of `new_page` becomes the row's `store`** — the
  plan's "legacy data passes through" read as: through INTO the live store.
  One Bag serves the dbenv walk, the channel-A writes, the capture (page
  collector + cache observer) and a future move package; two separate Bags
  (store + verbatim `data` field) would have split the capture from the
  writes. `_ensure_item_data` aliases `data` -> `store` on every row: the
  daemon-era name and the core name are one object.
- **`GenropyWorker.apply_forwarded` override — a declared deviation** (file
  not in this phase's Files list; Phase 3 touches it anyway): the core's
  STATE delivery writes with the new Bag API (`set_item`, `_fired`), the
  bridge stores are legacy Bags — the override translates to `setItem(...,
  _attributes=..., _reason=...)` / `pop(path, _reason=...)`, and a fired
  change resets the node's static value silently (the legacy one-shot).
  Without it, `set_datachange(register_name='user')` would crash.
- **`_ship_global` rerouted onto `worker.store_set`/`store_del`** one phase
  early, by necessity: Phase 2 kills `_fold` (the plan's own decision) and
  the global rail rode it. This is exactly Phase 3's ratified ascent; Phase
  3 re-verifies it and owns the rail tests.
- **`change_ts` normalized aware -> naive local at the legacy boundary**
  (`_change_to_client`) — the legacy world compares naive clocks, same
  convention as `_decode_global`.
- **The dbevents disguise lives in `_dbevent_to_client`**: path
  `gnr.dbchanges.<table>` with dots->underscores (the grammar of legacy
  `notifyLocalDbEvents`), origin page and reason as attributes. The core
  species stay separate up to that point.
- **Autocreate parents are part of the legacy capture** (as in Phase 1):
  a first write under a fresh prefix delivers the pair (parent Bag node,
  then the leaf) — the daemon's own triggers reported the same; tests
  assert the pair.
- **Duplicate `new_connection` answers the live row** (a browser
  re-presenting its cookie is a real case, the core `create` would raise);
  `drop_page`/`drop_connection` on a gone row are legitimate no-ops (expiry
  and double logout). Legacy `cascade` kwargs are absorbed: the cascade
  discipline is the core's, cemented.
- **`local_only` of the core `notifyDbEvents` is never used by the bridge**:
  the legacy hidden-transaction path never reaches the register (it goes to
  `page.notifyLocalDbEvents`, page-local list) — verified on
  gnrwebapp.py:142-148.
- **Pool-vs-single in `filter_subscribed_tables`** is structural: a worker
  on a real socket channel is a pool child (pass-through); a `LocalChannel`
  or no channel is the single (filters on `worker.subscriptions.pages_for`).
## Phase 3

- **The wire decodes the ascent's text — by construction, not by accident.**
  `_encode_global` ships TYTX-suffixed TEXT (ratified, unchanged) and the
  master keeps it verbatim (blind courier: the immediate-rail EVENT crosses
  no tytx hop). But every DESCENDING hop — the changes batch, the snapshot,
  the lease grant, the unlock — crosses `to_tytx`/`from_tytx`, and the
  suffix grammar being the shared historical one, the hop decodes the text
  back to the original value (`"7::L"` → 7, `"42::L::T"` → `"42::L"`). So
  values arrive at the edges ALREADY DECODED, and materializing them through
  the text-decoding `apply_global_write` would corrupt a legacy string that
  looks typed. The client therefore has two entry points:
  `apply_global_write`/`load_global_snapshot` keep the TEXT contract
  (the direct seam, stub tests), and `_materialize_global`/
  `_materialize_global_snapshot` take DECODED values (aware→naive
  normalization only) — the worker's `handle_frame` override and the lease
  grant use the latter. Master content is mixed (text from the immediate
  rail, raw from the lease releases) but converges at every edge.
- **Autocreated parents in the descending changes are skipped**: the master's
  collector captures the parent Bag node an intermediate write creates;
  materializing it into the legacy Bag would crash (`setBackRef` on a core
  Bag) and is pointless — the legacy Bag autocreates its own parents, the
  leaves travel as changes of their own.
- **The lease lives in ServerStore('global')'s enter/exit** (client-internal
  `_open_global_lease`/`_close_global_lease`): sync `with` form on the WSGI
  thread; grant materializes the master; thread-local `lease_writes` makes
  `_ship_global` collect instead of shipping; exit applies to the working
  copy and releases (all-or-nothing — a raising body applies nothing).
  `GnrDaemonLocked` wraps any acquire failure. Lease tests run the with-block
  in `asyncio.to_thread` — the WSGI-thread shape it has in production.
- **Phase 3 tests use the REAL single**: `UserStickyCommander(workers=0,
  local_worker=True, worker_class="genropy_asgi...GenropyWorker")` — the
  full protocol on a LocalChannel (dev==deploy), the core's own
  test_spa_single.py fixture pattern.

- **Tests open the CALL sinks with the core's own `call_sink` convention**
  (genro-asgi tests/test_spa_worker.py): the lifecycle ops announce on the
  CALL that causes them, and outside a CALL the events sink is closed by
  design. State building itself goes through the register's public commands
  on a real site.
