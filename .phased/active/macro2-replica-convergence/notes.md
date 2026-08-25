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
