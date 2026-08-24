## Phase 1

- **The install recipe needed the `pgsql` extra.** The plan's Details step 1
  installs `gnrpy` plain, but the Postgres driver (`psycopg2-binary`, `psycopg`)
  is an optional dependency in `gnrpy/pyproject.toml`, so the venv could not
  reach the database and the phase's `Done:` — a successful login — was
  unreachable. Installed `gnrpy[pgsql]`. `gunicorn` is a base dependency, so the
  plan's "install it if not" check was a no-op.
- **The twin instance carries configuration only.** `site/data/` held the live
  runtime state of the other stack (35 connection folders, `_users`,
  `_frozen_users`) and `site/_static/_jslib` a regenerable minified-js cache.
  Copying them would have started the first trace on another run's leftovers,
  so only `instanceconfig.xml`, `root.py` and `site/siteconfig.xml` were copied.
  The site recreates the rest. Owner approved on 2026-08-23.
- **Debug off is the standard declared condition.** Rejected running in debug
  even though it would populate `X-GnrSqlTime` / `X-GnrSqlCount`:
  `gnr web serveprod --debug` wraps the site in werkzeug's debugging middleware,
  which the bridge has no equivalent of, so error responses would diverge
  because of the instrument rather than the stacks. The recorders collect the
  `X-Gnr*` headers either way, so nothing has to be re-instrumented; a debug run
  is the declared variant when those two fields must carry real numbers. Owner
  approved on 2026-08-23.
- **The sitedaemon binds 40004 and the site config cannot change it.** The CLI
  passes no port, so the chain in `gnr/web/daemon/service.py` ends at
  `PYRO_PORT` in `gnr/web/daemon/siteregister.py`. The port 40404 the site
  config reports under `gnrdaemon` is the address of the multi-site daemon and
  is never used here. Worth knowing before anyone tries to run two standalone
  sitedaemons side by side.
- **A stale multi-site `gnrdaemon` (from 2026-08-19) was holding 40004** and
  would have silently prevented the sitedaemon from starting. Terminated with
  the owner's approval; the hygiene check in the README exists for this.

## Phase 2

- **The seam is a request header, not a thread-local of ours** (owner's own
  proposal, 2026-08-23, adopted after verification). The site already keeps the
  current request per thread: `GnrWsgiSite.currentRequest`, a `ThreadedDict`
  filled in `dispatcher` (`gnrwsgisite.py:1155`) and cleared at `:1446`, so it
  spans the whole dispatch — statics and `_ping` included. `currentPage` would
  not have worked: it is only set at `:1347`, and during a ping it is still
  `None`, which is precisely an exchange whose register calls must be
  attributable. The register client holds the site
  (`SiteRegisterClient.__init__(self, site)`). Three gains over the thread-local
  the plan first named: no global state in the bench code, the join key visible
  in the trace among the recorded request headers, and two recorders that share
  only a header name instead of importing each other — which is what makes the
  pair installable on the bridge.
- **A filter, not a truncation** (owner, 2026-08-23). Statics and empty pings
  produce no line at all, so nothing that IS recorded is ever cut. The
  alternative — a uniform cap — was rejected because macro-phase 2 diffs bodies,
  and a divergence past the cap would be invisible.
- **The idle ping is not an empty Bag.** First guess, and it made the filter
  never fire on the live stack: `handle_ping` builds `Bag(dict(result=None))`
  and only adds `dataChanges` when there is something to deliver
  (`gnr/web/daemon/siteregister.py:928`), so the wire shape is
  `<GenRoBag><result _T="NN"></result></GenRoBag>`. Caught by looking at a real
  recorded ping, not by reading the code — worth remembering as the cheaper
  order of operations.
- **`__file__` does not exist in a gunicorn config file here.** genropy's own
  `load_config_file` (`gnr/web/cli/gnrserveprod.py:39`) does `exec(code, config)`
  into a bare dict, unlike gunicorn's loader — which is why the ancestor
  `benchmarks/gunicorn_count.conf.py` hardcoded an absolute path. The config
  recovers the path from the frame instead
  (`inspect.currentframe().f_code.co_filename`, the path genropy compiled), so
  any `-c` argument works from any working directory.
- **A recorder failure did reach the response, once.** The buffering decision in
  `relay_body` sat outside the try block, so an exception there propagated after
  `start_response` had already fired. Found by the isolation check, not by
  reading. Now the decision is guarded and defaults to buffering; the same
  failure recurs inside `write_record`, where it is recorded as
  `recorder_error`.
- **Wrapping the app costs the `wsgi.file_wrapper` fast path**: gunicorn only
  takes it when the application returns a file wrapper, and the recorder returns
  a generator. Irrelevant to fidelity, relevant to anyone reading timings off a
  recorded run.
- **`X-GnrSqlTime` / `X-GnrSqlCount` arrive as `0`, not empty**, with debug off —
  measured on a real exchange. Phase 1's README wording said "empty" and was
  corrected.
- **Every run starts from an empty register** (owner, 2026-08-23), which means
  restarting the sitedaemon at every restart, not only gunicorn: on the bridge a
  restart wipes the registers unless a soft reset says otherwise, so a legacy run
  inheriting a live register is not comparable. And restarting the daemon is not
  enough on its own — it saves its status on stop
  (`gnr/web/daemon/siteregister.py:1057`) and restores it on start when the
  pickle exists (`:1087`), so `siteregister_data.pik` has to be deleted. Gunicorn
  starts last: `SiteRegisterClient` reads the Pyro URIs from `sitedaemon.xml`
  when it is built.
- **Gunicorn holds no session state.** After a gunicorn-only restart the owner
  found himself still logged in: the identity is the signed site cookie (it
  carries `user` and `connection_id`) plus the daemon's register, and neither was
  touched. Recording a login therefore requires the clean restart above, or a
  logout, or a private window.
- **Cookies are not scoped by port**, and both stacks run on `127.0.0.1`: a
  browser used against one sends its cookies to the other too. A `sticky_cid`
  from the bridge was observed arriving on a legacy request. In the macro-phase 2
  diff those are browser leftovers, not divergences between the stacks.
- **The login is driven over HTTP, not through a browser** (owner, 2026-08-23:
  "mi aspettavo che tu facessi la chiamata http e verificassi il risultato").
  Phase 1 had already replayed it that way. The login-gated rule that hands a
  browser check to the human is about a person's own identity in a real session,
  not about replaying a documented bench account (`benchmarks/usernames.txt`,
  password `a`) against `127.0.0.1`. So `drive_login.py` is versioned and the
  check is the machine's.
- **The two helper scripts are versioned, not scratch.**
  `http_recorder_check.py` is the only machine evidence for the promise that a
  recorder fault never reaches the response, and `drive_login.py` is what makes
  the recorder exercisable without a human. Both would have died in `temp/`,
  which is gitignored.
- **Deleting the trace with the server up is a mistake easy to repeat** — made
  twice in this phase, the second time minutes after documenting it. The README
  warning is worth its space.
- **Naming review: accept all** (owner, 2026-08-23). Twelve new names, no
  minimality flags. Two with a single caller were kept separate on purpose:
  `is_empty_ping`, because it carries the wire-shape rule where it can be read,
  and `append_error`, because it keeps `write_record` legible.
- **Macro 2 is going to change shape** (owner, 2026-08-23, decided during this
  phase and NOT yet written into the roadmap): instead of recording the bridge
  and diffing the traces offline, drive both stacks and stop at the first
  divergence, then fix genro-asgi, genropy-asgi or the tester until the two
  agree. The objection that killed the naive version — a divergence
  contaminating everything downstream — dissolves precisely because nobody walks
  past the first one. Identifiers still have to be adapted per stack (each has
  its own session, page_id, connection_id), which the owner accepts as iterative
  work. [SUPERSEDED 2026-08-23, see ## Phase 3: the bridge side runs on a
  database copied on the fly, so writes are allowed from the start.] The doubled
  database only becomes a problem when a macro writes, so the loop starts
  read-only and a db copy is a later run condition. Both recorders
  survive this change and matter MORE: at each stop, the register trace is what
  says whether the bridge reached the same answer through the same calls, which
  the HTTP layer cannot see. Open: whether this becomes Phase 4 of this workflow
  (the bridge already exists, no waiting) or a rewritten Macro 2 on the roadmap.
  [RESOLVED 2026-08-23 — a rewritten macro-phase 2 on the roadmap, not a Phase 4
  here: the convergence loop MODIFIES the bridge, which this workflow's rules
  forbid, and it has no end a phase's `Done:` could state. Written into
  `.phased/roadmap.md` 2.0, commit 6bed20e.]

## Phase 3

- **Foreman decision on the `clarify?` of 2026-08-23 — the interception point is
  a wrapper OBJECT, not the legacy `__getattr__`.** The phase's `Decisions:` line
  called `__getattr__` the class's single funnel; that is wrong, verified at the
  source in the bench venv: `SiteRegisterClient` declares about 26 explicit
  methods found on the class — `new_page`, `new_connection`, `pages`,
  `connections`, `users`, `counters`, `refresh`, `get_item`, `page`, `connection`,
  `user`, `make_store`, the four `*Store` builders, `dump`, `load` — which reach
  the Pyro proxy directly and never touch `__getattr__`. Patching that funnel
  would record the residue and miss the lifecycle. The decision is not a
  preference: the plan's first `Must not break:` line requires the same pair of
  recorders to install on the bridge, and the bridge's `GenropyRegisterClient`
  has no `__getattr__` at all (the one at line 220 of
  `src/genropy_asgi/siteregister/siteregister_client.py` belongs to its
  `ServerStore`, whose class starts at 93 while the client's starts at 245). A
  recorder built on the funnel would therefore record NOTHING on the bridge. The
  wrapper object also needs no knowledge of which names are explicit, which is
  what keeps it installable on both stacks.
- **Store traffic is in scope for this phase**, tagged with the store's
  `register_name` and `register_item_id`. `ServerStore.__init__` keeps the client
  it was built from, so a store handed back unwrapped takes its whole
  conversation — `set_datachange`, `subscribe_path`, `reset_datachanges`,
  `drop_datachanges`, the lock in `__enter__`/`__exit__` — outside the recorder.
  A register comparison that cannot see `set_datachange` is not a register
  comparison: the datachange half is precisely where the bridge's emulation has
  no upstream test suite, so the trace is the only place a divergence would
  surface. Both stacks have a `ServerStore` with its own `__getattr__`, so the
  surface stays comparable in macro-phase 2, and the phase's own `Pattern:`
  (`benchmarks/sr_counter.py`) wrapped the store on purpose. Consequence for the
  record shape: a recorded line is a call on the client OR a call on a store, and
  the store lines carry which register and which item they belong to.
- **Foreman decision on the second `clarify?` of 2026-08-23 — the register
  recorder installs from a versioned launcher, not from a gunicorn hook.**
  Verified at the source, not taken on the child's word: in
  `gnr/web/cli/gnrserveprod.py` `main()` builds the application first
  (`get_gnr_wsgi_application`), reads the `-c` file only afterwards, and hands an
  already-built app to `GnrProductionServer`, whose `load()` just returns it;
  `GnrWsgiSite.__init__` forces the register into existence, under genropy's own
  comment saying not to remove that line. So the client instance exists in the
  MASTER process before the configuration file is read and before the fork —
  every hook, `post_fork` included, is too late, and patching the name from the
  config would be a no-op on the instance the site already holds. The child also
  measured it: master and worker share one inherited socket to the sitedaemon.
  Shape chosen: `benchmarks/compare/serve_legacy.py`, which calls the install and
  then `gnrserveprod.main()`. Rejected the alternative of reaching into the live
  site from `post_worker_init` to overwrite `domain_proxy._register`: it writes a
  private attribute of genropy and has to grip the closure the application
  factory returns, a hold that breaks silently on any refactor upstream. The
  launcher keeps installation a plain call, which is what the plan's first
  `Must not break:` line asks for, and the phase's own `Pattern:` had already
  learned the lesson — `sr_counter.py` installed itself sitecustomize-style
  precisely because the config file runs too late.
- **Phase 2's install path is NOT reopened.** The HTTP recorder stays where it
  is, installed from `post_worker_init`, verified and owner-confirmed. Two
  recorders, two install points, one documented command that runs both: rewriting
  closed, working work is scope the plan never bought.
- **The bare-stack command stays valid.** `benchmarks/compare/README.md` carries
  TWO launch commands from now on: the plain `gnr web serveprod` of Phase 1, which
  remains the declared condition of a run with no recorders, and the launcher for
  a recorded run. Phase 1's declared condition is extended, never falsified.
- **Two consequences of building the wrapper in the master, for the phase to
  handle.** First, no trace file handle may be opened in the master and inherited
  across the fork — two processes appending on one descriptor interleave
  mid-line; the writer opens per write or lazily per pid. Second, the master
  makes real register calls before any exchange exists (`__init__` forces the
  register, then `DataCollector(self.register.siteregister)`). Those are recorded,
  explicitly marked as belonging to no exchange, NOT filtered: the empty-ping
  filter exists for noise the wire carries anyway, while startup register traffic
  is exactly what macro-phase 2 will want to compare between the two stacks.
- **The `attempts` field and the surface it was intercepted on live in
  `Details:`**, with the rest of the record shape — no separate plan line needed.
- **Foreman ruling on the third `clarify?` of 2026-08-23 — `macros/` disappears
  with nothing in its place.** The owner has retired the word and, with it, the
  idea of a hand-written sequence to be replayed: the concept is REPLICA — he
  performs a session in a browser and the replica reproduces it, then compares
  the frontier register calls and the responses by STRUCTURE (same sequence of
  verbs, same shape of arguments and answers; a different customer read in the
  two runs is not a divergence). In that shape the recorded trace IS the script:
  the replica derives what to do from the exchanges it reads, so a written
  sequence would be a second source of truth for the same thing, free to drift
  from the first.
- **What Phase 3 owes instead is the reference, and the recipe to remake it.**
  Not a stored artifact: the traces carry whole bodies, including the login
  exchange with the bench password and the session cookies, and this repository
  is PUBLIC — they stay in `temp/`, gitignored, as the README already declares.
  So macro-phase 2 must never depend on an archived reference trace; it depends
  on the ability to PRODUCE one on demand. Phase 3 therefore closes with the two
  recorders working plus a README section stating, in plain words, what the
  reference session did and under which declared conditions — reproducible by
  recipe, never committed as data.
- **The replica and the structural comparison stay in macro-phase 2.** Phase 3 is
  legacy-only collection: it cannot own a mechanism that drives both stacks.
- **The bridge side runs on a database copied on the fly** (owner, 2026-08-23): a
  test cycle begins by copying the db for the genropy-asgi server, so writes are
  allowed from the start on that side. This supersedes the last bullet of
  `## Phase 2`, which had the loop starting read-only with a db copy as a later
  run condition. It is a macro-phase 2 run condition and belongs in the roadmap
  rewrite, not in this phase.
- **Three documents are the foreman's to amend, not the phase's**, and they are
  held together awaiting the owner's ok: Phase 1's step-by-step (the Postgres
  extra, the sitedaemon port, and now the second launch command), the roadmap's
  naming line — which still ratifies `macros/` and is now contradicted — and the
  roadmap's macro-phase 2, still describing the offline diff instead of the
  convergence loop.
- **The slug keeps `macro1`.** A branch name is a historical address, not a
  claim about vocabulary; renaming it mid-workflow would rewrite every reference
  for no gain.
- **`attempts` is counted on the Pyro proxy, and this is why.** The plan asked for
  the number of attempts and the error class, and neither is observable from
  outside the legacy funnel: the retry loop lives inside the closure
  `SiteRegisterClient.__getattr__` builds, and its `except Exception` neither logs
  nor re-raises, so a fourfold failure comes back indistinguishable from a
  legitimate `None`. The proxy is therefore wrapped as well — but it writes no
  line of its own: it deposits the count and the error class into the call in
  flight on that thread. Two properties matter more than the number. One line per
  call the SITE made, never one per wire round trip. And the same line shape on
  both stacks: on the bridge `client.siteregister` returns the client itself and
  there is no per-call retry, so `attempts` is honestly 1 there — a real
  difference between the stacks rather than an artefact of the instrument, which
  is exactly the distinction macro-phase 2 has to be able to make.
- **The guard is `inspect.isroutine`, not `callable`.**
  `site.register.locked_exception` is a class (`GnrDaemonLocked`), so it is
  callable: a wrapper that wrapped it would return a function, and the `except`
  clause using it would stop matching — silently, in a path that only runs once
  something has already gone wrong. `callable()` was the first guard written and
  the isolation check is what caught it. Pyro's proxy is not callable, so
  `DataCollector(self.register.siteregister)` at site boot was never at risk.
- **Bags had to become XML, and that was found on the live trace, not by
  reading.** The plan says Bags serialise as truncated `repr`; taken literally
  that produces `<gnr.core.gnrbag.Bag object at 0x10fcb0ce0>` — no content, and a
  memory address that changes at every run, so two runs of the same session would
  differ on every line carrying a Bag. Against a comparison that is STRUCTURAL by
  the owner's rule, that is not a detail: it is noise indistinguishable from a
  divergence. Bags now go in as `toXml()`, and any other repr has its address
  stripped.
- **A store's internal call is not recorded twice, on purpose.** A `ServerStore`
  keeps the client it was built from, so `set_datachange` on a store delegates to
  the real client and produces the store line only. The recorded line is the call
  the site made; the delegation is mechanical. The alternative — building the
  store ourselves so its delegation passes through the wrapper — would double
  every store call and duplicate one line of genropy's own logic.
- **A store's register reads are properties, so they are recorded by name.**
  `data`, `register_item`, `datachanges` and `subscribed_paths` cannot be
  intercepted as calls: reading the attribute IS the register read. Four names in
  a tuple, which is the one place the recorder needs to know a name — everywhere
  else it stays ignorant of which methods the class declares.
- **A property that raises AttributeError falls through to `__getattr__`.** Found
  while writing the isolation check: a fake client without `remotebag_uri` made
  `ServerStore.register_item` raise AttributeError inside the property, and Python
  turned that into a lookup on `ServerStore.__getattr__`, which reported
  "register_item has no attribute 'register_item'". Genropy's behaviour, not
  ours, and worth knowing before anyone debugs a store read.
- **The isolation check runs on the bench venv**, unlike Phase 2's, because the
  recorder imports genropy. It builds the real `SiteRegisterClient` past its
  `__init__` with a fake proxy in place of the wire, so the retry loop under test
  is genropy's own and not a copy of it.
- **Names**: `RegisterRecorder` stands in place of the client, `StoreRecorder` in
  place of one store, `WireCounter` in place of the Pyro proxy, `TraceWriter`
  appends the lines. Bench scaffolding, not package surface.
- **A recorder decision riding on a shared flag is this workflow's recurring
  defect, twice now.** In Phase 2 the buffering decision sat outside the try
  block, so a failure there reached the response after `start_response` had
  already fired. In this phase the *write* rode on the buffering *skip*: statics
  are not buffered, and the call to `write_record` hung off the same flag, so
  filtered statics produced no stub at all — while pings did, which made the hole
  look like a working feature. Same shape, same discovery route both times: the
  isolation check, never reading. Two decisions that happen to agree today get
  tied to one variable, and the day they should diverge nothing says so. Worth
  expecting again on the bridge, where the same recorder gets a second install
  point.
- **A store's Bag read costs two register round trips where one would do.** Not
  ours and not a defect of the recorder: `ServerStore.data` is
  `if self.register_item: return self.register_item['data']`, and `register_item`
  is a property that calls `get_item` on the register — so it is evaluated twice
  and pays a round trip each time (plus one on the remotebag proxy for the Bag
  operation itself). Measured on the fake wire. It sits on the polling path, so
  it happens constantly. genropy stays untouched, but macro-phase 3 will measure
  it and should not rediscover it as a mystery.
- **The field is `wire_calls`, and `attempts` was the wrong name.** It counted
  round trips, but it read as retries, and it misled its first reader — the
  foreman, who saw `2` on a routine global read and warned the owner that the
  legacy register was retrying. Nothing was retrying. The lesson is the project's
  own naming rule arriving from the other end: a name that needs a convention
  explained is a name that will be read wrong by whoever has not read the
  explanation, and in a bench a misread number becomes a divergence that is not
  there. A retry now shows as more round trips than the call's shape costs,
  together with a `wire_error`.
- **Naming review: the owner delegated the names for this instrument** ("per
  questo strumento scegli pure tu i nomi, mi raccomando significativi e seguendo
  le mie regole"), so they were rewritten against rules 9 and 11 rather than
  accepted as first written. What changed and why: `run_recorded` →
  `perform_recorded_call` (it performs and records, and a transitive verb carries
  its object); `recording` → `get_recorded_call` and `counting` →
  `get_counted_call` (pure reads that answer with a callable, so the explicit
  `get_` prefix); `end_flight` → `take_wire_count` ("flight" was a metaphor the
  reader had to learn, and the method both takes the count and restores the
  previous one); `wrapped` → `get_recorded_answer` (the old name said neither
  what it wrapped nor that it hands the answer back untouched when there is
  nothing to wrap); `store_fields` → `store_identity` (the fields ARE which
  register and which item); `next_ordinal` → `assign_ordinal` (it mutates a
  counter, so the verb leads); `readable` → `get_comparable_value` (the purpose
  is comparability between runs, which is what the Bag-as-XML decision is for);
  `current_exchange` → `current_exchange_id` (it returns the id, not the
  exchange); `TraceWriter.append` → `append_record`; `filtered_reason` →
  `get_filter_reason` and `stub_record` → `get_stub_record`; the constants
  `VALUE_REPR_LIMIT` → `VALUE_LENGTH_LIMIT` (values are no longer always a
  `repr`) and `STORE_READS` → `STORE_READ_PROPERTIES`. No record field changed,
  which is what let the reference session stand instead of being performed a
  third time — verified field by field, not assumed.
- **`duration_ms` was charging the call for our own serialisation**, found in the
  light review after the phase closed. The elapsed time was computed inside
  `write_record`, i.e. after `get_comparable_value` had run over the arguments,
  the keyword arguments and the answer — and for a Bag that means a `toXml()`.
  The number is now taken the instant the call returns, before anything is
  serialised, and a check pins it: with the serialisation artificially slowed to
  50ms, the recorded duration stays under it. It mattered because macro-phase 3
  reads these numbers and the inflation was invisible — a plausible value, just
  wrong, and worst exactly on the calls that carry the biggest Bags.

## Phase 4

- **The channel between the two recorders is the environment, and that is a
  choice about Phase 5.** The register recorder is born in the master and the
  HTTP recorder in the forked worker, so they have to agree on one archive file.
  An inherited Python object would have worked here — gunicorn forks — and would
  have failed on the bridge, where the pool names its worker as an import string
  and the worker process is started fresh: nothing crosses. So the launcher
  publishes the archive path in `GNR_BENCH_RUN` and every recorder attaches to
  it. The register recorder is additionally handed the object itself through a
  `functools.partial`, because genropy builds its client as
  `SiteRegisterClient(site)` with no room for a second argument, and a
  module-level global would have been the alternative.
- **`subject` is the name of the column that carries the path for an HTTP line
  and the verb for a register one.** Chosen by this session under the owner's
  Phase 3 delegation of the names of this instrument; the candidates offered
  were `subject`, `name` and `about`. It reads as what it is — what the line is
  about — and it is the column a person filters on when reading the archive by
  hand, which is why it earned a promotion out of the JSON.
- **`stack` is the one declared exception to the copy rule.** Every other
  promoted column repeats a value the record JSON still holds; `stack` repeats
  the run row's declared condition instead. Putting it into the record would
  change a record shape that the first `Must not break:` line requires to be
  identical on both stacks, and one file is one run is one stack anyway — the
  column exists so a query over several attached archives can separate them.
- **A SQLite connection opened in a forked child SEGFAULTS with sqlite 3.51.0 in
  WAL mode.** Measured on this machine's pyenv python 3.12.9: SIGSEGV inside the
  C call, nothing to catch, the child dead before its first line. The bench venv
  carries 3.50.4 and does it cleanly, which is why the recorded stack is
  unaffected and why `run_archive_check.py` runs on the venv python even though
  it imports no genropy. Same family as the libpq/Kerberos segfault behind
  `PGGSSENCMODE=disable`. It cost half an hour of bisecting a check that looked
  like a bug in the archive and was a property of the interpreter running it —
  and it is a live trap for whoever moves the bench to a newer python, where the
  gunicorn worker would die on its first recorded line.
- **One implementation assertion was removed with the implementation it
  photographed**: `sorted(rec.trace.__dict__) == ["lock", "path"]`, which pinned
  `TraceWriter` holding no open file between writes. `TraceWriter` no longer
  exists; the same property — the connection is never inherited across a fork —
  is now asserted where it lives, in `run_archive_check.py`, with a real fork.
  Declared here in the same change, per the plan's rule on the two kinds of test.
- **The WAL has to be folded in when a run ends.** Nothing on the legacy stack
  gets a "run finished" hook — gunicorn is killed — so the archive file can be
  left with its tail in the `-wal` companion. A reader opening the `.sqlite`
  sees everything, but anyone copying the file alone loses the tail. The README
  now closes the recipe with `PRAGMA wal_checkpoint(TRUNCATE)`.
- **Stop the servers before taking a census.** Between the census and the stop,
  two more idle pings landed in the archive and moved four of the numbers. The
  documented figures are the ones measured after the stop, and the README says
  to stop first for exactly this reason. Harmless here, silently wrong in a
  document that gets compared later.
- **The login RPC method carries its component prefix** in this run:
  `*|login:LoginComponent;login_checkAvatar`, where the Phase 3 README recorded
  the bare name. It is what the `method` field carries when the login page is a
  component; the identity itself still travels in the two places the login trap
  describes, flat fields and the XML Bag.
- **Naming review: accept all** (owner, 2026-08-23). Twelve new names, one
  minimality flag presented and waved through: `RunArchive.stack`, a property
  with a single caller over a dict that is already public. Kept because it names
  the one declared exception to the copy rule where that rule is applied, which
  is the same reason Phase 2 kept `is_empty_ping`. Two others were flagged as
  kept-on-purpose rather than as warnings — `RunConditions.database` and
  `.bench_commit`, single callers that carry the "read each condition where it is
  true" rule at the place it is obeyed.

## Phase 5

- **One file reached under two module names yields two classes, and the store
  recording dies in silence.** The daemon override installs
  `genropy_asgi.siteregister` into `sys.modules` as `gnr.web.daemon`; importing
  the client afterwards by its OWN dotted name executes the file a second time,
  and the two `ServerStore` classes that result fail `isinstance` against each
  other. The first version of the mixin imported it that way: every store call
  would have gone unrecorded, with no error anywhere. Caught by the coverage
  check's first assertion, which was written for a different reason — that the
  daemon provider is set at all — and turned out to catch this. Every bench
  module now imports the client the way the SITE imports it, from
  `gnr.web.daemon.siteregister_client`, and the check pins the identity.
- **A subclass is inside the client's own call path; a wrapper never was.** This
  is the one place where the mixin cannot simply reuse what the legacy recorder
  does. The bridge's client calls six of its own public commands internally
  (`get_item`, `local_item`, `pages`, `set_datachange`,
  `set_serverstore_changes`, `subscribe_path`), and every `ServerStore`
  delegates its whole conversation back to the client that made it — so a naive
  mixin writes lines the legacy trace cannot have, and macro-phase 2 would read
  a divergence produced by the instrument. Only the outermost call is recorded,
  which restores the semantics Phase 3 had already stated in words: a line is a
  call the SITE made. Rejected alternative: recording everything and filtering
  at read time — it moves a decision about what a line MEANS out of the
  instrument and into every future reader.
- **`serve_bridge.py` was added, and it is not in the phase's `Files:` list.**
  The first version minted the run inside the recipe's `main()`. That is wrong
  twice: building a recipe would then have consequences — the drift check could
  not build it without creating an archive — and `main()` is called by the
  config layering, not once per server. So the run moved to a launcher, which is
  exactly the role `serve_legacy.py` plays on the other stack. The plan's
  decision is untouched: the INSTALL still rides the recipe (the pool resolves
  the worker class string itself); the launcher owns only the RUN and the import
  path. One owner of the run per stack, symmetrical.
- **The mixin installs its overrides through `__init_subclass__` and a
  descriptor**, not fifty transcribed method bodies. The list of names stays
  explicit — that is what the coverage check reads — while `RecordedVerb` holds
  the verb and the parent implementation it shadows where a reader can see both.
  Rejected: a module-level closure factory (a module-level function that wants
  to be a method) and `locals().update(...)` in the class body (unreadable).
- **`wire_calls` is 1 on the bridge, and that is a statement, not a default.**
  The legacy field counts round trips so a swallowed retry stays visible. Here
  the register is in the worker's own process: there is nothing to swallow and
  nothing to count. Implemented as an override of `take_wire_count` rather than
  by leaving the counter at 0, because 0 would read as "not measured".
- **The pool's first worker can be killed before it presents itself.** Building
  a `GnrWsgiSite` outran the 10s presentation timeout on a cold start; the pool
  logged `its process never presented itself`, killed it and started another,
  which came up and served. Not the recorders' doing — they add nothing to the
  site build — but the traceback in the log reads like a failure, so the README
  says so.
- **The SQLite fork segfault does not reach the bridge.** The pool spawns fresh
  processes instead of forking, so the 3.51.0 trap that forces
  `run_archive_check.py` onto the bench venv has nothing to bite. Measured
  before writing a line of the phase: parent and spawned child writing
  concurrently into one WAL archive, clean.
- **The two stacks run the same genropy, and only the commits say so.** The
  legacy venv reports `26.8.19.1` and the bridge's editable install reports
  `26.6.8`, both from the same working tree at `9e39fe9c1` — verified by hashing
  the 319 `.py` files of the ten shared packages, identical byte for byte. An
  editable install never refreshes its metadata, so the bridge run row carries
  `genropy_commit` and `genro_asgi_commit` beside the version strings. Owner's
  call (2026-08-24): the bridge stays on the pyenv interpreter with the three
  editable working trees rather than getting a frozen venv of its own, because
  macro-phase 2 edits genro-asgi and genropy-asgi at every turn of the
  convergence loop and a frozen environment would have to be rebuilt each time.
- **Naming review: accept all** (owner, 2026-08-24). Thirteen free names, three
  minimality flags presented and waved through — `RecordedVerb.call`,
  `BridgeLauncher.publish_import_path` and `.start_run`, each with a single
  caller and each kept because it names a step the reader would otherwise have
  to reconstruct from a closure or a three-statement block. `ServerConfiguration`
  and its `main` are fixed by the runtime, which looks the recipe up by that
  class name. The `RunConditions` surface is deliberately ricalcata from
  `serve_legacy.py`, so the two run rows read side by side.

## Quality check 2026-08-24

Three defects found by the whole-diff review and repaired before the stamp.
None was in the recording mechanism itself; all three were in what the bench
would have TOLD us later, which is the worse kind.

- **A request that died left no HTTP line at all** (`http_recorder.py`). The
  exchange id is injected into the environ before the application is called, so
  the register recorder was already stamping calls with it — but the only writer
  is the `finally` of the generator `relay_body` returns, and an application
  that raises before returning an iterable never gets there. Measured: id
  injected, zero rows. The result was unjoinable register lines, the exact
  invariant the stub was invented to protect, broken on the one case
  macro-phase 2 most wants to compare. Repaired by writing the line from the
  `except` and re-raising untouched. Second half of the same repair: with no
  reply there is nothing to filter ON, so the filters are skipped and the
  exchange gets a full record whose null status says what happened — otherwise
  a `/_ping` that died would have been filed as an empty ping that answered
  nothing.
- **The anti-drift check compared the pool and nothing else**
  (`bridge_coverage_check.py`). The bench recipe transcribes the whole document
  — listener, middleware, applications, console gate, commander — and only the
  group kwargs were being compared. Measured: with the default port changed to
  9999 and `cfg.middleware()` deleted, the check still said the transcription
  had not drifted. Repaired by comparing the rendered XML of the whole tree,
  with the worker class substituted back so the one licensed difference does not
  mask the others.
- **The declared debug could lie** (`serve_bridge.py`). The run row derived it
  from `--nodebug` on the command line while the recipe derives it from
  `GNR_ASGI_DEBUG`; a variable exported in the shell would decide the run while
  the archive declared the opposite, and debug changes what the site measures.
  Repaired by importing the recipe's own rule (`DEBUG_OFF_WORDS`) instead of
  restating it, and by a check that simulates the real order — the launcher
  reads the condition, then the CLI writes the environment, then the recipe is
  built — over six combinations of flag and variable.

Each repair carries a check that fails when the defect is put back; verified by
putting each one back. The two archived reference runs are unaffected: the
bridge run has zero unjoinable lines, its declared debug matches what ran, and
the recipe was a faithful transcription on the day it was performed (diffed line
by line: only comments and the worker-class line).
