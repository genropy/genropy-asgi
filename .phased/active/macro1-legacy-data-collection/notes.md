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
  work. The doubled database only becomes a problem when a macro writes, so the
  loop starts read-only and a db copy is a later run condition. Both recorders
  survive this change and matter MORE: at each stop, the register trace is what
  says whether the bridge reached the same answer through the same calls, which
  the HTTP layer cannot see. Open: whether this becomes Phase 4 of this workflow
  (the bridge already exists, no waiting) or a rewritten Macro 2 on the roadmap.

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
