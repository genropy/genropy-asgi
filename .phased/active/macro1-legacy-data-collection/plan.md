# Context: wf/macro1-legacy-data-collection
Parent: main
Mode: interactive
Must not break: both recorders install on the bridge too, where there is no gunicorn hook — installation is a call, not a hook (Macro 2)
Must not break: every HTTP record carries the RPC method and the payload — Macro 2 pairs exchanges by RPC method plus payload shape
Must not break: every HTTP record carries its duration and the `X-Gnr*` breakdown, or Macro 3 has to re-instrument from scratch
Must not break: the reference session is reproducible ON DEMAND from the recipe in `benchmarks/compare/README.md` — the traces are never committed (whole bodies, login, cookies, public repository), so macro-phase 2 reads them only from the out-of-tree archive Phase 4 builds
Must not break: the RECORD SHAPE is identical on both stacks even though the mechanisms differ — wrapper object on legacy, mixin on the bridge; the comparison reads lines, never the way they were obtained

## Objective
Make it possible to take a single HTTP request and read which site-register calls
it caused, in which order, with which answers — on the classic GenroPy stack
first (Phases 1-3, done) and then on the genropy-asgi bridge (Phase 5), with both
reference sessions kept in a durable archive (Phase 4). Two recorders write two
traces linked by one column, the `exchange_id`, written straight into a per-run
SQLite file. This is macro-phase 1 of
`.phased/roadmap.md`: fidelity work, timings are not read, so the instrumentation
may be as heavy as it needs.

The bridge collection was moved here from macro-phase 2 (owner, 2026-08-23) for a
reason that outlives the convenience: the replica cannot be designed before the
bridge's own trace exists, because only that trace shows how far the identifiers
actually diverge and what "adapting them" means. The replica and the convergence
loop stay in macro-phase 2.

## Work Plan
- [x] **Phase 1**: the classic stack up and serving
  > Done: the classic stack serves the twin instance `test_invoice_pg_legacy` on
    `http://127.0.0.1:8099` — clean venv `temp/legacy_venv` (genropy 26.08.19.1
    with the `pgsql` extra, gunicorn 26.1.0, no genro_asgi), twin instance
    carrying configuration only against the same db `test_invoice_pg`, standalone
    sitedaemon on 40004, gunicorn at one process and 16 threads. The bring-up
    recipe, the declared run conditions and the login trap are written down in
    `benchmarks/compare/README.md`, which also makes `benchmarks/compare/` the
    home the two recorders need in Phase 2 and Phase 3.
  > Files: benchmarks/compare/README.md,
    .phased/active/macro1-legacy-data-collection/plan.md,
    .phased/active/macro1-legacy-data-collection/notes.md.
    Outside git: temp/legacy_venv/ (gitignored) and the twin instance
    ~/Sviluppo/Genropy/genropy/projects/test_invoice/instances/test_invoice_pg_legacy/
    (untracked in the genropy working copy, never committed there)
  > Verified: site answers 200 on http://127.0.0.1:8099; login replayed for
    `alexander.king` (benchmarks/usernames.txt, password `a`) returns the real
    avatar from the db on `login_checkAvatar` and a clean `login_doLogin`, no
    `<error>` in either body
  > Verify: now — open http://127.0.0.1:8099 in the browser, log in with a user
    from `benchmarks/usernames.txt` (password `a`): the application page appears.
    Login-gated, so the login is the human's by rule — an agent-driven browser
    pass would not remove the human from this check. PERFORMED by the owner,
    2026-08-23, confirmed.
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
       `uv pip install --python temp/legacy_venv/bin/python "$HOME/Sviluppo/Genropy/genropy/gnrpy[pgsql]"`.
       The `pgsql` extra is REQUIRED, not optional hygiene: the Postgres driver is
       an optional dependency of genropy, so a plain install cannot reach the
       database and this phase's `Done:` — a successful login — is unreachable.
       NOT editable: an editable install points at the genropy working tree and
       forbids isolated trials. genropy-asgi must NOT enter this venv. `gunicorn`
       is a base dependency and needs no check.
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
       address. It always binds 40004 and no site configuration can move it: the
       CLI passes no port and the chain ends at `PYRO_PORT`, so two standalone
       sitedaemons cannot run side by side. The 40404 the site config reports
       under `gnrdaemon` is the multi-site daemon's address and is never used
       here. The twin instance needs no extra config.
    4. Gunicorn: `PGGSSENCMODE=disable gnr web serveprod test_invoice_pg_legacy
       -b 127.0.0.1:8099 -w 1 -k gthread --threads 16`. One process, 16 threads.
       The variable is mandatory on macOS: libpq negotiating Kerberos in a forked
       child segfaults the worker on the first request.
    5. Hygiene before every start: no stale process on 8098, 8099, 40004. An old
       server left standing falsifies everything downstream.
    6. The recipe stays written in `benchmarks/compare/README.md`: the commands
       above, the declared run conditions (stack, debug off in the standard run,
       one process and 16 threads, which db) and the accounts
       (`benchmarks/usernames.txt`, password `a`). From Phase 3 the README carries
       TWO launch commands: the plain `gnr web serveprod` above, which stays the
       declared condition of a run with NO recorders, and the launcher
       `benchmarks/compare/serve_legacy.py` for a recorded run — a gunicorn hook
       cannot install the register recorder, because the site builds its register
       client in the master before the config file is read. This step is extended,
       not falsified.
  - Done: the site answers on `http://127.0.0.1:8099` and login with a user from
    `benchmarks/usernames.txt` succeeds
  - Verify: now — open the browser, log in, the application page appears

- [x] **Phase 2**: the HTTP recorder
  > Done: `HttpRecorder`, a WSGI middleware wrapping the site application,
    installed by one call from `post_worker_init`. It mints the `exchange_id`,
    injects it as the `X-Bench-Exchange-Id` request header — the seam Phase 3
    reads back through `site.currentRequest.headers` — and appends one JSONL
    line per recorded exchange to `temp/http_trace.jsonl`: method, path, query,
    whole request and response bodies, headers, the `X-Gnr*` breakdown, RPC
    method and form payload, thread id, timestamp, duration. Statics, favicon
    and the pings that rendered nothing produce no line at all, so nothing that
    IS recorded is ever truncated. A recorder failure is written as
    `recorder_error` and never reaches the response. Two versioned helpers ship
    with it: `http_recorder_check.py` (19 isolation checks, no server needed)
    and `drive_login.py` (replays a login over HTTP, no browser).
  > Files: benchmarks/compare/http_recorder.py,
    benchmarks/compare/gunicorn_recorders.conf.py,
    benchmarks/compare/http_recorder_check.py,
    benchmarks/compare/drive_login.py,
    benchmarks/compare/README.md,
    .phased/active/macro1-legacy-data-collection/plan.md,
    .phased/active/macro1-legacy-data-collection/notes.md
  > Verified: `python3 benchmarks/compare/http_recorder_check.py` — 19 checks
    green, covering the filters, the whole bodies, the `X-Gnr*` harvest and both
    failure paths (response intact, failure recorded). `ruff check
    benchmarks/compare/` clean. On the live legacy stack: a login driven over
    HTTP by `drive_login.py` produced a trace whose exchanges all carry distinct
    `exchange_id`s, the header matching the id on every line, four interleaved
    thread ids, zero `recorder_error`.
  > Verify: now — PERFORMED twice, 2026-08-23, confirmed: by the owner in the
    browser and by the session over HTTP. Both show the identity in its two
    places — flat `user=alexander.king` / `password=a` on `login_checkAvatar`,
    and the same values inside the XML Bag in the `login` field on
    `login_doLogin`.
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
    `post_worker_init`. It mints the `exchange_id` and injects it into the
    request as the `X-Bench-Exchange-Id` header — that is the seam Phase 3
    reads, and it is a contract, not an internal detail. (Owner, 2026-08-23:
    supersedes the thread-local this field first named. The site already keeps
    the current request per thread, `GnrWsgiSite.currentRequest`, a
    `ThreadedDict` filled for the whole dispatch — statics and `_ping`
    included, unlike `currentPage` — and the register client holds the site.
    So no global state of ours, the join key is visible in the trace among the
    request headers, and the two recorders share only a header name instead of
    importing each other.) One line per exchange: method, path, query, request headers,
    request body, status, response headers, response body, the `X-Gnr*` headers,
    thread id, timestamp, duration, and the RPC method plus form payload parsed
    the way `capture_proxy.py` already does. Whole bodies, **no truncation
    anywhere** — instead, certain exchanges are not recorded at all (owner,
    2026-08-23: a filter, not a cut): static assets, recognised by the response
    content type plus `favicon.ico`, and pings that rendered nothing — the bare
    envelope `<GenRoBag><result _T="NN"></result></GenRoBag>`, which is what
    `handle_ping` returns when there is nothing to deliver
    (`gnr/web/daemon/siteregister.py:928`); an empty Bag, the first guess, never
    occurs on the wire. A ping carrying a datachange IS recorded — that Bag is
    the register answering, and it is what the replica compares in macro-phase 2. A failure inside the
    recorder is recorded and never propagates to the response. Traces are
    written under `temp/`.
  - Done: a hand-driven session produces `http_trace.jsonl` where every exchange
    carries whole request and response bodies and a distinct `exchange_id`; an
    error forced inside the recorder leaves the response intact
  - Verify: now — in the trace of the login you can see the two places the
    identity travels (flat fields on `login_checkAvatar`, the XML Bag in the
    `login` field on `login_doLogin`)

- [x] **Phase 3**: the register interceptor and the reference session
  > Done: `RegisterRecorder`, a wrapper OBJECT standing in place of
    `SiteRegisterClient`, installed by one assignment from the versioned launcher
    `serve_legacy.py` — no gunicorn hook is early enough, because
    `gnrserveprod.main()` builds the site, and with it the register client, in the
    master process before it reads the `-c` file. It records one JSONL line per
    call the SITE made into `temp/register_trace.jsonl`: the verb, the surface it
    was intercepted on (`client` for a method declared on the legacy class,
    `passthrough` for a name its `__getattr__` forwards, `store` for a call on a
    `ServerStore` it hands back and wraps), arguments and answer written to be
    comparable between runs (Bags as XML, memory addresses stripped), the round
    trips the call cost on the wire, the error class the legacy retry loop
    swallows, the ordinal within its exchange, duration, thread and pid; store
    lines name their register and item. The exchange is read from the
    `X-Bench-Exchange-Id` header Phase 2 injects, and is ABSENT from the record
    for the calls the master makes at boot. The HTTP recorder gained the
    counterpart the join needed: a filtered exchange now leaves an id-only stub
    line — what it was and why it was filtered, never a body — so no register
    line names an exchange the HTTP trace does not contain (owner, amending his
    own filter). The reference session was performed in the browser by the owner
    and is documented in `benchmarks/compare/README.md` as a recipe with its
    recorded evidence; the traces themselves are never committed.
  > Files: benchmarks/compare/register_recorder.py,
    benchmarks/compare/serve_legacy.py,
    benchmarks/compare/register_recorder_check.py,
    benchmarks/compare/http_recorder.py,
    benchmarks/compare/http_recorder_check.py,
    benchmarks/compare/README.md,
    .phased/active/macro1-legacy-data-collection/plan.md,
    .phased/active/macro1-legacy-data-collection/notes.md
  > Verified: `register_recorder_check.py` 33 assertions green on the bench venv
    (the two client surfaces, the store and its lock, genropy's REAL retry loop
    swallowing four round trips, the absent exchange, the comparable values, and
    a recorder fault never reaching the site); `http_recorder_check.py` 22
    assertions green (was 19 — the stub names itself, carries no body, and its
    reason is right for each filtered shape); `ruff check benchmarks/compare/`
    clean. On the live stack, the owner's reference session: 260 HTTP exchanges
    (23 full records, 237 stubs — 224 `static`, 13 `empty_ping`), 1918 register
    calls on 13 threads, ZERO unjoinable lines, 2 calls with the exchange
    explicitly absent (the master's boot), zero `recorder_error`, no memory
    addresses. The record shape was re-checked field by field after the naming
    review and is identical, so that reference is what the current code produces.
  > Verify: now — take one RPC call and read what it did to the register.
    PERFORMED by the owner, 2026-08-23, confirmed on `saveRecordCluster`: the
    identity reads, `get_dbenv`, then `subscribed_tables` →
    `filter_subscribed_tables` → `notifyDbEvents` on `invc.customer`, then the
    page lock around the write — 18 calls on the client, 14 on stores, ordinals
    1 to 32 unbroken.
  - Run: opus / high
  - Pattern: `benchmarks/sr_counter.py` (valid as design only — its code is
    expired: it patches a module that no longer exists)
  - Files: `benchmarks/compare/register_recorder.py`,
    `benchmarks/compare/register_recorder_check.py`,
    `benchmarks/compare/serve_legacy.py`, `benchmarks/compare/README.md`.
    NOT `gunicorn_recorders.conf.py`: the HTTP recorder's install point is closed
    work and this phase does not reopen it.
  - Decisions: the patch point is the name `SiteRegisterClient` in the
    `gnr.web.gnrwsgisite` namespace (imported at line 45, instantiated by the
    `register` property at line 178) — genropy itself is never modified. The
    recorder is a wrapper OBJECT standing in place of the client: it builds the
    real client, holds it, and catches every attribute through its own
    `__getattr__` — explicit methods included. NOT the legacy class's
    `__getattr__`: that funnel is bypassed by about 26 methods declared on
    `SiteRegisterClient` (`new_page`, `new_connection`, `pages`, `connections`,
    `users`, `counters`, `refresh`, `get_item`, `page`, `make_store`, the four
    `*Store` builders, `dump`, `load` and more), and the bridge's own
    `GenropyRegisterClient` has no `__getattr__` at all — a recorder built on the
    funnel would record nothing there, breaking the first `Must not break:` line.
    The wrapper needs no list of which names are explicit. The stores it hands
    back are wrapped too (see Details). There are no *macros*: the concept is a
    **replica** of a session the owner performs (owner, 2026-08-23, retiring the
    word); the roadmap's stages are *macro-phases*.
  - Details: the wrapper builds the real client and records every call: verb,
    arguments, answer, the round trips the call cost on the wire, error class,
    ordinal within the
    exchange, `exchange_id` read with
    `self.site.currentRequest.headers.get('X-Bench-Exchange-Id')` — the seam
    Phase 2 built, superseding the thread-local this field first named (owner,
    2026-08-23) — thread id. The legacy
    funnel retries up to `MAX_RETRY_ATTEMPTS` and then returns `None` without
    re-raising, so the round trips and the error class must be recorded or a
    failing register becomes invisible. The field counts ROUND TRIPS, not
    attempts, and the name says so: a retry shows as more round trips than the
    call's own shape costs, together with an error. Naming it `attempts` fooled
    this plan's own author within an hour of the field existing — a routine read
    costing two round trips read as a retrying register, and the two are the
    reading of `ServerStore.data`, which evaluates its `register_item` property
    twice and pays a round trip each time, on a perfectly healthy register. Bags serialise as truncated `repr`, never pickle.
    The install point is a versioned launcher, `benchmarks/compare/serve_legacy.py`:
    it calls the install and then `gnrserveprod.main()`. A gunicorn hook cannot
    serve here — `main()` builds the site before it reads the `-c` file, and
    `GnrWsgiSite.__init__` forces the register into existence, so the client
    already exists in the master process before any hook runs and before the fork
    (measured: master and worker share one inherited socket to the sitedaemon).
    The launcher keeps installation a plain call, which is what the first
    `Must not break:` line requires. Because the wrapper is born in the master,
    two things follow — the trace writer opens per write or lazily per pid, never
    a handle inherited across the fork, and the register calls the master makes
    before any exchange exists are recorded with the exchange explicitly absent,
    not filtered and never carrying a stale id.
    The stores the client hands back are wrapped as well: `ServerStore.__init__`
    keeps the client it was built from, so an unwrapped store takes its whole
    conversation — `set_datachange`, `subscribe_path`, `reset_datachanges`,
    `drop_datachanges`, the lock taken in `__enter__`/`__exit__` — outside the
    recorder. A store line carries, besides everything a client line carries, the
    `register_name` and `register_item_id` of the store it happened on. Both
    stacks have a `ServerStore` with its own `__getattr__`, so the surface stays
    comparable in macro-phase 2.
    No sequence is written down. The concept is a REPLICA of a session the owner
    performs in the browser (owner, 2026-08-23), and in that shape the recorded
    trace is itself the script the replica reads — a hand-written sequence beside
    it would be a second source of truth for the same session, free to drift.
    What this phase produces is the reference and the recipe to remake it: the two
    traces of one session the owner performs, and a README section stating in
    plain words what that session did and under which declared conditions. The
    traces are NOT committed, so macro-phase 2 never depends on an archived
    reference, only on the ability to produce one on demand. The replica itself
    and the structural comparison belong to macro-phase 2: this phase collects on
    the legacy stack only.
  - Done: with the reference session performed, every line of
    `register_trace.jsonl` carries an `exchange_id` that exists in
    `http_trace.jsonl` — as a full record, or as the id-only stub of an exchange
    the filter kept out, or with the exchange explicitly absent for the calls the
    master makes at startup — and one chosen RPC exchange shows in order the
    register calls it made, calls on the client and calls on a store alike, the
    latter naming their register and item
  - Verify: now — take one RPC call and read what it did to the register: it
    makes sense

- [>] **Phase 4**: the recorders write into a per-run archive, and the legacy reference re-performed into it
  > In execution since 2026-08-23T23:58:00
  > WIP: done: `run_archive.py` with `RunArchive` (one JSON column plus the
    promoted ones), both recorders writing into it instead of JSONL,
    `serve_legacy.py` minting the run and publishing it in `GNR_BENCH_RUN`,
    three checks green (http 24, register 35, archive 17) and ruff clean |
    missing: the README rewritten from JSONL to the archive, and the legacy
    reference session re-performed into a run archive with its census |
    next: rewrite the README sections that describe the traces |
    commit: 67d4ccf
  - Run: opus / medium
  - Pattern: `new-pattern` (nothing comparable in the repo; SQLite from stdlib)
  - Files: `benchmarks/compare/run_archive.py`,
    `benchmarks/compare/run_archive_check.py`,
    `benchmarks/compare/http_recorder.py`,
    `benchmarks/compare/register_recorder.py`,
    `benchmarks/compare/http_recorder_check.py`,
    `benchmarks/compare/register_recorder_check.py`,
    `benchmarks/compare/README.md`
  - Decisions: **the archive IS the recording target** (owner, 2026-08-23,
    reversing the JSONL-plus-loader shape this plan carried for a day — see the
    reasoning below; the earlier decision is superseded, not merely refined). One
    SQLite file per run, OUTSIDE the git tree, on a LOCAL filesystem (WAL does not
    work over network mounts), one connection per process. The table is ONE JSON
    column holding the whole line plus promoted columns, each promoted because it
    has a job: `exchange_id` and the run id to JOIN, the stack to SEPARATE,
    timestamp and thread to ORDER, the line kind and the verb or path and the
    status to FILTER. A promoted column is a COPY of what the JSON holds, never
    the only place a value lives — otherwise the blob stops being the record and
    this is a schema again. A field is promoted only once it is queried often; an
    occasional query reads inside the JSON.
  - Details: three arguments were weighed and the JSONL-then-load shape lost all
    three. A truncated JSONL line IS possible when a process dies mid-write while
    a half-written SQLite row is not, so durability favours the db. The
    fixed-schema objection dissolved with the one-JSON-column design, which takes
    any shape without a migration. Lock contention between the bridge's worker
    processes is real as mechanics but harmless here: WAL serialises writers, and
    macro-phases 1 and 2 do not read timings — macro-phase 3, which does, runs
    with collection switched off. What decided it is the fourth argument, already
    paid for: a separate load step is a step that can be forgotten, and the
    reference session of 2026-08-23 was lost precisely in the window between the
    run and its archiving. Writing into the archive removes the window.
    So both recorders take a writer instead of a file handle. The writer owns the
    run — its id, the declared conditions as data (stack, debug, worker and thread
    counts, db, the bench commit, the genropy and genro-asgi versions) — and opens
    its connection lazily per pid, the same rule the JSONL writer already had for
    the fork. A failure inside the writer is recorded and never reaches the
    request, which is what the two isolation checks assert and must keep
    asserting. The phase closes by re-performing the legacy reference session,
    which now lands in the archive by construction.
  - Done: the legacy reference session is archived — one SQLite file outside the
    tree whose run row carries the declared conditions, where the join written as
    a query returns zero register lines without an HTTP exchange, and where the
    counts match what the session's own census reports; both isolation checks
    still green, including a forced writer failure that leaves the response intact
  - Verify: now — the archive answers a question you would actually ask: pick one
    RPC exchange and read its register conversation out of the SQLite file

- [ ] **Phase 5**: the two recorders on the bridge, and its reference session
  - Run: opus / high
  - Pattern: `benchmarks/compare/register_recorder.py` and
    `benchmarks/compare/http_recorder.py` (the record shape is the contract),
    `src/genropy_asgi/spa/config.py:108` (where the worker class is named)
  - Files: `benchmarks/compare/bridge_recipe.py`,
    `benchmarks/compare/recording_worker.py`,
    `benchmarks/compare/register_recorder_mixin.py`,
    `benchmarks/compare/bridge_coverage_check.py`,
    `benchmarks/compare/README.md`
  - Decisions: the install rides the RECIPE, not a patch (owner's own proposal,
    2026-08-23, verified). The pool names its worker as an import STRING —
    `"genropy_asgi.spa.genropy_worker:GenropyWorker"` in `spa/config.py:108` —
    resolved by the worker process itself when it is spawned. A bench recipe
    naming a recording subclass therefore installs both recorders in every
    worker with no environment variable, no sitecustomize and no seam added to
    genro-asgi. On the register side the mechanism is a MIXIN overriding the
    client's explicit methods and delegating to the parent, which fits the bridge
    the way the wrapper object fitted legacy: there the client had a generic
    funnel, here every method is declared. Neither genro-asgi nor genropy-asgi is
    modified: everything lives in `benchmarks/compare/`.
  - Details: the recording worker installs the HTTP recorder around the WSGI app
    it hosts and the register recorder on its client, before the site is built
    (`genropy_worker.py:302`). The record shape is copied from the legacy
    recorders unchanged — that is the `Must not break:` line, and the comparison
    reads lines, never mechanisms. A mixin over explicit methods is a list of
    names that can silently fall behind, which the legacy wrapper never could
    because it caught everything: hence the coverage check, which compares the
    client's public methods against those the mixin covers and FAILS when they
    diverge, in the spirit of the tripwire that already guards the daemon
    contract. `wire_calls` is honestly 1 on the bridge — the register lives in
    the worker's own process, there is no wire — and that is a real difference
    between the stacks, not an artefact of measurement. If anything here turns
    out to need a change inside genro-asgi, it is reported to the mother session
    and NOT done from this workflow.
  - Done: a reference session performed on the bridge produces its own archive
    file with the same record shape as the legacy one, every register line naming an
    exchange the HTTP trace contains (or the exchange explicitly absent), the
    coverage check green against the current client, and the run archived by
    Phase 4's archiver alongside the legacy one
  - Verify: now — open the two archived runs side by side and read the same RPC
    call on both stacks: the shapes are comparable and the differences are the
    stacks', not the instruments'

## Notes
- **Phases 1-3 are records of what happened and keep their wording**, including
  the JSONL traces under `temp/` they genuinely produced. Phase 4 replaces that
  storage with the per-run SQLite archive and rewrites the code and the README
  together; until it runs, `benchmarks/compare/README.md` correctly documents
  JSONL, because that is what the bench does today. Changing the README before
  the code would make it describe software that does not exist.
- genropy is never modified: everything lives in the venv and in
  `benchmarks/compare/`. The genropy working copy is on `develop` at `9e39fe9c1`
  and stays untouched (owner's rule: changes only through an approved PR).
- The bridge is touched only from the mother session: if the bench reveals a
  bridge defect, report it, do not fix it here.
- Debug is OFF in the standard declared run (owner, 2026-08-23): `--debug` wraps
  the site in werkzeug's debugging middleware, which the bridge has no equivalent
  of, so error responses would diverge because of the instrument rather than the
  stacks. Cost: `X-GnrSqlTime` and `X-GnrSqlCount` arrive as `0`. A debug run is
  the declared variant for when those two must carry real numbers.
- The 16 threads in one process interleave the calls: every line carries thread id
  and `exchange_id`, or the trace is unreadable. That is why the two recorders are
  designed together even though they land in two phases.
- Source of truth for the whole bench: `temp/startdoc_test_parallelo_2026-08-22.md`
  (verified facts, do not redo the investigation) and `.phased/roadmap.md`.
