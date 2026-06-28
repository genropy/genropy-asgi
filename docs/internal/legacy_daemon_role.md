# How legacy GenroPy uses the daemon — verified facts

**Version**: 0.1.0
**Status**: 🔴 DA REVISIONARE
**Last Updated**: 2026-06-22

> Internal working doc. Collects ONLY what we verified — by measurement or by
> reading the source (file:line cited) — about how a legacy GenroPy site uses
> the site register / daemon. The goal is a stable factual base before
> redesigning the ASGI front for legacy sites. Assumptions and open questions
> are kept in a separate section and must not be read as facts. Language:
> English (the analysis docs in this folder are mixed; facts here in English).

---

## Scope and goal

End goal: an **ASGI application that serves a legacy GenroPy site**. This
document fixes what the legacy actually does with the daemon, so design choices
rest on facts, not on experimental code we wrote (see "Not a constraint").

---

## 1. What the site register holds (and where it runs)

The **site register** holds the per-site shared state: connections, pages,
users, and the per-page **serverstore** (a live Bag). In daemon mode it lives in
a separate process (`:40405`, asyncio + uvloop + msgpack); in in-process mode it
lives inside the worker.

- The register client is chosen per-process at first use:
  `make_site_register_client(site)` returns `InProcessSiteRegisterClient` when
  siteconfig has `<gnrdaemon mode="inprocess"/>`, else `SiteRegisterClient`
  (daemon TCP). — `siteregister_client.py:528`, read lazily in
  `gnrwsgisite.py:179` (`register` property).
- Verified by measurement: in daemon mode, worker-side probes on the register
  read **0** — the calls execute inside the daemon process. The only place that
  sees all register traffic is the daemon's server-side dispatcher.

## 2. The register is consulted on every rpc — ~56 calls per HTTP request

Measured at the daemon dispatcher during one A1 round (1 user, 35 HTTP
requests, instrumented daemon): **1960 register calls / 35 requests ≈ 56 per
request**. Breakdown: ~83% are Bag/item reads (`remotebag_getItem` 819,
`get_item` 811); `subscription_storechanges` fires ~once per pageCall
regardless of what the call does. The register is touched on *every* rpc, even a
trivial one.

## 3. One register call costs 72× more via daemon than in-process

Measured in isolation (5000 iterations of `get_item`): in-process **0.60 µs**
(direct RAM call under the per-site lock), daemon **43.20 µs** (msgpack +
loopback TCP). Of the 43 µs, ~12.6 µs is GIL-held CPU (serialization), ~22 µs is
socket wait.

## 4. The connection_id is stable per-session ONLY because the register is shared

This is the core service the daemon provides. Verified in source:

- The connection_id is minted once with `getUuid()` at first contact and then
  travels in the signed cookie on every request. — `connection.py:65` (`create`),
  `connection.py:105` (`write_cookie`).
- On each request, GenroPy **validates** the cookie's connection_id by looking
  it up in the register: `connection_item = self.page.site.register.connection(connection_id)`.
  It is reused only if found AND the user matches; otherwise `connection_id`
  stays `None`. — `connection.py:75-86` (`validate_connection`).
- If after validation connection_id is `None`, GenroPy mints a **new** one. —
  `gnrwebpage.py:315-318` (`_register_new_page` → `connection.create()`).
- `register.connection(id)` is just a register item fetch. —
  `siteregister.py:641` (daemon) / in-process override.

**Consequence (verified):** with a **shared** register (daemon), any worker
finds the connection_id → it is stable across interchangeable workers. With a
**per-process** in-process register, a request that lands on a worker whose
register does not hold that connection_id is treated as a new connection → a new
id is minted and the cookie reissued. So in-process requires that a user always
returns to the **same** worker.

## 5. "The connection is not longer valid"

Raised when a request carries a `page_id` but `connection.connection_id` is
`None` (the register did not recognise the connection). —
`gnrwebpage.py:298-300` (`_check_page_id`). This is the observable symptom of a
request reaching a worker that does not hold the connection (the in-process
no-sticky failure mode).

## 6. At 1 worker the daemon is not the throughput bottleneck

Measured (loadRecordCluster, light SQL, ramp 1→32 users, in-process vs daemon):
the two modes are within noise of each other; per-call the daemon is 72× slower
but that cost is masked inside a request dominated by SQL + Bag rendering + the
single GIL. The daemon-less advantage at 1 worker is real per-call but hidden in
the aggregate; it is meant to show up across **N workers** (the shared daemon is
the single point all workers converge on — measured cap ~134-166 req/s at N
workers, not N×).

Supporting decomposition of one light request (~21 ms): SQL ~5 ms, register ~6 ms
(56 × ~0.11 ms), Bag build + serialize + WSGI overhead ~10 ms. On a heavy
getSelection (14 relation columns) SQL alone is ~69 ms (JOINs resolved in SQL,
verified via Postgres `log_min_duration_statement`), which masks everything.

## 7. Locks (per the in-process register) — measured, not a problem so far

- The per-site `critical_section` (one RLock) is taken on every `_sr_call`. Under
  load (1 worker, 8 gthread threads) it is **held ~8% of wall time, waited ~0%**
  (instrumented). It did not serialise threads meaningfully at this scale.
- A per-page `lock_item` serialises only threads on the **same** page
  (`ServerStore.__enter__`, `siteregister_client.py:40-63`). Distinct pages do
  not contend.
- Caveat: measured only up to 8 threads on one worker, never under a real
  multi-worker sticky setup.

---

## Direction (decided with the user) — not yet a verified fact

- The legacy WebSocket was experimental and will be **replaced** by a new
  WebSocket designed in the ASGI app. It does not constrain the design.
- The new WebSocket is intended to be **pervasive and to talk to the process
  that holds the data**. Since that state lives in the worker, this points to
  the **in-process + sticky** model (state local to an addressable worker, HTTP
  and WebSocket of a user reaching that worker), not the shared-daemon model.
- The sticky proxy code on branch `feature/poc-sticky-workers` is our own
  experimental, poorly-written code — to be redesigned from scratch with the
  clearer model, possibly discarded. See "Not a constraint".

### Worker = uvicorn single-worker spawned by us (no gunicorn)

Chosen transport/process model: an **own spawner** launches and supervises **N
single-worker uvicorn processes** (1 process = 1 GIL = 1 core), each serving the
legacy site as an ASGI app; the front does **sticky-per-user** routing across
them. No gunicorn. Rationale, from verified facts:

- **The ASGI→WSGI bridge already exists.** `GenropyProxy.handle_request` runs the
  WSGI `GnrWsgiSite` in a thread via `await smartasync(_run_wsgi)(...)` —
  `genropy_proxy.py:86-88`, `_run_wsgi` at `genropy_proxy.py:213`. `smartasync`,
  called from an async context on a sync callable, dispatches to
  `asyncio.to_thread` (smartasync `core.py`, case `(async, sync)`). So the model
  "N uvicorn with smartasync→thread for the sync GenroPy code" is already in use.
- **uvicorn worker is ASGI-native → the pervasive browser WebSocket reaches the
  data-holding process directly** (genro-asgi has native WebSocket,
  `websocket.py`). The WSGI workers (gunicorn/`gnrwsgiserve`) cannot receive
  WebSocket — this is the decisive reason to prefer uvicorn workers.
- **gunicorn offers nothing required by this model** (verified): its worker
  load-balancing is blind (shared socket / round-robin) and unusable for
  sticky; GenroPy defines **no gunicorn hooks** (no `post_fork`/`worker_exit`/…,
  `gnrserveprod.py`), so there is no per-worker init/cleanup to replicate. The
  only real services — respawn and graceful shutdown — the spawner covers
  (respawn; `worker_orchestrator` already sketches it) and uvicorn covers by
  itself (lifespan startup/shutdown).
- **Trade-off**: today gunicorn builds the site once in the master and forks it
  (copy-on-write). N independent uvicorn processes each run a full
  `GnrWsgiSite.__init__` (`gnrwsgisite.py:409+`) — N parallel inits at startup
  (more memory, slower boot), but a clean per-process isolation that is exactly
  what the in-process register needs (no state shared via fork).
- **Performance is unchanged vs gunicorn-WSGI**: threads inside one uvicorn share
  one GIL; scaling comes from the N processes, the threads only cover SQL I/O
  (which releases the GIL). This is the `-w 1 --threads 8` profile already
  measured. uvicorn is chosen for the native WebSocket, not for speed.

### Plan (agreed with the user) — two steps

1. **Sticky-per-user on top of the existing (new) daemon, nothing removed.**
   Keep everything working as today (register in the daemon) but replace
   gunicorn's blind multi-process with our spawner doing sticky-per-user. With
   the shared daemon still active, sticky is an *optimisation*, not a
   prerequisite: a routing miss does not break (the shared register covers), so
   the spawner/sticky can be made solid without solving identity at the same
   time. Welcome worker for guests (reserves capacity); on login the connection
   is dispatched to the user's worker (sticky-per-user: all of a user's
   connections go to the same process).
2. **Remove the daemon by functionality, one at a time.** Once sticky is solid,
   move each register feature (locks, serverstore, datachanges, identity) into
   the worker process incrementally, verifying at each step. Sticky is the
   prerequisite that makes the gradual removal possible.

## Open questions (to verify before committing to the design)

1. **Identity coined upstream?** Can the ASGI front impose the connection_id /
   page_id on the worker (so affinity is fixed before the worker mints state),
   instead of the worker generating it? `validate_connection` accepts a passed
   connection_id — needs checking whether it can be driven from outside.
2. **Sticky bootstrap.** The connection_id is a usable affinity key from the
   2nd request on (it is in the cookie); the only real problem is anchoring the
   FIRST request (no cookie yet) to the worker that will mint the id, so HTTP and
   WebSocket of the same user co-locate.
3. **Reactive state.** How much of the reactive model (datachanges /
   subscriptions / push to the browser) must the new WebSocket carry vs. how much
   can live in the ASGI layer — this sizes how central the register stays.
4. **Locks under real load.** Do the per-site and per-page locks hold under a
   real multi-worker sticky load (beyond the 8-thread single-worker test)?

## Not a constraint (our experimental code — do not let it drive decisions)

- The sticky proxy and its CLI on `feature/poc-sticky-workers`
  (`GenropyStickyProxy`, `sticky_proxy.py`, `sticky_cli.py`, `sticky_config.py`)
  and the affinity-key logic (`cid:` / `tcp:`) we debugged this session.
- The legacy experimental WebSocket.

## The two candidate paths (summary)

1. **ASGI → multi-process gunicorn + (new) daemon.** Workers interchangeable
   because identity lives in the shared daemon. Practically ready. Cost: ~56
   round-trips/request to the daemon; the daemon is the single convergence point
   (bottleneck at many workers). A pervasive WebSocket talking to the data would
   pay a daemon round-trip per message.
2. **Pool of single-process gunicorn workers + sticky, in-process register.**
   State local to the worker → a pervasive WebSocket attached to that worker
   reads/writes state in RAM, zero round-trips. Removes daemon and round-trips;
   locks remain (measured non-problematic so far). Requires a working sticky
   (a real prerequisite, not an optimisation) and carries higher compatibility
   risk.

---

## Method notes (numbers that lie — do not trust without checking at the source)

- `X-GnrSqlCount` is always 0 in nodebug — use Postgres `pg_stat_database` /
  `log_min_duration_statement` for real SQL cost.
- In daemon mode the register runs in another process — worker-side probes read
  0; measure at the daemon dispatcher.
- A `200 OK` body can be `<error>...</error>` — check the body content, not size.
- Single-worker aggregate throughput masks the register (SQL/GIL dominate) — use
  isolated micro-benchmarks for per-call cost.
