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
