# Context: wf/macro1-legacy-data-collection
Parent: main
Mode: interactive
Must not break: both recorders install on the bridge too, where there is no gunicorn hook — installation is a call, not a hook (Macro 2)
Must not break: every HTTP record carries the RPC method and the payload — Macro 2 pairs exchanges by RPC method plus payload shape
Must not break: every HTTP record carries its duration and the `X-Gnr*` breakdown, or Macro 3 has to re-instrument from scratch
Must not break: the written macro is re-runnable identically on the bridge, or the two traces are not comparable (Macro 2)

## Objective
From the classic GenroPy stack, make it possible to take a single HTTP request
and read which site-register calls it caused, in which order, with which
answers. Two recorders write two JSONL traces linked by one column, the
`exchange_id`. This is macro-phase 1 of `.phased/roadmap.md`: fidelity work,
timings are not read, so the instrumentation may be as heavy as it needs.

## Work Plan
- [>] **Phase 1**: the classic stack up and serving
  > In execution since 2026-08-23T06:52:29Z
  - Run: opus / low
  - Pattern: `benchmarks/gunicorn_count.conf.py` (the launch recipe)
  - Files: `benchmarks/compare/README.md`, `temp/legacy_venv/` (not committed),
    the twin instance under
    `~/Sviluppo/Genropy/genropy/projects/test_invoice/instances/test_invoice_pg_legacy/`
  - Decisions: the venv lives in `temp/` (gitignored, never committed); the twin
    instance points at the SAME db as `test_invoice_pg`; no gunicorn config file
    in this phase — it arrives in Phase 2, when there is something to install.
  - Details:
    1. Clean venv: `uv venv temp/legacy_venv --python 3.12`, then
       `uv pip install --python temp/legacy_venv/bin/python ~/Sviluppo/Genropy/genropy/gnrpy`.
       NOT editable: an editable install points at the genropy working tree and
       forbids isolated trials. genropy-asgi must NOT enter this venv. Check
       gunicorn is present (`gnr web serveprod` needs it) and install it if not.
    2. Twin instance: copy `test_invoice_pg/` to `test_invoice_pg_legacy/` under
       the same `test_invoice` project. The db line stays identical
       (`dbname="test_invoice_pg"`, postgres localhost:5432): same database,
       different site name, so the two stacks never collide on site folder or
       cookie. The resolver finds it as `instances/test_invoice_pg_legacy/site`.
    3. Sitedaemon in foreground: `gnrdaemon test_invoice_pg_legacy`. With a
       sitename that command runs the site register server directly and stays in
       the foreground; without one it starts the multi-site daemon, which spawns
       its children with multiprocessing and dies on macOS. It writes
       `sitedaemon.xml` in the site folder — that is where the client reads its
       address. hmac key and port 40004 already come from
       `~/.gnr/environment.xml`; the twin instance needs no extra config.
    4. Gunicorn: `PGGSSENCMODE=disable gnr web serveprod test_invoice_pg_legacy
       -b 127.0.0.1:8099 -w 1 -k gthread --threads 16`. One process, 16 threads.
       The variable is mandatory on macOS: libpq negotiating Kerberos in a forked
       child segfaults the worker on the first request.
    5. Hygiene before every start: no stale process on 8098, 8099, 40004. An old
       server left standing falsifies everything downstream.
    6. The recipe stays written in `benchmarks/compare/README.md`: the commands
       above, the declared run conditions (stack, debug yes or no, one process
       and 16 threads, which db) and the accounts
       (`benchmarks/usernames.txt`, password `a`).
  - Done: the site answers on `http://127.0.0.1:8099` and login with a user from
    `benchmarks/usernames.txt` succeeds
  - Verify: now — open the browser, log in, the application page appears

- [ ] **Phase 2**: the HTTP recorder
  - Run: opus / medium
  - Pattern: `benchmarks/capture_proxy.py` (ancestor of the record format),
    `benchmarks/gunicorn_count.conf.py` (the `post_worker_init` install point)
  - Files: `benchmarks/compare/http_recorder.py`,
    `benchmarks/compare/gunicorn_recorders.conf.py`,
    `benchmarks/compare/README.md`
  - Decisions: names are ours to pick (bench scaffolding, not package surface) —
    `http_recorder`, trace `http_trace.jsonl`, linking field `exchange_id`. NO
    schema version field: the traces are consumed immediately, and a format
    change means re-running the collection (owner, 2026-08-23 — this supersedes
    the roadmap's former "versioned from the start" line). Installation is a
    plain call the gunicorn config invokes, never logic living in the hook: the
    bridge has no gunicorn.
  - Details: a WSGI middleware wrapping the app, installed from
    `post_worker_init`. It mints the `exchange_id` and deposits it in a
    thread-local — that is the seam Phase 3 reads, and it is a contract, not an
    internal detail. One line per exchange: method, path, query, request headers,
    request body, status, response headers, response body, the `X-Gnr*` headers,
    thread id, timestamp, duration, and the RPC method plus form payload parsed
    the way `capture_proxy.py` already does. Whole bodies, no truncation beyond
    what keeps a single line sane. A failure inside the recorder is recorded and
    never propagates to the response. Traces are written under `temp/`.
  - Done: a hand-driven session produces `http_trace.jsonl` where every exchange
    carries whole request and response bodies and a distinct `exchange_id`; an
    error forced inside the recorder leaves the response intact
  - Verify: now — in the trace of the login you can see the two places the
    identity travels (flat fields on `login_checkAvatar`, the XML Bag in the
    `login` field on `login_doLogin`)

- [ ] **Phase 3**: the register interceptor and the first macro
  - Run: opus / high
  - Pattern: `benchmarks/sr_counter.py` (valid as design only — its code is
    expired: it patches a module that no longer exists)
  - Files: `benchmarks/compare/register_recorder.py`,
    `benchmarks/compare/gunicorn_recorders.conf.py`,
    `benchmarks/compare/macros/`, `benchmarks/compare/README.md`
  - Decisions: the patch point is the name `SiteRegisterClient` in the
    `gnr.web.gnrwsgisite` namespace (imported at line 45, instantiated by the
    `register` property at line 178) — genropy itself is never modified. The
    wrapper goes through the class's single funnel, `__getattr__`
    (`gnr/web/daemon/siteregister_client.py:326`). Browser sequences are called
    *macros*; the roadmap's stages are *macro-phases* (owner, 2026-08-23).
  - Details: the wrapper builds the real client and records every call: verb,
    arguments, answer, number of attempts, error class, ordinal within the
    exchange, `exchange_id` read from the thread-local, thread id. The legacy
    funnel retries up to `MAX_RETRY_ATTEMPTS` and then returns `None` without
    re-raising, so attempts and error class must be recorded or a failing
    register becomes invisible. Bags serialise as truncated `repr`, never pickle.
    Also born here: `benchmarks/compare/macros/` holding ONE minimal macro
    written down — login, one navigation, one save — with its run conditions
    declared. More macros will follow in Macro 2; the folder exists so they have
    a home.
  - Done: with the minimal macro executed, every line of `register_trace.jsonl`
    carries an `exchange_id` that exists in `http_trace.jsonl`, and one chosen
    RPC exchange shows in order the register calls it made
  - Verify: now — take one RPC call and read what it did to the register: it
    makes sense

## Notes
- genropy is never modified: everything lives in the venv and in
  `benchmarks/compare/`. The genropy working copy is on `develop` at `9e39fe9c1`
  and stays untouched (owner's rule: changes only through an approved PR).
- The bridge is touched only from the mother session: if the bench reveals a
  bridge defect, report it, do not fix it here.
- Debug on or off is a *declared condition* of the run, not a dilemma: macro-phases
  1 and 2 do not read timings. The SQL counters only increment when the site runs
  in debug, so a debug run populates the `X-Gnr*` SQL fields and a non-debug one
  does not — whichever is used, the README says which.
- The 16 threads in one process interleave the calls: every line carries thread id
  and `exchange_id`, or the trace is unreadable. That is why the two recorders are
  designed together even though they land in two phases.
- Source of truth for the whole bench: `temp/startdoc_test_parallelo_2026-08-22.md`
  (verified facts, do not redo the investigation) and `.phased/roadmap.md`.
