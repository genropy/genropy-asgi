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
- Open, waiting on the owner: a site resource loaded under a flat module name —
  genropy's `projects/gnrcore/packages/adm/model/preference.py` — comes out as
  `preference.py:23`, the directory lost, because the dotted name has no dots to
  count. It is the same string on both stacks, so it produces no divergence, but
  it does not locate the file. The absolute path is NOT the fix: the two stacks
  read that same resource from different roots (frozen copy vs editable), so it
  would read as a divergence in the report.
