# Claude Code Instructions - genropy-asgi

**Parent Document**: This project follows all policies from the central [meta-genro-modules CLAUDE.md](https://github.com/softwellsrl/meta-genro-modules/blob/main/CLAUDE.md)

Read the parent document first for: language policy (English only), git commit authorship
rules (no Claude co-author), development status lifecycle, standardization requirements,
coding style, mypy policy (advisory, never blocking), and all general policies.

## Project-Specific Context

### Current Status
- **Development Status**: Alpha (`Development Status :: 3 - Alpha`)
- **Version**: 0.6.1
- **Python**: >= 3.11
- **Build**: hatchling, src/ layout, `py.typed`
- **Has Implementation Code**: Yes

### Project Purpose

**genropy-asgi** is the daemon-less bridge that serves GenroPy legacy (synchronous)
sites on top of [genro-asgi](https://github.com/genropy/genro-asgi)'s SPA pool. It was
extracted from `genro-asgi/contrib/genropy_asgi/` to live as its own package, with its own
test suite — so changes here no longer drag the genro-asgi framework test suite.

- **Naming**: package `genropy-asgi` (PyPI, hyphen) · import `genropy_asgi` (underscore).
- Depends on `genro-asgi` as a library (uses its **public API only**: `AsgiServer`,
  `AsgiConfigBuilder`, `SpaApplicationNew`, `SpaWorker`, `RegisterRegistry`,
  `SpaConsoleMcpApplication`, `OpenApiApplication`). It does NOT reach into internals.
- GenroPy itself is a **runtime** requirement (the worker runs a `GnrWsgiSite`), not a Python
  import dependency of this package: the source imports no `gnr.*`.

### Architecture (rebased on the new core, 2026-08 — `bridge-rebase-new-core`)

- **Front** (`GenropySpaApplication` on the core's `SpaApplicationNew`): ROOT mount (`""`),
  serves `/metrics` natively and forwards site paths to the pool. The pool is born at
  startup from OUR recipe (`spa/config.py`): `orchestration().commander(...)` + ONE
  `group(worker_class=GenropyWorker, ...)` — genro-asgi 0.36 put the pool under the
  application's `orchestration` node, so the path is
  `applications.site.orchestration.commander`. The debug door mounts the core console on
  `_console` when `GNR_ASGI_CONSOLE` is set (mounting IS the gate).
- **Commander** (`GenropySpaCommander` on the core's `SpaCommander`, `spa/genropy_spa_commander.py`,
  #8 / genro-asgi #59): the vertex with the site's desk. `DeliveryDesk` (`spa/delivery_desk.py`,
  with `SubscriptionIndex`) is the `delivery` branch of the commander's dispatcher —
  `/commander/delivery/{subscribe_table,exchange,deposit,on_datachange}`; `GenropyCommanderEnvelopeHandler`
  is the envelope layer whose `on_new_page` hands the announced `table_subscriptions` to
  `record_page_table_subscriptions`; `on_worker_presented` and `broadcast_subscribed_tables` push the
  source filter down as the order `/commander/delivery/subscribed_tables`; the drop verbs reach the desk.
  `GenropySpaApplication.commander_class` names it.
- **Worker** (`GenropyWorker` on the core's `SpaWorker`): a child process hosting the
  `GnrWsgiSite`; registers user/connection/page items, runs the site in a thread. Carries the
  site's data-plane verbs (`setStoreSubscription`, `subscribeTable`, `notifyDbEvents`,
  `set_datachange`, `reset_datachanges`, `drop_datachanges`, `collect_page`), the request slot
  with the deposits (`GenropyRequestSlot`), the `delivery` orders (`DeliveryOrders`) and the
  `subscribed_tables` filter. `GenropyRegistry` / `GenropyPageRow` (`spa/genropy_register.py`)
  are its registry and its page row: legacy Bag stores, the row queue, the three subscription
  sets, the `user_view`.
- **Register client** (`GenropyRegisterClient`, `siteregister/`): the in-process fake of
  the `gnr.web.daemon` client — the surface the site calls. Reads are worker-local;
  cross-worker delivery rides the end-of-request exchange with the commander's desk.
  The daemon override engages ONLY with `GNR_DAEMON_PROVIDER=genropy-asgi` (genropy #1070).
- **Proxy** (`GenropyProxyOpenApiApplication`, `proxy/`): a separate, smaller thing — the
  legacy db behind an OpenAPI application; unrelated to the SPA pool.

### Cemented decisions — DO NOT reopen

- **One identity** (owner, 2026-08-22): the routing cookie is `spa_connection_id` and
  carries the connection id the SITE itself creates (24h, `HttpOnly; SameSite=Lax`).
  The front mints nothing; it writes the cookie only when the reply names a different id.
  Dead ancestors: `gnr_cid`, `sticky_cid`, the GenroPy session-cookie decoder.
- **Site-facing semantics imitate pre_refactoring in full** (owner, 2026-08-19). The one
  licensed divergence is the worker->commander dialogue. The bridge changes BASE, not
  semantics.
- **No worker-count selector** (Phase 2, 2026-08-21): the pool always runs and sizes
  itself (`worker_max_number`); `workers=`, `--workers`, `GNR_ASGI_WORKERS` are dead.
- **Population maps live on the commander** (`user_map`, `connection_user_map`,
  `page_connection_map`); the bridge's register reads are worker-local — the site-wide
  read is core work, tracked as S1 in `temp/problemi_genro_asgi_dal_ponte_2026-08-22.md`.
- **`drop_page` keeps `cascade=` on the bridge** and composes the no-climb drop from the
  registry pieces (D7, 2026-08-20); the core keeps the `*_register` names, the bridge
  exposes `user_items`/`connection_items`/`page_items` as translating properties (§7a).

### Project-Specific Guidelines

- Tests use the **public API only** — never wire internal registry state by hand; build state
  by making lifecycle events happen (via `apply_lifecycle`).
- Docstrings declare what is PROVISIONAL and what is FIXED.
- Verify live state (git/tests/PyPI) before asserting — never trust cache.

### Related Documentation

- Current state and open defects: `temp/problemi_ponte_2026-08-22.md` (ours) and
  `temp/problemi_genro_asgi_dal_ponte_2026-08-22.md` (what the core owes the bridge).
- The data plane (datachanges, table subscriptions, dbevents): `docs/internal/datachanges.md`,
  `docs/internal/dbevents.md`; the move from the core: `temp/piano_59_bridge_2026-09-04.md`,
  `temp/note_per_genro_asgi_59.md`.
- Workflow record: `.phased/active/bridge-rebase-new-core/` (plan + notes).
- Historical vision (pre-rebase, superseded): `genro-asgi/temp/architettura_daemonless.html`.

---

**All general policies are inherited from the parent document: [meta-genro-modules CLAUDE.md](https://github.com/softwellsrl/meta-genro-modules/blob/main/CLAUDE.md)**
