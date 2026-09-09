# Population cycle

Migrated from the L120 comparison worktree. This scenario admits users, measures
full activity, pauses part of the population and measures their return. A fixed
plan supplies the same per-user schedule to legacy/Gunicorn and the bridge.

## Code boundaries

- `make_cycle_trace.py`: deterministic plan generation.
- `cycle_probe.py`: one stack's population and phase driver.
- `admission_guard.py`: stop admitting users after sustained latency pressure.
- `certify_dynamic_cap.py`: verify the dynamic topology's settings.
- `../../execution/load_engine.py`: paced requests and call/window accounting.
- `../../measurement/`: memory guard, process sampling, bridge observations and
  GenroPy page-class cache checks.
- `../../replay/genropy_session.py`: GenroPy login, indexed selection and logout.

The latter is a bridge adapter. Process classification, cache certification and
bridge observations are also specific integrations; their placement beside the
sampler does not make them generic core features.

## Verification

From the worktree root:

```bash
python -m benchmarks.scenarios.eight_core_cycle.checks
python -m benchmarks.measurement.checks.stop_guard_check
python -m benchmarks.measurement.checks.container_probe_check
python -m benchmarks.measurement.checks.page_class_cache_check
python -m pytest tests/test_benchmark_load_engine.py --no-cov -q
```

The four existing suites cover 395 checks. Two additional contracts demonstrate
that an empty queue can still have a request in flight and that a decreasing
start delay must produce negative drift even when calls complete out of order.
Both failed on the imported engine and pass after the corrections.

Generate a plan or inspect the driver:

```bash
python -m benchmarks.scenarios.eight_core_cycle.make_cycle_trace \
    --out temp/cycle_plan.json --seed 20260831
python -m benchmarks.scenarios.eight_core_cycle.cycle_probe --help
```

The default plan was compared byte-for-byte with the source generator:
`376ea43df7141f77d631d97126a38bd0ef8fa58427b930b3704d966458d96bdd`.
The generator still reads the existing capture and account fixtures; replacing
those fixtures and curating historical results are separate work.

## Accounting corrections for new runs

Pending work now includes requests already taken off the queue but still waiting
for their response. A drain timeout invalidates the phase instead of closing a
partial measurement as complete. CSV writes share the accounting lock.

Delay percentiles sort by magnitude. Delay drift instead compares the two halves
of the calls in scheduled order. The historical implementation sorted delays
before splitting them, which could turn improving delays into positive drift.
Historical files have not been recalculated.

The legacy `completed` field counts terminated logical calls, including transport
failures; `responses_received` separately counts calls that returned an HTTP
response. The original retry policy remains in place and must be reviewed before
using this engine for workloads with writes.

## Docker runner

`run_cycle.sh` and `run_smoke.sh` now use the shared lifecycle in
`../../execution/lab_lifecycle.sh`. The fixed-topology recipe and Compose
overrides live beside this scenario. The Docker bridge entrypoint honors
`GNR_ASGI_POOL_RECIPE` while preserving dependency resolution for the mounted
core and bridge packages.

Generate certified plans once, outside the repository:

```bash
python -m benchmarks.execution.make_plans benchmarks/scenarios/eight_core_cycle \
    --out "$HOME/genro_bench/genropy-asgi/plans/eight_core_cycle"
```

The generator takes argument lists from `plans.spec.json`, verifies existing
plans without regenerating them, and publishes new plans only after their hashes
match. A mismatch is an error and produces no new certificate.

On the configured Linux laboratory, run the reduced fixed-topology campaign:

```bash
LAB_DIR=/path/to/configured/lab \
    bash benchmarks/scenarios/eight_core_cycle/run_smoke.sh legacy,bridge campaign_name
```

`LAB_DIR` must contain the Compose laboratory and its `.env`, including the
mounted source trees. Point `GENROPY_ASGI_TREE` at the intended worktree.
`PLAN_DIR` selects the certified-plan directory; `WORK_DIR` selects a fresh output
directory. Defaults are `~/genro_bench/genropy-asgi/plans/eight_core_cycle` and
`~/genro_bench/genropy-asgi/campaigns/<prefix>`. CPU and memory limits are passed
to both Compose services, not merely printed in the log. Keep each campaign's
prefix unique, including its journal files in the laboratory runtime directory.

The fixed recipe uses the current core's CPU admission/offload controls, both
disabled, and `worker_memory_admission_percent`. Removed growth/reservation
settings are not passed to the core. The driver requires each live setting to be
present, so a missing disabled setting cannot masquerade as an explicit null.
The request plan is unchanged, but the orchestration implementation and settings
are current: this is not a recreation of the historical software environment.

## Validation and remaining work

Validated: full/smoke plan hashes; current-core fixed recipe at two and fifteen
users per worker; Compose resource limits and recipe selection; shared lifecycle
checks; and the Linux runner's failure/cleanup ordering inside an isolated Docker
container. The runner test substitutes Docker commands and readiness delays; it
confirms the second leg does not start after the first fails and the other stack
is stopped before a leg starts. It does not measure either stack.

The dynamic runner is also migrated. Its Linux sequencing test remains to be
run: the local Docker daemon timed out during verification. `run_profiles.sh` runs `legacy_w12`,
`bridge_dynamic`, and `legacy_w16` sequentially. Its explicit laboratory policy
uses CPU admission thresholds 50/30, disables CPU offload, and leaves user count
uncapped. A sixteen-worker ceiling remains a measured constraint. This profile
covers population admission and return; it does not establish coverage of every
restart, offload, freeze or worker-failure scenario.

No new real two-stack Docker campaign has been run. The worktree has no configured
laboratory `.env` or prepared database. The recorded bridge integration smoke is
separate from a complete ASGI/Gunicorn campaign.

## Offline comparison report

Select run prefixes explicitly (the part before `_outcome.json`):

```bash
python -m benchmarks.analysis.cycle_report \
    /archive/campaign/run_legacy /archive/campaign/run_bridge \
    --out /archive/campaign/comparison.html
```

The standalone HTML contains completion status, plan hashes, per-phase tables,
latency/start-delay charts and input file hashes. Missing metrics remain missing;
incomplete runs remain visible. Matching plans do not certify matching machines,
software or database state. Percentiles are never averaged across windows. The
command refuses to overwrite an existing report and does not import or modify
historical archives. Resource time-series charts and historical result selection
belong to the later data work.

Source paths, commit and pre-migration hashes are recorded in
`../../docs/scenario-code-sources.json`. Only selected code was imported;
collected results and source worktrees were not modified.
