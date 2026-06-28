# Claude Code Instructions - genropy-asgi

**Parent Document**: This project follows all policies from the central [meta-genro-modules CLAUDE.md](https://github.com/softwellsrl/meta-genro-modules/blob/main/CLAUDE.md)

Read the parent document first for: language policy (English only), git commit authorship
rules (no Claude co-author), development status lifecycle, standardization requirements,
coding style, mypy policy (advisory, never blocking), and all general policies.

## Project-Specific Context

### Current Status
- **Development Status**: Alpha (`Development Status :: 3 - Alpha`)
- **Version**: 0.1.0
- **Python**: >= 3.11
- **Build**: hatchling, src/ layout, `py.typed`
- **Has Implementation Code**: Yes

### Project Purpose

**genropy-asgi** is the daemon-less **commander/worker** model that serves GenroPy legacy
(synchronous) sites on top of [genro-asgi](https://github.com/genropy/genro-asgi). It was
extracted from `genro-asgi/contrib/genropy_asgi/` to live as its own package, with its own
test suite — so changes here no longer drag the genro-asgi framework test suite.

- **Naming**: package `genropy-asgi` (PyPI, hyphen) · import `genropy_asgi` (underscore).
- Depends on `genro-asgi` as a library (uses its **public API only**: `AsgiServer`,
  `GenroAsgiWorker`, `AsgiApplication`, `AsgiConfigBuilder`, `AsgiDbHandlerBase`,
  `get_current_request`, `HTTPServiceUnavailable`). It does NOT reach into genro-asgi internals.
- GenroPy itself is a **runtime** requirement (the proxy runs a `GnrWsgiSite`), not a Python
  import dependency of this package: the source imports no `gnr.*`.

### Architecture (daemon-less commander/worker)

- **Commander** (`WorkerCommanderApplication`): an app mounted on the MAIN `AsgiServer`. It
  spawns/supervises worker processes via an orchestrator, forwards every request to the
  right worker (reverse-proxy), and holds the affinity registries.
- **Worker** (`GenroAsgiWorker` from genro-asgi + `GenropyProxy`): a minimal single-app ASGI
  server hosting `GenropyProxy`, which runs the GnrWsgiSite in a thread via the executor.
- **Monitor** (`MonitorApp`, optional): a live `/_monitor` dashboard (genro-ws-web).

### Cemented decisions — DO NOT reopen

- Routing reads **our** opaque cookie `gnr_cid` (cleartext, `HttpOnly; SameSite=Lax`),
  minted by the commander on `new_connection`. The GenroPy session-cookie decoder is **dead**.
- Registries: `cid_to_user` + `user_registry` (`user -> {connections, worker}`). There is no
  separate `user_to_worker` map — the worker lives inside the user entry.
- One worker group `pool`; the **first** worker also serves guests, with a lower cap
  (`max_users_first` < `max_users_other`). Round-robin is banned; no separate welcome group.
- User migration is a handshake `move_user` -> `pop_user` (source) + `add_user` (destination);
  payload is an **opaque pickled blob** (the commander is a blind courier), binary transport.
- `decide_worker` chooses by **capacity** (user count, PROVISIONAL — will become real load).

### Project-Specific Guidelines

- Tests use the **public API only** — never wire internal registry state by hand; build state
  by making lifecycle events happen (via `apply_lifecycle`).
- Docstrings declare what is PROVISIONAL and what is FIXED.
- Verify live state (git/tests/PyPI) before asserting — never trust cache.

### Related Documentation

- Vision document (target architecture): `genro-asgi/temp/architettura_daemonless.html`.
- The split to 3 repos (framework / orchestration / GenroPy runner) is a future move; this
  package is the first cut (asgi vs everything-else).

---

**All general policies are inherited from the parent document: [meta-genro-modules CLAUDE.md](https://github.com/softwellsrl/meta-genro-modules/blob/main/CLAUDE.md)**
