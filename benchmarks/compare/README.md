# The legacy/bridge comparison bench

Everything needed to bring up the classic (synchronous) GenroPy stack and record
from it. Later the same two recorders go on the genropy-asgi bridge, where a
**replica** reproduces a session the owner performed and the run stops at the
first divergence — not an offline diff of two finished traces (owner, 2026-08-23;
macro-phase 2 in the roadmap).

The programme is in `.phased/roadmap.md`: three macro-phases, the first two
about **fidelity**, the third about performance. Fidelity work does not read
timings, so the instrumentation may be as heavy as it needs to be.

`benchmarks/compare/` is versioned. What it records is not: every run writes
into a SQLite archive of its own, **outside the git tree** — see *The run
archive* below.

## What runs where

| Piece | Where | Why separate |
|---|---|---|
| The venv | `temp/legacy_venv/` | never committed; genropy-asgi must not enter it |
| The instance | `<genropy>/projects/test_invoice/instances/test_invoice_pg_legacy/` | twin of `test_invoice_pg`, same db, different site name |
| The recorders | `benchmarks/compare/` | versioned; genropy itself is never modified |
| The run archives | `~/genro_bench/runs/` (outside the tree) | whole bodies, the login, the cookies — and this repository is public |
| The bridge | the pyenv interpreter, `genropy_asgi` + `genro_asgi` + genropy installed **editable** | it is the software under comparison; macro-phase 2 edits it at every turn of the loop, so it is not frozen |

`<genropy>` is `~/Sviluppo/Genropy/genropy` — the value of `gnrhome` in
`~/.gnr/environment.xml`.

## Bring-up

### 1. The venv

```bash
uv venv temp/legacy_venv --python 3.12
uv pip install --python temp/legacy_venv/bin/python "$HOME/Sviluppo/Genropy/genropy/gnrpy[pgsql]"
```

Not editable: an editable install points at the genropy working tree and
forbids isolated trials. The `[pgsql]` extra is **mandatory** — the Postgres
driver (`psycopg2-binary`, `psycopg`) is an optional dependency of genropy, and
without it the site cannot reach the database. `gunicorn` is a base dependency
and needs no extra step.

The site logs `ERROR: missing dependencies: pdf2image, watchdog, pyotp, qrcode`
at boot. These are optional features the bench does not exercise; the message is
noise, not a failure.

### 2. The twin instance

A copy of `test_invoice_pg` under the same project, carrying **configuration
only** — the runtime state (`site/data/`, `site/_static/`) is left out so the
first run starts clean, and the site recreates what it needs:

```bash
SRC=~/Sviluppo/Genropy/genropy/projects/test_invoice/instances/test_invoice_pg
DST=~/Sviluppo/Genropy/genropy/projects/test_invoice/instances/test_invoice_pg_legacy
mkdir -p "$DST/site"
cp "$SRC/instanceconfig.xml"  "$DST/instanceconfig.xml"
cp "$SRC/root.py"             "$DST/root.py"
cp "$SRC/site/siteconfig.xml" "$DST/site/siteconfig.xml"
```

`instanceconfig.xml` is copied unchanged: the twin points at the **same**
database, `test_invoice_pg` on postgres localhost:5432. Only the site name
differs, which is enough — the two stacks never collide on site folder or on
the session cookie, which is named after the site (`test_invoice_pg_legacy`).

The resolver finds the instance as `instances/test_invoice_pg_legacy/site`; the
old `sites/` shell no longer exists.

### 3. Hygiene, before every run

```bash
lsof -nP -iTCP:8098 -iTCP:8099 -iTCP:40004 -sTCP:LISTEN
```

Must come back empty. An old server left standing falsifies everything
downstream — and on 40004 it silently prevents the sitedaemon from starting.

**Every run starts from an empty register, and that means restarting the
sitedaemon too, not only gunicorn.** The reason is comparability: on the bridge
a restart wipes the registers unless a soft reset says otherwise, so a legacy
run whose register survived from an earlier session does not start from the same
state and the two runs are not comparable.

Restarting the daemon is not enough by itself: it **saves its status on stop**
(`gnr/web/daemon/siteregister.py:1057`) and **restores it on start** whenever the
pickle is there (`:1087`). The file has to go:

```bash
SITE=~/Sviluppo/Genropy/genropy/projects/test_invoice/instances/test_invoice_pg_legacy/site
rm -f "$SITE/siteregister_data.pik" "$SITE/siteregister_data_loaded.pik"
```

Nothing to delete on the recording side: each run mints an archive file of its
own, so a run never lands on top of another one's lines.

So the full clean restart is, in order: stop gunicorn, stop the sitedaemon,
delete the pickle, start the sitedaemon, start gunicorn. Gunicorn
last, and always after the daemon: `SiteRegisterClient` reads the Pyro URIs out
of `sitedaemon.xml` when it is built, so a worker started before a new daemon
holds addresses that no longer answer.

**A surviving register keeps you logged in.** The site cookie carries the user
and the `connection_id`; if the register still knows that connection, the browser
walks straight into the application and the trace contains no login at all. Seen
on 2026-08-23: a session from an earlier run reappeared after a gunicorn-only
restart, because gunicorn holds no session state — the cookie plus the daemon's
register are the whole identity.

### 4. The sitedaemon, in the foreground

```bash
PGGSSENCMODE=disable temp/legacy_venv/bin/gnrdaemon test_invoice_pg_legacy
```

With a sitename that command runs the site register server directly and stays in
the foreground. Without one it starts the multi-site daemon, which spawns its
children with multiprocessing and dies on macOS.

It writes `sitedaemon.xml` in the site folder, and that file is how the site
finds its register: `SiteRegisterClient` reads `register_uri` from it.

**It binds port 40004, and nothing in the site config changes that.** The port
comes from `PYRO_PORT` in `gnr/web/daemon/siteregister.py`, because the CLI
passes no port and the fallback chain ends there. The `gnrdaemon` port the site
config reports (40404) is the address of the *multi-site* daemon and is not used
here. The hmac key does come from `~/.gnr/environment.xml`.

### 5. Gunicorn: one process, 16 threads

```bash
PGGSSENCMODE=disable temp/legacy_venv/bin/gnr web serveprod test_invoice_pg_legacy \
    -b 127.0.0.1:8099 -w 1 -k gthread --threads 16
```

`PGGSSENCMODE=disable` is mandatory on macOS: libpq negotiating Kerberos in a
forked child segfaults the worker on its first request.

The site answers on `http://127.0.0.1:8099`.

## Declared conditions of a run

Every run declares these, and two runs are only comparable under the same
declaration. From Phase 4 the declaration is not only written here: the launcher
reads it where each condition is true and stores it **as data** in the archive's
own `run` row, so a run carries its conditions with it.

| Condition | Standard value |
|---|---|
| Stack | legacy: standalone sitedaemon + gunicorn |
| Processes / threads | 1 process, 16 threads (`-w 1 -k gthread --threads 16`) |
| Recorders | both, installed by `serve_legacy.py` + the `-c` config; a run with neither uses step 5's plain command |
| Debug | **off** |
| Register at start | **empty** — daemon restarted and `siteregister_data.pik` deleted |
| Database | `test_invoice_pg`, postgres localhost:5432 |
| genropy | 26.08.19.1 (working copy on `develop`, untouched) |
| Python / gunicorn | 3.12.12 / 26.1.0 |

The launcher never assumes any of it: the workers, the threads, the bind and the
debug flag come from the command line it was given, the database from the
instance's own `instanceconfig.xml`, the versions from the installed
distributions, and the bench commit from git.

**Why debug is off in the standard run.** `gnr web serveprod --debug` wraps the
site in werkzeug's debugging middleware, which the bridge does not have: on
error responses it would introduce a divergence produced by the instrument
rather than by the two stacks. The cost is that the SQL counters stay at zero —
they only increment when the site runs in debug — so `X-GnrSqlTime` and
`X-GnrSqlCount` arrive as `0` (measured, not empty: the headers are present and
carry a zero). The recorders collect the `X-Gnr*` headers either way; a debug
run is the variant to declare when those two fields have to carry real numbers.

`--debug` does **not** reduce concurrency: it forces `workers=1` (already the
case) and its `threads` override never lands, so 16 threads survive.

## Accounts

`benchmarks/usernames.txt` — 32 accounts, password `a` for all of them.

**The login trap** (a whole session was lost to it once): the identity travels
in TWO places, and rewriting only the first logs every session in as the
captured user.

- `login_checkAvatar` carries flat `user=` / `password=` form fields;
- `login_doLogin` carries them inside the XML Bag in its `login` field
  (`<user>…</user>`, `<password>…</password>`).

`inject_identity()` in `benchmarks/scaling_probe.py` rewrites both.

**Cookies are not scoped by port.** Both stacks live on `127.0.0.1`, so a
browser used against one sends its cookies to the other as well — a legacy trace
recorded in that browser carries the bridge's cookies among the request headers,
and the other way round. When the replica compares the two stacks those are
leftovers of the browser, not divergences between the two implementations. Observed on
2026-08-23: a `sticky_cid` from the bridge arrived on a legacy request.

## The two recorders

- **HTTP recorder** — `http_recorder.py`, a WSGI middleware wrapping the app.
  Built. Writes `http` lines into the run archive.
- **Register recorder** — `register_recorder.py`, a wrapper object standing in
  place of `SiteRegisterClient`, patched by name in the `gnr.web.gnrwsgisite`
  namespace. Built. Writes `register` lines into the same archive, every line
  carrying the `exchange_id` of the exchange that caused the call.

Both are **installed by a plain call**, never by logic living inside a gunicorn
hook: the bridge has no gunicorn, and the same two recorders install there too.
On the legacy stack the register recorder cannot use a hook at all — the site
builds its register client in the master process before the configuration file
is read — so it installs from the launcher `serve_legacy.py`. On the bridge both
installs happen inside the worker, in `recording_worker.py`; see **The bridge
side** below.

The 16 threads in one process interleave the calls, so every line of both kinds
carries a thread id as well as the `exchange_id`. That is why the two recorders
are designed together.

### The seam between them: a request header

The HTTP recorder mints the `exchange_id` and injects it into the request as the
**`X-Bench-Exchange-Id`** header. The register recorder reads it back through
`site.currentRequest.headers`.

`GnrWsgiSite.currentRequest` is GenroPy's own per-thread request — a
`ThreadedDict` filled in `dispatcher` (`gnrwsgisite.py:1155`) and cleared at the
end (`:1446`), so it covers the **whole** dispatch, statics and `_ping`
included. `currentPage` would not: it is only set later, at `:1347`, and during
a ping it is still `None`. The register client has the site in hand —
`SiteRegisterClient.__init__(self, site)` stores it.

Three things follow, and they are why the seam is a header and not a
thread-local of ours:

- no global state in the bench code, and no reimplementation of per-thread
  affinity — GenroPy's own is used;
- the join key is **visible in the archive**, among the recorded request headers:
  the file carries the key it is joined on;
- the two recorders never import each other. They share the name of a header,
  nothing else — which is what makes the pair installable on the bridge.

Register calls made outside any request — service threads, or anything after the
end-of-dispatch cleanup — carry no `exchange_id`. That is information, not a
loss: in the trace they appear as calls belonging to no exchange.

### Running with both recorders

A recorded run replaces the command of step 5 with the launcher, from the
repository root. Step 5's plain command stays valid and is the declared
condition of a run **without** recorders:

```bash
PGGSSENCMODE=disable temp/legacy_venv/bin/python \
    benchmarks/compare/serve_legacy.py test_invoice_pg_legacy \
    -b 127.0.0.1:8099 -w 1 -k gthread --threads 16 \
    -c benchmarks/compare/gunicorn_recorders.conf.py
```

Everything after the script is genropy's own `serveprod` command line: the
launcher adds nothing and takes nothing away.

**Two recorders, two install points, one command.** The HTTP recorder installs
from `post_worker_init`, which runs right after gunicorn's own `load_wsgi()`, so
`worker.wsgi` is already the site application and one line wraps it (verified on
gunicorn 26.1.0). The register recorder cannot use any hook, and this is not a
preference:

- `gnrserveprod.main()` builds `GnrWsgiSite` **before** it reads the `-c` file,
  and hands an already-built application to gunicorn, whose `load()` only returns
  it;
- `GnrWsgiSite.__init__` forces the register into existence, under genropy's own
  comment "this is needed, don't remove";
- so the client exists in the **master** process before any hook runs and before
  the fork. Measured: master and worker hold one inherited socket to the
  sitedaemon, same descriptor, same address pair.

Patching the name from the configuration file would therefore be a no-op on the
instance the site already holds. The launcher assigns the name before genropy's
entry point runs — one plain assignment, which is what makes the same recorder
installable on the bridge, where the call goes wherever the worker builds its
site.

### The run archive

**The archive is the recording target, not a place lines are loaded into
afterwards** (owner, 2026-08-23, reversing the JSONL-plus-loader shape this
bench carried for a day). Three arguments were weighed and the load-later shape
lost all three. A truncated JSONL line is possible when a process dies
mid-write, while a half-written SQLite row is not. The fixed-schema objection
dissolves with the one-JSON-column design below. Lock contention between the
bridge's worker processes is real as mechanics and harmless here: WAL serialises
the writers, and the two fidelity macro-phases do not read timings —
macro-phase 3, which does, runs with collection off. What decided it is the
fourth argument, already paid for: **a separate load step is a step that can be
forgotten**, and the reference session of 2026-08-23 was lost exactly in the
window between the run and its archiving. Writing into the archive removes the
window.

One file per run, `~/genro_bench/runs/<run_id>.sqlite`, or under
`GNR_BENCH_ARCHIVE_DIR` when that is set. **Outside the git tree** — the lines
carry whole bodies, the login with the bench password and the session cookies,
and this repository is public — and on a **local** filesystem, because WAL does
not work over network mounts.

```
run     run_id, started, conditions   -- one row: the declared conditions, as JSON
record  id, run_id, stack, kind, exchange_id, ts, thread, subject, status, line
```

`line` holds the **whole** record as JSON. The other columns are **promoted**,
each because it has a job: `run_id` and `exchange_id` to JOIN, `stack` to
SEPARATE, `ts` and `thread` to ORDER, `kind`, `subject` and `status` to FILTER.
A promoted column is always a **copy** of what the JSON still holds, never the
only place a value lives — otherwise the blob stops being the record and this is
a schema again. A field is promoted once it is queried often; an occasional
query reads inside the JSON with `json_extract(line, '$.field')`. There is no
schema version, by the same rule that governs the record shape: a line of a new
shape needs no migration.

- `kind` is `http` or `register`, so both recorders share one table and one join.
- `subject` is what the line is *about*: the path for an HTTP exchange, the verb
  for a register call. It is the column you filter on by hand.
- `exchange_id` goes in as **NULL** when the record has no such key — the
  faithful copy of an absence, which is what the master's boot calls are.
- `stack` is the one declared exception to the copy rule: it is a copy of the
  run's own declared condition, not of the record. Putting it in the record
  would change a record shape that has to stay identical on both stacks.

**How the two recorders find the same run.** `serve_legacy.py` mints the archive
and publishes its path in **`GNR_BENCH_RUN`** before genropy's entry point runs,
so before the fork. The register recorder is handed the archive object directly,
through a `partial` — genropy builds its client as `SiteRegisterClient(site)`,
with no room for a second argument. The HTTP recorder, born later in the worker,
attaches to the path in the environment. The environment is the channel because
it is the only one that survives both a **fork** here and a **spawn** on the
bridge, where the worker process is started fresh.

**The connection is opened per process, never inherited.** A handle carried
across a fork is the same class of invisible defect as a shared file descriptor.

**A trap measured on 2026-08-23**: `sqlite3.connect` in a forked child
**segfaults** with sqlite 3.51.0 in WAL mode — SIGSEGV inside the C call, no
exception to catch. The bench venv carries sqlite 3.50.4 and does it cleanly,
which is why the recorded stack is unaffected and why `run_archive_check.py`
runs on the venv python. Same family as the libpq/Kerberos segfault that makes
`PGGSSENCMODE=disable` mandatory. Anyone moving the bench to a python with a
newer sqlite finds the gunicorn worker dying on its first recorded line.

### What lands in the archive, and what does not

**Filtered — one id-only stub line, never a body**:

- static assets, recognised by the **response content type** (javascript, css,
  images, fonts) plus `favicon.ico`;
- pings that rendered nothing.

A stub carries `exchange_id`, `ts`, `thread`, `method`, `path`, `query`,
`rpc_method`, `status` and `filtered` — the reason, `static` or `empty_ping`.
Nothing else: no bodies, no headers.

**Why a stub and not silence** (owner, 2026-08-23, amending his own filter). The
register recorder stamps every call with the exchange that caused it, filtered
exchanges included, so with no line at all those calls named an exchange the
HTTP lines did not contain: measured on the first reference session, 531 register
lines over 240 exchanges. And the ping is the carrier of the datachange half of
the register conversation — the half the bridge's emulation has no upstream test
suite for — so on the register side those exchanges are the material
macro-phase 2 most needs. Recognising them by guessing from their verbs is how an
artefact comes to read as a divergence. The rule the filter was built on is
intact: nothing recorded is ever cut, because a stub has no body to cut.

Recognition is by content type and never by path, except `favicon.ico`: the
decision is taken when the answer is known, with no guessing from the URL.

**The ping that rendered nothing is not an empty Bag** — that was the wrong
guess, and it made the filter never fire. `handle_ping` builds
`Bag(dict(result=None))` and adds a `dataChanges` node only when there is
something to deliver (`gnr/web/daemon/siteregister.py:928`), so on the wire the
idle answer is the bare envelope:

```xml
<GenRoBag><result _T="NN"></result></GenRoBag>
```

That shape — a null `result` and nothing else — is what the filter matches. As
soon as `dataChanges` appears the exchange is recorded.

**Recorded whole, with no truncation anywhere**: everything else. RPC calls,
pages, XML, JSON — and the pings that *do* carry a datachange, because that Bag
is the register answering, and it is what the replica compares in macro-phase 2.

One `http` line per exchange, with these fields inside `line`:

| Field | What |
|---|---|
| `exchange_id` | the join key with the register lines (a promoted column too) |
| `filtered` | present only on a stub: `static` or `empty_ping`. A stub carries none of the fields below except `status` |
| `ts`, `thread` | wall clock and thread ident (16 threads interleave) |
| `method`, `path`, `query` | the request line |
| `req_headers`, `req_body`, `req_len` | whole request |
| `rpc_method`, `form` | the RPC method and the form payload, parsed as `capture_proxy.py` does (`method` or `_M`) |
| `status`, `resp_headers`, `resp_body`, `resp_len` | whole response; headers as a list of pairs, so a repeated `Set-Cookie` survives |
| `gnr_headers` | the `X-Gnr*` breakdown: `X-GnrTime`, `X-GnrSqlTime`, `X-GnrSqlCount`, `X-GnrXMLTime`, `X-GnrXMLSize` |
| `duration_ms` | entry to end of the body, measured by the recorder |
| `recorder_error` | present only when the recorder itself failed on that exchange |

A failure inside the recorder is written as `recorder_error` and **never**
reaches the response: the response is served intact and the trace says the line
is incomplete.

The `X-Gnr*` headers only arrive on the page-serving path — they are set by
`setResultInResponse` (`gnrwsgisite.py:1371`). Statics would not have them, and
statics are not recorded anyway. With debug off, `X-GnrSqlTime` and
`X-GnrSqlCount` arrive as `0`; see the declared conditions above.

### What the register lines carry

One `register` line per call **the site made** — never one per wire round trip. Three
surfaces, and the line says which one it was intercepted on:

| Surface | What it means |
|---|---|
| `client` | a method declared on `SiteRegisterClient` (`get_item`, `page`, `new_page`, `refresh`, the four `*Store` builders, …) |
| `passthrough` | a name the legacy class's `__getattr__` forwards to the register (`drop_page`, `setInClientData`, `handle_ping`, …) |
| `store` | a call on a `ServerStore` handed back by one of the `*Store` builders |

The wrapper is an **object** standing in place of the client, not a patch on
`__getattr__`. That funnel is bypassed by about 26 methods declared on the class,
and the bridge's own client has no `__getattr__` at all — a recorder built on the
funnel would record nothing there.

| Field | What |
|---|---|
| `exchange_id` | the join key with the HTTP lines — a full record or a stub. **Absent** when the call belongs to no exchange, and NULL in the promoted column |
| `ordinal` | position within its exchange, from 1 |
| `surface`, `verb` | where it was intercepted, and the name called |
| `args`, `kwargs` | the arguments |
| `result` | the answer |
| `wire_calls` | round trips the call cost: `0` when it never left the process, `1` normally, more when the shape costs more or the legacy loop retried |
| `wire_error` | the error class the legacy retry loop swallowed, when it swallowed one |
| `error` | the exception that reached the site, when one did |
| `duration_ms`, `ts`, `thread`, `pid` | timing and provenance |
| `register_name`, `register_item_id` | store lines only: which register and which item |
| `recorder_error` | present only when the recorder itself failed on that call |

**`wire_calls` is counted on the Pyro proxy, not guessed.** The legacy retry loop
lives inside the closure `SiteRegisterClient.__getattr__` builds, and its
`except Exception` neither logs nor re-raises: from outside that funnel a
fourfold failure is indistinguishable from a legitimate `None`. Counting on the
wire and attributing to the call in flight on that thread is what makes the
number true, and it keeps one line per call. The two groups behave differently
and the trace shows it rather than hiding it: a `passthrough` verb can carry
`wire_calls: 4` with a `wire_error` and a `null` result, while a `client` method
raises and carries an `error`.

**The field is not called `attempts`, and the name was changed after it misled
its first reader** into taking a routine number for a retry. What one call costs
on the wire is a property of its shape, measured on the fake wire:

| Call | Round trips |
|---|---|
| `pageStore(...)` and the other `*Store` builders | 0 — the store is built in process |
| `store.register_item` | 1 |
| `store.data` | 2 — `ServerStore.data` evaluates `self.register_item` twice |
| `store.getItem(path)` | those 2, plus one on the remotebag proxy, which is not counted here |

So a Bag read through a store shows `wire_calls: 2` and has retried nothing. A
retry shows as more round trips than the shape costs, together with a
`wire_error`. On the bridge there is no per-call retry and no wire at all, so the
numbers will differ by nature — which is a real difference between the stacks and
not an artefact of the instrument.

**The calls with no exchange are recorded, not filtered.** The register client is
born in the master process, so the site's own boot makes real register calls
before any exchange exists: those lines simply have no `exchange_id` key. An
absent exchange is absent — never faked, never inherited from whatever ran last
on that thread. They are material the replica will want, since the bridge boots
its registers too.

**What is deliberately not recorded twice**: the call a store makes internally on
the client. A store keeps the client it was built from, so `set_datachange` on a
store produces the store line and no client line. A line is a call the site made.

**Values are written to be comparable between runs.** A Bag goes in as its XML:
the default `repr` carries no content and a memory address that changes at every
run would read as a divergence. Anything else goes in as its `repr` with the
address stripped. Long values are truncated with their real length appended.

The archive **opens its connection per process**. The wrapper is born in the
master, and a handle inherited across the fork would let two processes step on
each other — the same class of invisible defect as a recorder fault reaching the
response.

## The bridge side

The same two recorders, on the other stack. What changes is only how they are
caught — the lines they write are the same shape, field by field, and the
comparison of macro-phase 2 reads lines, never mechanisms.

### What is different, and why

| | legacy | bridge |
|---|---|---|
| Processes | gunicorn **forks** one master into workers | the pool **spawns** each worker as a fresh `python -m` process |
| The register | a daemon on the other side of a Pyro socket | in-process, inside the worker itself |
| The register client | `SiteRegisterClient`, most of its surface behind one `__getattr__` | `GenropyRegisterClient`, 49 explicit methods and no funnel at all |
| HTTP recorder install | `post_worker_init` in the gunicorn config | one call in the worker's constructor |
| Register recorder install | an assignment from `serve_legacy.py`, in the master | the same assignment, in the worker, before the site is built |
| Concurrency | 1 process, 16 threads | up to 6 workers, thread pools left at the core's defaults |

Two consequences reach the recorded lines, and both are real differences between
the stacks rather than artefacts of the instrument:

- **`surface` is always `client`.** There is no `__getattr__` to pass through, so
  the `passthrough` lines of the legacy trace have no counterpart here.
- **`wire_calls` is always 1.** The register lives in the worker's own process
  and no call costs a round trip. On legacy the same field counts what the call
  cost on the wire, which is how a swallowed retry stays visible; here there is
  nothing to swallow.

Everything else is identical, and that is checked rather than asserted: the
machinery that builds a line is *inherited* from `register_recorder.py` instead
of being written a second time.

### The three pieces

- **`bridge_recipe.py`** — the server recipe. The install rides the recipe: the
  pool names its worker class as an import string that the worker process
  resolves for itself, so a recipe naming the recording worker installs both
  recorders in every worker. Nothing is patched, no environment switch, no
  `sitecustomize`, and neither genro-asgi nor genropy-asgi is modified. It is
  the shipped recipe transcribed with ONE line changed, and the coverage check
  builds both and fails if anything else comes to differ.
- **`recording_worker.py`** — `RecordingGenropyWorker`, a `GenropyWorker`
  subclass. It assigns the recording client into `gnr.web.gnrwsgisite` *before*
  `super().__init__` builds the site — `GnrWsgiSite.__init__` forces
  `site.register` into existence, so an assignment made afterwards would patch a
  name the site has already read — and wraps `wsgi_app` with the HTTP recorder
  afterwards, outermost, so the exchange header is in the environ before
  anything reads the request.
- **`register_recorder_mixin.py`** — the mixin. Where the legacy client could be
  shadowed by a wrapper object catching every attribute, this one declares
  everything explicitly, so the recording client is a real **subclass** and each
  recorded verb is an override delegating to the parent.

### Only the outermost call is recorded

A line is a call the SITE made. On legacy that came for free: the wrapper stands
in front of the client and is not in its internal call path, so a command
calling another command produced one line. A subclass **is** in that path, and
the bridge's client calls six of its own public commands internally
(`get_item`, `local_item`, `pages`, `set_datachange`, `set_serverstore_changes`,
`subscribe_path`), while every `ServerStore` delegates its whole conversation
back to the client that made it.

Recorded naively the bridge's trace would carry lines the legacy one cannot
have, and macro-phase 2 would read a divergence produced by the instrument. So a
call that begins while another is already being recorded runs untouched. The
coverage check asserts both halves of that: the store's own conversation IS
recorded, its inner client calls are NOT.

### The trap: one file, two module names, two classes

The daemon override installs `genropy_asgi.siteregister` into `sys.modules` as
`gnr.web.daemon`. A module reachable under **both** dotted names can end up
executed twice — the same file then yields two distinct class objects, and an
`isinstance` between them is False.

Measured on 2026-08-24: importing the client as
`genropy_asgi.siteregister.siteregister_client` after the alias had already
loaded it produced a second copy, and the `ServerStore` handed back by one was
invisible to the other — every store call would have gone unrecorded, in
silence. So every bench module imports the client **the way the site imports
it**, from `gnr.web.daemon.siteregister_client`, and the coverage check pins
that: the class the site's own module holds must be the very one the mixin
subclasses.

Same family as the two other traps this bench has already paid for: the
libpq/Kerberos segfault behind `PGGSSENCMODE=disable`, and the SQLite one below.

### The SQLite trap does not bite here

The bridge runs on a python whose sqlite is **3.51.0** — the version that
segfaults when a connection is opened in a **forked** child in WAL mode, the
reason `run_archive_check.py` runs on the bench venv instead. The pool does not
fork: every worker is a fresh process, so there is nothing inherited to break.
Measured on 2026-08-24: parent and spawned child writing concurrently into one
WAL archive, clean.

### Declared conditions of a bridge run

Read by `serve_bridge.py` where each one is true and stored as data in the run
row, exactly as on the legacy side. The keys the two stacks share read side by
side; the ones only this stack has are the two extra packages and the commits.

| Condition | Standard value |
|---|---|
| Stack | bridge: `gnrasgiserve`, the register in-process, no daemon |
| Processes / threads | pool ceiling 6 (`worker_max_number`), thread pools at the core's defaults |
| Recorders | both, installed by the recipe naming the recording worker |
| Debug | **off** (`--nodebug`) — same reason as on the legacy side |
| Database | `test_invoice_pg`, postgres localhost:5432 — the same db the legacy twin serves |
| Bind | `127.0.0.1:8098` (the legacy stack keeps 8099) |

**Why the commits are recorded and the versions are not enough.** genropy,
genro-asgi and genropy-asgi are all installed **editable** on this side, so a
distribution version records the moment of installation, not the code that ran:
genropy reports `26.6.8` here and `26.8.19.1` in the legacy venv, while both are
the same working tree. Verified on 2026-08-24 by hashing the 319 `.py` files of
the ten shared packages (`app core db dev lib prj sql utils web xtnd`): byte for
byte identical. The run row therefore carries `genropy_commit` and
`genro_asgi_commit` beside the version strings, and it is the commits that say
whether two runs compared the same code.

### Running the bridge with both recorders

Hygiene first, as always: no stale process on 8098, and the legacy stack down if
it is still up. Then, from the repository root:

```bash
PGGSSENCMODE=disable python benchmarks/compare/serve_bridge.py test_invoice_pg \
    -p 8098 --nodebug
```

Every argument is the `gnrasgiserve` command line; the launcher adds only
`--config benchmarks/compare/bridge_recipe.py`, and leaves a `--config` the
caller named alone. It prints the archive it is recording into.

What the launcher owns is the RUN, not the install: it puts `benchmarks/compare`
on the import path — in this process and in the environment every spawned worker
inherits, which is how the workers resolve `recording_worker` — mints the archive
with the conditions above, and publishes its path in `GNR_BENCH_RUN`. The
environment is the only channel that reaches a spawned worker: no object crosses
a spawn, which is exactly why the two recorders were built to agree on a path
rather than on an inherited handle.

The recipe itself does nothing but declare. That is deliberate: building a
recipe has to stay free of consequences, or the drift check could not build it.

**The first worker may be killed before it presents itself.** Building a
`GnrWsgiSite` can outrun the pool's 10s presentation timeout on a cold start;
the pool logs `its process never presented itself` and starts another, which
comes up. Observed on 2026-08-24 with the recorders installed, and it is not
theirs: they add nothing to the site build. Worth knowing so the traceback in
the log is not read as a failure of the bench.

## The reference session

What the collection produces is a **reference**: one archived run of a session
performed in the browser, plus the recipe to make another. There is no script
beside it — in the replica shape the recorded lines *are* the script the replica
reads, and a hand-written sequence next to them would be a second source of
truth free to drift.

**The archive is never committed, and it is kept.** The lines carry whole
bodies: the login exchange with the bench password, the session cookies, and
this repository is public — so the file lives outside the tree. It is kept
because the recipe alone is not enough: a session performed by hand in a browser
does not reproduce identically, and the clean restart wipes the register at every
run. Measured and unrecoverable a minute later is what happened on 2026-08-23,
before the archive existed.

**One reference per stack, the same session.** The steps 3 to 5 below are
identical on both sides — that is the whole point: what differs between the two
archives must be the stacks, not what was done to them. Only the bring-up and
the shutdown differ.

The session, under the declared conditions above and starting from an empty
register:

1. hygiene — no stale process on 8098, 8099 or 40004, and the register empty.
   **Legacy**: stop gunicorn and the sitedaemon, delete `siteregister_data.pik`
   from the site folder. **Bridge**: stop the server; there is no pickle to
   delete, the registers live in the workers and die with them. Nothing to
   delete on the recording side either way: the run mints its own file;
2. start the stack — **legacy**: the sitedaemon (step 4), then `serve_legacy.py`;
   **bridge**: `serve_bridge.py` alone. Either prints the archive it is
   recording into;
3. in the browser, log in with an account from `benchmarks/usernames.txt`,
   password `a`;
4. open one table page and let the grid load;
5. open one record, change one field, save it.

Do it in a **private window**, so the login really lands in the archive instead
of being skipped by a session cookie left over from a previous run.

Then that archive is the reference: every register line joins an HTTP exchange by
`exchange_id` — a full record or the stub of a filtered one — except the boot
calls, which have no exchange by construction.

**Close the run before reading it.** Stop the server first — gunicorn then the
sitedaemon on legacy, `serve_bridge.py` on the bridge — because while they run
the browser's idle pings keep landing in the archive and any census taken
earlier stops matching. Then fold the WAL into the file itself, so the
`.sqlite` alone is the whole archive and nobody loses the tail by copying it
without its `-wal` companion:

```bash
sqlite3 ~/genro_bench/runs/<run_id>.sqlite 'PRAGMA wal_checkpoint(TRUNCATE);'
```

**Reading it back.** The join, and the census that says whether the run is whole:

```sql
-- register lines naming an exchange the HTTP lines do not carry: must be 0
SELECT count(*) FROM record r
 WHERE r.kind = 'register' AND r.exchange_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM record h WHERE h.kind = 'http'
                     AND h.run_id = r.run_id AND h.exchange_id = r.exchange_id);

-- the census of a run
SELECT kind, count(*) FROM record GROUP BY kind;
SELECT json_extract(line, '$.filtered') AS filtered, count(*)
  FROM record WHERE kind = 'http' GROUP BY filtered;
SELECT json_extract(line, '$.surface') AS surface, count(*)
  FROM record WHERE kind = 'register' GROUP BY surface;

-- one RPC exchange and the register conversation it caused, in order
SELECT subject, json_extract(line, '$.rpc_method') FROM record
 WHERE kind = 'http' AND json_extract(line, '$.rpc_method') IS NOT NULL;
SELECT json_extract(line, '$.ordinal'), surface, subject,
       json_extract(line, '$.args'), json_extract(line, '$.wire_calls')
  FROM record WHERE kind = 'register' AND exchange_id = '<the id>'
 ORDER BY json_extract(line, '$.ordinal');
```

**Recorded evidence, reference run `legacy-20260823T232924`** under the declared
conditions above (debug off, 1 process and 16 threads, register empty at start,
both recorders, db `test_invoice_pg`), performed in a private window so the login
is really in the archive. The session was login, one table page with its grid,
one record opened, one field changed and saved:

| | |
|---|---|
| HTTP exchanges | 266 — 32 full records, 234 stubs |
| Stub reasons | 223 `static`, 11 `empty_ping` |
| Register calls | 1788, on 17 threads — 833 `client`, 878 `store`, 77 `passthrough` |
| Unjoinable register lines | 0 |
| Calls with the exchange absent | 2, both the master's boot |
| `recorder_error` | 0 |
| Register calls per RPC exchange | 25 exchanges: 5 minimum, 25 median, 92 maximum |
| RPC methods in the run | `*|login:LoginComponent;login_checkAvatar`, `*|login:LoginComponent;login_doLogin`, `main`, `getRemoteTranslation`, `app.dbSelect`, `app.getSelection`, `app.checkFreezedSelection`, `loadRecordCluster`, `saveRecordCluster` |

The two login calls arrive with their component prefix in the `method` field —
that is what the wire carries when the login page is a component, and the flat
`user=`/`password=` fields plus the XML Bag of the login trap are unchanged
underneath.

**The filtered exchanges are not free, which is why they get a stub.** Their
register traffic, measured on this run:

| Filtered exchange | Count | Register calls each | Verbs |
|---|---|---|---|
| `static` | 223 | 2 | `globalStore`, `getItem` on the global register |
| `empty_ping` | 11 | 5 | `globalStore` and `getItem` twice, then `handle_ping` |

501 register calls on exchanges the HTTP lines would otherwise not have
mentioned. And the split is only knowable *because* of the stub: before it, both
shapes were exchanges with no HTTP line, and a two-line one could not be told
from a ping — an earlier run of the same session showed 223 two-line and 17
five-line exchanges with no way to say which was which, and the guess made at
the time (all of it ping traffic) was wrong: the two-line ones are statics.

**What one RPC exchange looks like read out of the archive.** `saveRecordCluster`
in the reference run, its 32 ordinals unbroken: the identity reads on the global,
connection and page stores, then `get_dbenv`, then `subscribed_tables` →
`filter_subscribed_tables` → `notifyDbEvents` on `invc.customer`, then
`subscription_storechanges`, then the page lock — `__enter__`, `get`, `setItem`,
`__exit__` — around the write. Every `getItem` on a store costs `wire_calls: 2`
for the reason given above, and every `*Store` builder costs 0: the store is
built in process.

### Exercising the recorders without a browser

Five scripts in this folder, all runnable from the repository root.

```bash
python3 benchmarks/compare/http_recorder_check.py
temp/legacy_venv/bin/python benchmarks/compare/register_recorder_check.py
temp/legacy_venv/bin/python benchmarks/compare/run_archive_check.py
GNR_DAEMON_PROVIDER=genropy-asgi PYTHONPATH=benchmarks/compare \
    python benchmarks/compare/bridge_coverage_check.py
python3 benchmarks/compare/drive_login.py [username]
```

`http_recorder_check.py` needs nothing running: a minimal WSGI app, a recorder
wrapping it, a throwaway archive, and 24 assertions over the filters, the stub of
a filtered exchange, the whole bodies and the three guarantees about failure —
on the request side, on the reply side, and inside the archive writer itself. It
is the machine evidence that a fault inside the recorder never reaches the
response — kept versioned rather than in a scratch file, because evidence that
gets deleted is not evidence.

`run_archive_check.py` needs nothing running either, and runs on the bench venv
for the sqlite reason above: 17 assertions over the schema, the run row and its
conditions, WAL, attaching to an existing archive, every promoted column as a
copy of the JSON, the absent exchange as NULL, the join in both directions, and
the connection that is never inherited across a fork.

`register_recorder_check.py` needs nothing running either, but it does need the
bench venv, because the recorder imports genropy: 35 assertions over the two
client surfaces, the store and its lock, the legacy retry loop (the real one —
the client is built past its `__init__` with a fake proxy in place of the wire),
the absent exchange, the comparable values, and the promise that a fault inside
the recorder — the archive writer included — never reaches the site. Among them, one guards a trap worth naming:
`register.locked_exception` is a **class**, so it is callable, and a wrapped
class stops matching the `except` clause that uses it — silently, in a path that
only runs once something has already gone wrong. The wrapper hands back anything
that is not a routine untouched, and `inspect.isroutine` is the guard, not
`callable`.

`bridge_coverage_check.py` needs nothing running and runs on the bridge's own
interpreter: 22 assertions, and it is the one check whose job is to fail *later*
rather than now. The bridge recorder is a mixin over an explicit list of verbs,
which can silently fall behind the client it shadows — the legacy wrapper never
could, since it caught everything — so this check compares the list against the
client's live surface in both directions and says which name appeared or
disappeared. It guards the second copy this side needs as well: it builds the
bench recipe and the shipped one through the runtime's own read door and fails
if they come to differ in anything but the worker class. Then it pins the
module-identity trap described above, and exercises the recorder itself on a
stub site — one line for the call the site made, none for the calls the client
makes on itself, the store handed back wrapped and its own conversation
recorded.

The two env vars are not optional: without the provider the check would look at
the legacy Pyro client instead of the bridge's — which is exactly the mistake it
exists to catch, so it fails loudly on its first assertion rather than passing
against the wrong class.

`drive_login.py` replays a real login against the running site over HTTP, no
browser involved: it reuses `replay_a1.build_plan` to pull the two login
pageCalls out of the captured session and `scaling_probe.login_user` to replay
them on one keep-alive connection, with the identity rewritten in **both** places
(see the login trap above). Default user `alexander.king`, password `a`. Use it
to leave a login in the archive whenever a recorder changes.

Wrapping the app costs the `wsgi.file_wrapper` fast path: gunicorn only takes it
when the application returns a file wrapper, and the recorder returns a
generator. Irrelevant for fidelity work, worth knowing before anyone reads
timings off a recorded run.
