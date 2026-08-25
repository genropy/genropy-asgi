## Phase 1

- The path in `site_caller` is cut by the frame's dotted module name, not by
  walking the filesystem up while `__init__.py` exists. The walk was the first
  implementation and it overshot on the editable side: `genropy/gnrpy/` carries
  an `__init__.py` of its own, so the same genropy file came out as
  `gnr/lib/services/__init__.py` on legacy (frozen copy under site-packages) and
  `gnrpy/gnr/lib/services/__init__.py` on the bridge — a divergence produced by
  the instrument, found by the two smokes of this phase before any comparison
  code exists to be misled by it. The module name is what says where the package
  begins; the filesystem does not. Both smokes now write the same caller strings
  (0 callers unique to either stack).
- The frames to skip are derived from `type(self.client).__mro__` plus the
  recorder's own modules, so the bridge mixin needed no change: its client is a
  subclass, so the mixin's module arrives with the MRO. Rejected: an explicit
  list of module names, which is the drift trap `RECORDED_VERBS` already is.
- The legacy wrapper path is asserted in `register_recorder_check.py`, the bridge
  mixin path in `bridge_coverage_check.py`: `register_recorder_mixin` does not
  import at all on the legacy venv (`RecordedVerb.__init__` binds every verb to
  the legacy class, which has no `allowedUsers`), so one script cannot cover both
  sides. Foreman decision of 2026-08-24, recorded in the plan.
- Found in passing, NOT fixed here: `bridge_coverage_check.py` fails its two
  recipe-drift assertions, and it failed them before this phase. Commit 7cd15de
  added `engine_factory`/`engine_kwargs` to the shipped recipe
  (`src/genropy_asgi/spa/config.py`) and `benchmarks/compare/bridge_recipe.py`
  never followed, so the bench bridge still spawns workers that build their own
  site while the shipped one forks them from a template. The bench bridge runs
  and records (384 register lines in the smoke), so nothing is blocked today, but
  the two recipes now differ in more than the worker class.
- The field carries THREE frames, innermost first, joined by ` <- ` (owner,
  2026-08-24). One frame was the first implementation and the first measurement
  killed it: 242 of the 384 register calls a login makes come from the service
  cache check, whose innermost frame is always `gnr/lib/services/__init__.py:232`
  or `:234` and never says who asked for the service. With three frames the same
  calls read `... __call__ <- services/__init__.py:75 getService <-
  gnrwsgisite.py:723 getService`. The owner's principle behind the decision: the
  instrument may cost time while measuring FIDELITY; it must not while measuring
  PERFORMANCE (macro-phase 3). That mode does not exist yet — when it is built it
  must be a declared condition of the run, not a second record shape, or it
  breaks the "no format versioning" rule of the roadmap.
- `site_caller` is also a PROMOTED column of the archive, a copy of what the JSON
  line holds. Asked for by the owner as a table of call chains with an id, which
  would have made the chain live in one place only — the rule the archive is
  built on forbids exactly that. The promoted column buys the same query
  (`GROUP BY site_caller` with the call count and the summed milliseconds) with
  no second source of truth. Measured on the login: 121 calls / 94.2 ms on legacy
  and 122 / 18.5 ms on the bridge for the one chain.
- A site resource loaded under a flat module name — genropy loads its project
  resources that way — keeps three directories above the file:
  `packages/adm/model/preference.py` (owner, 2026-08-25). One frame gave
  `preference.py:23` alone, which does not locate the file. The absolute path
  was rejected as the fix: the two stacks read that same resource from different
  roots (frozen copy vs editable), so it would read as a divergence.
- What the two stacks do NOT share, measured with three frames: five chains
  differ by a line number in `gnr/web/gnrwsgisite.py` alone — 1350 vs 1356, 1663
  vs 1669, a six-line offset. It is not the bridge behaving differently: the two
  stacks run different genropy trees, and the run rows say so (legacy 26.8.19.1
  frozen under temp/legacy_venv, bridge 26.6.8 editable at commit 6da02feda —
  the version string is the stale one, the commit is the truth). Phase 3 prints
  `site_caller` and does not compare it, so this produces no divergence; anyone
  grouping ACROSS the two stacks has to know that line numbers shift.
- The `Done:` exception is the foreman's, recorded here as it asked (c74af49):
  `bridge_coverage_check.py` passes with its two recipe-drift assertions
  excepted. They fail for the drift described above, which is now Phase 4's
  work, not this phase's.

## Phase 2

- The bench now PINS genropy, and that was not in the plan. The precondition the
  phase owns — `genropy_parity_check.py` — refused at first run with 9 differing
  files, and the reason was not drift between two frozen copies: the developer's
  checkout had moved onto `fix-1153-service-freshness`, whose one commit
  (`e2bdb1e60`) skips the register read for services not configured in the
  database, which is 242 of the 384 register calls a login makes. The bridge,
  installed editable, was already running it. The legacy stack was not. Owner's
  decision, 2026-08-25: keep the two stacks still, on a dedicated worktree.
  `<genropy>/worktrees/bench-baseline`, detached at `6da02feda` — the commit
  `develop` carries and the one the bridge ran during Phase 1.
- Pinning the `gnr` package alone would not have been enough, and this is the
  part that was nearly missed: `resources/`, `packages/` and `webtools/` are
  executed code, resolved through `~/.gnr/environment.xml`, which names the
  developer's checkout. They would have kept moving under the bench. The way out
  is genropy's own `GENRO_GNRFOLDER`: a configuration folder of the bench,
  `temp/gnr`, whose `environment.xml` names the pinned trees. `projects.genropy`
  deliberately does NOT move — the bench instances live in the checkout,
  untracked, and never travel with a commit.
- Nothing global was touched, and that was a constraint, not a preference: the
  bridge's genropy is installed editable in the pyenv 3.12.9 that every project
  on this machine shares, so re-pointing that install would have changed genropy
  everywhere. `PYTHONPATH` does the same job per process and wins, because the
  editable finder appends itself to `sys.meta_path` and the ordinary path lookup
  answers first (measured 2026-08-25).
- The bench configuration folder MUST be called `gnr`. genropy loads the whole
  folder as one Bag whose top node is the folder's own name, and every lookup is
  written `gnr.environment_xml…`. A folder called `bench_gnr` loaded without
  complaint and answered `None` to every question; the sitedaemon died with
  `TypeError: argument of type 'NoneType' is not iterable`. Cost: one failed
  start. Written into the code and the README so it costs nobody else.
- `serve_legacy.py` now declares `genropy_source` and `genropy_commit`. The
  source is read from the installer's own `direct_url.json`, not guessed: a
  version string records when a copy was installed, never what it holds, and a
  frozen copy is no working tree of its own. Both archives now name the same
  commit, so two runs that disagree about their genropy say so in the run row.
- What the replica does NOT replay, both declared because a silent skip is a
  divergence nobody can see afterwards: `/_ping` (a heartbeat on a timer — what
  it carries depends on when it fires; consequence, the delivery of datachanges
  through a ping is never compared; in the reference session the rule costs one
  non-empty ping, the 412 before the login) and statics (223 of 266 exchanges,
  each producing the same pair of register calls, `globalStore` and `getItem`,
  446 lines carrying no information; consequence, the serving of static assets is
  never compared). Owner's decision, 2026-08-25.
- The pairing between the reference run and the replica run rides a REQUEST
  HEADER, `X-Bench-Replica-Of`. The HTTP recorder already writes every request
  header into its line, so the join Phase 3 needs exists in the archive with no
  recorder change and no new field in the record — the "no format versioning"
  rule of the roadmap is untouched. Rejected: writing a pairing file beside the
  archive, which would have been a second source of truth, and pairing by
  position in the replayed sequence, which is true only until the first skipped
  exchange changes.
- One recorded status cannot be reproduced, and the rule that says so is
  declared. In the reference session the two `login_doLogin` calls OVERLAP by
  22.8 ms on different threads, both carrying the pre-login cookie: the first
  rotates the connection, the second answers 400 with `The connection is not
  longer valid`. A replica replaying in order on one connection gets the 200 the
  site owes a legitimate call. Foreman decision, 2026-08-25 (`1ab7029`): an
  exchange whose recorded reply says the connection was already rotated AND
  which the trace shows overlapping an earlier one on the same pre-rotation
  cookie is a recognised race of the reference, not a divergence of the stack —
  reported by the replay, never passed in silence, and promoted to a named rule
  in Phase 3's declared-rules table. Both conditions are required: the same
  error with no overlap is a stale tab, and that one IS reproducible.
- The marker string is copied verbatim from `gnr/web/gnrwebpage.py:307`, typo
  included: the site writes `is not longer valid`. The first implementation
  wrote the grammatical `no longer` and matched nothing — the race went
  unrecognised and the replay exited 1. It is a literal the site emits, not a
  sentence.
- The identity map is a plain token substitution over the whole text of every
  form value and of the query string, not a list of keys to rewrite. The GET of
  a TH page carries `_calling_page_id` in its query string, and `main` carries
  `_calling_page_id`/`_root_page_id`/`_parent_page_id` in its form: a key list
  would have to grow every time the application uses a new one. `callcounter` is
  deliberately NOT adapted — the sequence replayed is the sequence recorded.
- `TraceReader.records` answers with fresh dicts on every read, so the overlap
  search compares exchanges by `exchange_id` and never by identity. The first
  implementation used `is`, which silently never matched the record handed in.
- `TraceReader.conditions` was written and then removed at the close: it read the
  declared conditions of the archived run, and only the check script ever called
  it — the replay never looks at them. Minimality applied honestly: Phase 3
  compares two runs and has a reason to read them, and it adds the property the
  day it does. Owner delegated the naming of this bench to the executing session
  (2026-08-25: "per questo progetto di comparatore non serve fare il battesimo
  nomi"), so every other proposed name stands as written.

## Phase 3

- The comparison never became a script beside the archives: `structural_diff.py`
  receives two readers and knows nothing about where they come from. The reader
  stayed `TraceReader` in `replica.py`, extended with `conditions`,
  `get_register_lines`, `last_record_id` and `get_exchange_replaying` — the
  property Phase 2 removed came back exactly where it said it would. A second
  reader inside the comparison was written first and deleted: two readers over
  one archive shape is the duplication the bench keeps refusing, and the only
  thing it bought was avoiding an import cycle that does not exist, since
  `replica.py` imports the comparison and the comparison imports nothing.
- The race rule MOVED out of `TraceReader.get_race_reason` into the declared-rules
  table, as the plan asked when it wrote "promoted to a DECLARED rule in Phase 3's
  rules table". The four Phase 2 assertions that called it now call
  `DeclaredRules().get_status_reason`, and one was added: the recognised status
  names the rule that recognised it. One table, two questions — a recorded status
  the replay cannot reproduce, and a register divergence that is a known fact —
  and a rule answers `None` to the one it does not know.
- The shape rule is what the whole comparison rests on, and it was chosen on
  measurement, not on taste. On the browser session of 2026-08-23 against its own
  replay, restricted to the exchanges whose call sequence already agreed: 636
  lines, 29 differing on the raw answer, 12 on the shape. The 29 are the clock,
  the user agent and the browser's `Accept-Language`; the 12 are real, four of
  them a connection register item that carries `last_refresh_ts`, `last_rpc_ts`
  and `last_user_ts` on one side and not on the other. A dict repr therefore
  compares by KEY NAMES and a Bag by NODE PATHS: values dropped, structure kept.
- The archive of 2026-08-23 is disqualified as a self-check reference, measured
  before any comparison code existed: 21 of its 31 paired exchanges differ at the
  level of the call sequence alone. Its register lines predate `site_caller`, its
  run predates the genropy pin, and its pages carry `rootenv.workdate`, so
  genropy's `_get_workdate` skips a four-call block (`pageStore`, `__enter__`,
  `getItem('rootenv')`, `__exit__`) that a replay performs. The plan's own Notes
  already said Phases 1-4 run on `drive_login` smokes; the foreman put the
  disqualification on the record in 335e548.
- A replay anchors itself to the target archive with `last_record_id` at start,
  and this was a real defect, not a precaution: a stack records EVERY cycle into
  the file it minted at startup, so a second replay looking up
  `X-Bench-Replica-Of` from the beginning finds the FIRST replay's exchange and
  compares it against itself. That comparison passes always and says nothing —
  the worst failure a comparison can have. Found while provoking the divergence
  report for the phase's own `Verify:` step.
- The provoked divergence was not fabricated in an archive by hand: the same
  reference was replayed a second time against a stack whose register the first
  replay had already populated. It stops on `getItem(CACHE_TS._mainpref_)` against
  `getItem(CACHE_TS.alexander.king_preference)`, and the two `site_caller` chains
  name different site code — `preference.py:23 getMainStorePreference` against
  `user.py:153 getPreference`. It is the shape Phase 6 will read, produced by the
  very rule the bench states about every run: start from an empty register.
- An exchange the reference raced is replayed and NOT compared, and the skip is
  printed. Its recorded reply is a 400 the site owed nobody, so its register lines
  are the lines of a refused call; comparing them against the lines of the call
  the site accepted would compare two different things. Declared here because a
  silent skip is a divergence nobody can see afterwards — the same reason Phase 2
  gave for `/_ping` and the statics.
- The S section of the rules table is EMPTY and that is the finished state, not a
  gap (foreman, 2026-08-25, answering this phase's clarify). S1/S2/S3/S5 are facts
  between workers: on a legacy-vs-legacy run they produce no register line at all,
  so their signature becomes observable at the first cycle against the bridge.
  Phases 5 and 7 add each rule when its divergence shows, with the owner's
  sign-off. `structural_diff_check.py` proves the mechanism with a stand-in rule
  of its own rather than with a real one written blind.

## Phase 4

- The register recorder's install point MOVED to the engine factory, because the
  fork moved where the site is built. `GnrWsgiSite.__init__` forces
  `site.register` into existence (`gnrwsgisite.py:510`), so under the template
  recipe the register client exists in the TEMPLATE, before any worker
  constructor runs. `recording_worker.py` kept only the HTTP recorder, which
  wraps `wsgi_app` — a name the worker constructor assigns, in the child.
  `recording_engine_factory.py` is new: `RecordingSiteEngineFactory`, the
  shipped `GenropySiteEngineFactory` subclassed, so the bench forks by the
  shipped protocol and not by a bench variant.
- The bench recipe now differs from the shipped one in TWO lines, not one:
  `worker_class` and `engine_factory`. `engine_kwargs` is identical to the
  shipped `{source, debug}`, so it is not a difference. `bridge_coverage_check.py`
  licenses exactly those two and still compares the whole rendered document.
- **The template must never open a sqlite connection**, and this is the finding
  of the phase, not a precaution. The plan expected the trouble to be an
  inherited handle; it is not. Measured 2026-08-25 on the bridge interpreter
  (pyenv python 3.12.9, sqlite 3.51.0): a forked child dies of SIGSEGV as soon
  as its PARENT has ever opened a connection — a different file works the same
  way, WAL is irrelevant, and closing before the fork does not help. What
  poisons the child is the library having been initialised at all. It is also
  INTERMITTENT, two runs in three, so one green run proves nothing. The legacy
  venv carries sqlite 3.50.4 and does the same thing cleanly, which is why the
  gunicorn stack — master mints, workers forked — has never met this and needs
  no change.
- It was seen before it was understood. The first fork recipe let the template
  record: the run archive held 4 register lines written by the template's pid,
  the forked worker was killed after `its process never presented itself in
  10.0s`, no second worker came up, and the front answered 503 with
  `no room for a newcomer: the pool is restricted`.
- A lazy attach was written first and reverted: making `RunArchive` read its run
  row on first use keeps a template that only ATTACHES free of sqlite, but the
  template does not only attach — it writes, because the site makes register
  calls while it is being built. Lazy reads would have hidden the problem one
  step further in. What is in the tree instead is `TemplateArchive`, an archive
  that swallows, bound to the client through a `partial` exactly as
  `serve_legacy.py` binds the run's archive; each forked worker replaces it with
  the real one as its first act.
- What that drops, declared: the register calls the site makes while it is being
  built — 4 on the bridge, 2 on legacy. They already differed, because the two
  stacks build the site in different processes, and they carry no `exchange_id`,
  so no comparison has ever read them. Weighed and rejected: buffering them in
  the template and flushing from the first forked child (which child is "first"
  is arbitrary, and every child would flush the same lines), and a short-lived
  helper fork to do the writing (a whole process for four lines).
- Fidelity of the change, measured on the `drive_login` smoke: the fork recipe
  records **380 register lines carrying an exchange**, exactly what the four
  previous spawn-recipe runs recorded, over **the same 52 distinct `site_caller`
  chains** (0 unique to either recipe). 0 lines without a caller, 0 without an
  exchange, 0 unjoinable, and every register line written by the forked worker's
  pid — none by the template's.
- Owner's decisions on the above, 2026-08-25, relayed through the foreman:
  - the loss of the four template-build register calls is ACCEPTED; no helper
    process is to be built to keep them;
  - the no-write template is the RIGHT DESIGN independently of the sqlite bug —
    a template that hands its children no archive state is what is wanted — so a
    future interpreter upgrade does not revert it;
  - upgrading the bridge interpreter to a sqlite 3.53.x build is macro-phase
    boundary hygiene, NEVER mid-workflow. Phases 5 and 7 do not attempt it: a
    change of interpreter changes what every comparison measures, and the bench
    is mid-comparison.

## Phase 5

- The refusal is asked of the PAIR and only ACROSS stacks, and the plan's first
  wording was broader than that. Taken literally — the target's dbname must
  always differ from the reference's — it would have refused the Phase 3
  self-check, which is legacy replayed against legacy on the same
  `test_invoice_pg` and is the one run that proves the comparison and the
  identifier adaptation work. The danger the rule exists for is the bridge
  writing into the db the reference was recorded on, and that is always a
  cross-stack run. Foreman amended the plan accordingly (7b1d760), with a
  forward caveat for Phase 7: a same-stack replay does share the db, harmless on
  a login-only reference, not harmless once the reference WRITES — two replays
  then start from two different db states and the difference reads as a
  divergence the stacks did not cause.
- `DatabaseSeparation` reads the two archives' `conditions` and nothing else. No
  new field, no new record shape: `stack` and `database.dbname` have been in the
  run row since macro-phase 1, which is why the gate cost no format change. The
  name is mine, per the owner's delegation of naming inside this bench.
- The copy CANNOT live in `replica.py`, and this is the fact behind the foreman's
  answer rather than a preference: by the time the replay starts, the bridge
  already holds connections on the copy db, and `createdb -T test_invoice_pg`
  needs its template free as well. So the copy is a recipe step run by hand
  before the launcher, like every other step of this bench, and what
  `replica.py` gained at cycle start is the refusal.
- A readiness probe is an exchange. Waiting for the bridge with a `GET /` on the
  site put one line in the run archive that no reference asked for — harmless,
  because the replay anchors itself to the archive at start and joins by
  `X-Bench-Replica-Of`, but it is a line nobody wrote on purpose. The README now
  says to wait on the launcher's LOG (`Application startup complete`).
- The first cycle stopped inside the FIRST exchange, not at the login. Reference
  `legacy-20260825T085605` against `bridge-20260825T113535`: exchange 1 answered
  200 and register call 5, `client:new_connection`, answers a connection register
  item whose key set differs — reference `datachanges`, `datachanges_idx`,
  `electron_static`, `register_name`, `subscribed_paths`; bridge `avatar_extra`,
  `last_refresh_ts`, `last_rpc_ts`, `last_user_ts`, `store`. Both `site_caller`
  chains name the same site code (`gnrwebpage.py:325 _register_new_page`), so the
  two stacks reach that call the same way and answer it differently. Phase 3's
  own notes had already seen three of those keys as a shape difference on the
  2026-08-23 archive; this is the first time the comparison stops on it. Whether
  it is a rule to declare or a fault to fix is the owner's judgment — Phase 6's
  starting point, and it means Phase 6 does not open on the login segment the
  plan expected.
- The naming review found NOTHING to review, and the reason is on record rather
  than an omission: no `wf:phase-5:new` marker was written on the new callables
  (a miss against the marker contract, noted honestly), and naming inside
  `benchmarks/compare` is delegated to the executing session by the owner's own
  decision of 2026-08-25 — so `DatabaseSeparation` and its properties carry the
  names they were born with, with no question put to him.
- The Done gate was re-checked on the RECORDED run, not by replaying the cycle a
  second time. A second replay into the same stack starts from a populated
  register, which is the one thing every run of this bench forbids: the check
  would have measured the contamination and not the phase.

## Phase 6

The per-key decisions the Details asked for, kind by kind. The target is the
answer the legacy daemon gave, read off the Phase 5 archives and confirmed
against the daemon's own source (`gnr/web/daemon/siteregister.py`).

Added to every kind, because `BaseRegister.addRegisterItem`:135 put them on
every row the daemon handed out:

- `register_name` — the daemon seeded it on the row itself. The view knows
  which kind it is projecting, so it answers it.
- `subscribed_paths` — the page row's own set, read through the existing
  `_item_subscribed_paths`; empty off the page register, as that helper already
  answered. Cheap and lock-free.
- `datachanges` — the empty list, NOT the real queue. On this stack the queue is
  not on the row: it lives in the page's collectors, and the surface that reads
  it is `ServerStore.datachanges`, which drains them there and is the only thing
  the legacy reads changes through (`daemon/siteregister_client.py`:403 reads
  the row because on the daemon the row IS the queue). Putting the real content
  on the projected row would take `dispatch_lock` once per page inside
  `pages()`, which that read path deliberately avoids as a hot path.
- `datachanges_idx` — `0`. The bridge numbers each change and not each item
  (`change_idx: 0` on every change it builds), so there is no item counter to
  answer with.

Added per kind:

- `electron_static` on a connection — the daemon's `create` took it and the
  bridge did not carry it at all. Added to `_conn_kwargs`, so a real electron
  client's value is stored and not invented at read time.
- `user` on a page — the daemon stored it; here it is DERIVED through
  `_page_owner`. The cemented decision (ownership derived, never stored) is
  untouched: the key the legacy reads is answered, and nothing is written on the
  row to answer it.
- `subscribed_tables` on a page — the daemon's name for what the core row
  carries as `table_subscriptions`. The translation moved INTO the projection,
  so `page()` lost the bolt-on it did after the fact and `new_page` answers the
  key too, which it did not before.

Removed from the site-facing row:

- `store` — the core's name for the live Bag. `data` is the daemon-era name and
  is the same object; two names on one answer was a bridge invention.
- `last_refresh_ts`, `last_user_ts`, `last_rpc_ts` — the foreman's decision of
  2026-08-25. They stay on the core row, where the expiry sweep reads them as
  floats. The datetime dressing that carried them out is gone with them, and so
  is the `EPOCH_STAMPS` constant.
- `avatar_extra` on a connection at birth — the daemon's `create` does not take
  it; `Connection.change_user` (connection.py:169) writes it at LOGIN. So it
  left `_conn_kwargs` and the projection answers it only when the row carries
  it (`LEGACY_LATE_FIELDS`). This is the one field whose presence is
  conditional, and it has to be: the pre-login read and the post-login read of
  the same row have different key sets on the legacy too.
- everything else the core keeps for itself — the collectors, the page tree
  (`root_page_id`, `parent_page_id`), `store_subscriptions`, `dbevents`,
  `avatar_key`, `user_view`. They were never in a daemon answer.

Three things the projection surfaced that the plan did not name:

- `data` is no longer aliased onto the answer at birth and left there. The
  daemon attaches it in `get_item` when the caller asks (`include_data`) and its
  client attaches it to a lifecycle answer; the bridge was setting the alias on
  the LIVE row at birth, so every later read of that row carried `data` where
  the legacy read carried none. Now `_adapt_to_legacy` never carries it and the two
  callers that owe it — `get_item` under `include_data`, and the lifecycle
  answer — add it. The alias on the live row stays, because `get_dbenv` reads it.
- `pages()` and `connections()` were calling `_adapt_to_legacy` with no
  `register_name`. Harmless while the view was a copy; with a projection it is
  the field list, so both now pass their kind and `_adapt_to_legacy` takes it as a
  required argument.
- four contract assertions in `tests/test_register_client_units.py` asserted the
  OLD answer and were rewritten with the owner's approval, each preserving what
  it was really testing: `store` visible in the answer moved to the core row
  (`test_new_connection_is_born_guest_with_live_data_bag`,
  `test_new_page_seed_data_becomes_the_live_store`); the identity of two
  `new_connection` answers became the identity of the row and of its Bag
  (`test_new_connection_twice_answers_the_same_row`, renamed); the page's `user`
  became "answered by the view, absent from the row"; and the three clock
  assertions became their absence from the view plus their presence as floats on
  the core row, the foreman having licensed exactly that.

No `tests/x/` folder was created. The three new tests assert the exact key set
each kind answers, which is behavioural continuity and not a photograph of an
implementation, so they went into `tests/` beside the other legacy-row contract
tests. Nothing this phase built needed an implementation test.

The rename the owner baptised on 2026-08-25 landed here: `_legacy_row` became
`_adapt_to_legacy(register_item)`, and with it the vocabulary of every docstring
and comment the method touches — `row` out, `register item` in, and the three
fields called CLOCKS, the name the core itself uses (`CLOCK_NAMES`,
spa_worker.py:293). The local variable is `adapted`. The owner baptised the one constant that survived,
`LEGACY_REGISTER_ITEM_FIELDS`, and chose the dict-of-three-kinds form over three
separate constants (`LEGACY_USER_...`, `LEGACY_CONNECTION_...`,
`LEGACY_PAGE_...`), which he weighed aloud.

The Done gate was re-measured AFTER the rename, on a fresh cycle: the archived
run `bridge-20260825T141541` was produced by the pre-rename code, and evidence
that does not come from the code that stays is not evidence. New copy db, new
bridge, archive `bridge-20260825T142217`: same stop, same call 15, and the two
key sets identical to the legacy reference — `new_connection` 17 keys,
`new_page` 14.

The second constant is gone, and with it the two classes of field. The owner's
rule (2026-08-25): keep the fields in one structure, and a field the register
item does not carry is not put in the answer. Applied literally to every field
it would have dropped `start_ts` and `user_name` from a user register item —
measured absent from the core item, and read there WITHOUT a guard by
`connected_users_bag` — so what changed instead is the COMPARISON: two answers
that differ only by a key carrying None are semantically identical (his words),
so `structural_diff.py` now drops null-valued keys from the shape it compares
(`DICT_NULL_KEY`). `avatar_extra` then went into the single field list and is
answered always, None until the login writes it, which is what the daemon's own
answer said in substance.

That rule change is a deviation from this phase's `Files:` — it touches
`benchmarks/compare/structural_diff.py`, the Phase 3 tool — authorised by the
owner in the phase chat and reported to the foreman.

And it earned its keep immediately: with null keys out of the shape, the replay
stopped at register call 6 on a difference the old rule had hidden — the page
register item carried `relative_url` on the legacy and None on the bridge.
`relative_url` is not an attribute of the WebPage at all: the daemon client read
it off the request (`daemon/siteregister_client.py`:220) and the bridge was
reading it as an attribute, so it answered None on every page ever registered.
Same class of defect as the missing `electron_static`, found by the sharper
instrument, fixed in `_page_kwargs`.

Final measurement, archive `bridge-20260825T144852` against reference
`legacy-20260825T085605`: no register item key set divergence anywhere; the
replay stops at register call 15 on the recorder's `surface` field —
`client:get_dbenv` on the bridge, `passthrough:get_dbenv` on the legacy, with
identical arguments, answer and site caller chain. That field is `client` when
the register client class declares the method and `passthrough` when
`__getattr__` reaches it (`register_recorder.py`:221): the legacy client
declares no `get_dbenv`, the bridge declares every command explicitly by design.
A candidate declared rule for Phase 7, not a defect.

## Phase 7

**The reference was re-recorded first.** The pinned genropy moved to `a1c0a8dd0`
while this phase was at its gate, so every archive recorded before 2026-08-25
15:13 belongs to `6da02feda`. New reference `legacy-20260825T151337`, same
`drive_login`, 4 exchanges: **152 register lines**, where the old one had 384.
The services freshness chain fell from 242 calls to 10 — #1154 skips the register
read for a service whose configuration did not come from the db. The figure
macro-phase 3 will want is 152 per login, measured, not the ~145 estimated.

**Divergence 1 — the recorder's `surface` field. Closed: the instrument.**
The replay stopped at register call 15 on `client:get_dbenv` against
`passthrough:get_dbenv`, with identical arguments, answer and site caller. The
field says how the recorder reached the method inside the register client:
`client` when the class declares it, `passthrough` when `__getattr__` does. The
legacy client hands most of its surface to `__getattr__`; the bridge declares
every command explicitly, by architectural choice. The site cannot tell them
apart, so the difference was the instrument's, not the stacks'. `LineShape.call`
now reads both as `client`, and `store` — another object's surface, the live
Bag's — stays distinct. Reported as `client` in the report too, so a line does
not print a difference the comparison did not make. Foreman decision of
2026-08-25, on the reasoning above; it is the same class as Phase 1's
module-name cut.

**Divergence 2 — the cold start. Closed: a comparison rule, the owner's.**
The replay stopped at register call 19 of the first exchange: the legacy makes a
freshness check for `storage_gnr` that the bridge does not — two lines, and in a
run of identical `globalStore()`/`getItem()` calls difflib cannot tell WHERE the
two are missing, so it charged them at the end and scrambled five following
pairs into spurious `arguments` divergences. Measured with the template made to
print what it swallows: the bridge DOES make that check, in the TEMPLATE its
workers fork from, whose register lines are dropped by construction (a template
that touches sqlite kills the children it forks — `recording_engine_factory.py`).
The four template lines are the `_mainpref_` read the legacy master makes too,
plus exactly the `storage_gnr` pair. So the two stacks make the same call and
only one of them is in an archive.

The owner's rule (2026-08-25): the comparison reads no register line from the
exchanges BEFORE the first RPC. Each stack finishes building lazily there, and
in a different process. `TraceReader.cold_start_exchanges` computes them from
the archive — no state in any recorder, so no per-worker flag that a second
bridge worker would not have — and `Replica.compare_exchange` prints the skip.
A run with no RPC at all has no cold start: the rule must never be able to
silence a whole comparison. The exchanges are still REPLAYED, the page the RPCs
need is created there. What it costs: the connection register item is not
compared at BIRTH; it is compared from the first login call on, with the same
key set (`connection` in exchanges 2, 3 and 4).

**Divergence 3 — the live object a store hands out. Closed: the bridge.**
`get_dbenv` answered a `workdate` node the legacy never carried.
`WebPage._get_workdate` (`gnrwebpage.py:541`) reads `rootenv` from the page store
and then assigns `rootenv['workdate']` INTO the object it read. On the legacy that
object had crossed the wire, so the write died with the request; in-process the
store handed out the live Bag and the write landed in the register item.

The owner ruled the fix, not a rule (2026-08-25): a store read hands the site a
copy. The first copy was not enough — `Bag.deepcopy` keeps a node's non-Bag value
by reference — and the next stop proved it, on the write side this time:
`GnrApp.getAvatar` (`gnrapp.py:1468`) POPS `user_id`, `user_name` and `tags` out
of the dict `tableCachedData` has just written into the page store. Evidence from
the archives: the same cache line, read again, still carries the three keys on the
legacy and carries none of them on the bridge — so the bridge's avatar fell back
to the username for all three (`user_id=alexander.king`,
`user_name=alexander.king`, `user_tags=user,alexander.king`).

So `ServerStore` copies in BOTH directions, which is what the wire did: `getItem`
answers a copy, `setItem` stores a copy, and `_copied` rebuilds a Bag node by node
and deep-copies a mutable value under a node. Everything a legacy store held had
crossed a pickle, so anything in there is copyable.

**Divergence 4 — the answer of `change_connection_user`. Closed: the bridge.**
The daemon's method (`daemon/siteregister.py`:778) closes without a `return`; the
bridge answered the updated connection item. No site code reads it —
`Connection.change_user` calls and ignores. One contract test did
(`test_login_stays_pages_keep_their_worker`), and the owner ruled as he ruled in
Phase 6: translating towards the legacy format is genropy-asgi's job, so the test
asserted the wrong thing. The assertion is preserved by reading the item back
with `client.connection(cid)`, one line below.

**The `+28%` did not appear, and cannot be decomposed.** The record of
2026-08-24 (`temp/documento_banco_2026-08-24.md`) measured 147 register calls on
the bridge against 115 on the legacy over the three login exchanges. The same
document names why it stayed open: the register line did not say who called, and
that field (`site_caller`) is Phase 1's. So the old figure was taken with an
instrument that could not attribute a single call, and no decomposition of it is
possible after the fact. Re-measured on the closing cycle, the counts are equal
where the comparison reads them: exchange 2 33/33, exchange 3 35/35, exchange 4
43/43; exchange 1 is 39/37, the two template lines, and is not compared. The
figure was taken on a browser session, not on `drive_login`, so Phase 8 is where
it would reappear if anything of it survives.

**The closing cycle.** Reference `legacy-20260825T151337` replayed against
`bridge-20260825T164102`: four exchanges, every one answering the status the
trace carries, no divergence left unexplained, nothing recognised by a declared
rule. The table still holds `reference-race` alone — this phase added no rule to
it, which is the outcome the owner chose each time: every stop was a defect, and
every defect was fixed.

**The report was widened at the owner's check (2026-08-25).** Reading it he
asked for what it did not carry: which run, under which parameters, and how long
it took. Added, all of it read from the archives themselves so the report stays
readable months later beside the two files it names — the instance and the
database of each side, the exchange count of the reference, the duration EACH STACK
recorded for each exchange, and a closing summary
with the exchanges compared, the ones not compared and why, and the register-call
count of each compared exchange on both sides. The first version printed the replay's own wall clock beside the reference's
recorded duration, and the owner rejected the pair: two different measurement
points invite a comparison that does not hold. It is now the same metre on both
sides — the HTTP recorder wrapping the application, in the process that served
the request, network excluded — which holds because the replay sends the same
call, with only the identifiers the target mints rewritten. Still not a
benchmark, and the report says so: both stacks run under two recorders, and
timings belong to macro-phase 3 under its own declared conditions. The wall clock
survives in one place only, the closing line, as how long the cycle took.

Then the owner said what he was actually after and was still not seeing: the
comparison of the response times, legacy against genropy-asgi. Two numbers on a
line are not a comparison. The report now closes on one — every exchange the
replay performed, the two durations, the signed percentage, and the total — with
the stacks named from the conditions each archive declares rather than called
"reference" and "replica". The cold-start exchange is in it: its register lines
are not compared, but a response time is a response time. Measured on the closing
cycle: 622 ms on the legacy against 447 on the bridge over the four exchanges,
-28%.
