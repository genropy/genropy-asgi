# Tool map

This map distinguishes maintained modules from historical evidence. Removed tools
and Git recovery instructions are listed in [retired tools](retired-tools.md).

| Purpose | Existing tools | Status |
|---|---|---|
| Recording foundation | `recording/run_archive.py`, `recording/http_recorder.py`, `recording/register_recorder.py` | Canonical modules, with compatibility imports in `compare/`; isolated recording checks pass. |
| HTTP capture | `capture_proxy.py`, `capture_loadrecord.py` | Earlier capture tools; inspect captured forms before export. |
| Recorded-session replay | `replay/replica.py` | Canonical replay with target identity mapping; compatibility entry point in `compare/replica.py`. |
| Earlier session replay | `replay_a1.py`, `session_bench.py` | Historical HTTP workload transformation, not a browser-gesture recorder. |
| HTTP transport | `execution/http_client.py` | Shared standard-library client, independent of GenroPy and orchestration. |
| Source parity | `execution/genropy_parity.py` | GenroPy-specific preflight checks; compatibility entry point in `compare/`. |
| Trace comparison | `analysis/structural_diff.py` | GenroPy-specific register shapes and declared rules; compatibility import in `compare/`. |
| HTTP/register parity | `compare/` recorders, replica, structural diff, twin proxy | Behavioral comparison; heavy recorder timings are not performance baselines. Launch imports need review. |
| Framework/RPC diagnostics | `floor_bench.py`, `ping_ramp.py`, `single_record_bench.py`, `capacity_bench*.py` | Small workload probes with different scopes. |
| Earlier elastic scenarios | `scenarios.py`, `load_harness.py`, `run_grid.py`, `cost_model_ramp.py` | Removed; superseded configuration/monitor surfaces. `scaling_probe.py` remains for retained callers. |
| Population churn | `churn_driver.py`, `churn_report.py` | Root driver is untracked; exact contents preserved. |
| Shared execution support | `execution/load_engine.py`, `measurement/`; source `bench_common/` for shell lifecycle | Python load and observation tools migrated with checks; shared shell lifecycle migrated for the fixed population cycle. |
| Fixed worker sensitivity | Dedicated/L120 worktrees: `worker_sensitivity/` | Root untracked copy is older; identical names do not mean identical tools. |
| L120 comparison | L120 worktree: `l120_comparison/` | Same offered-load plan, synthetic indexed RPC. |
| Population/freeze | L120 worktree: `population_freeze/` | Authenticated population, working set, freeze/thaw. |
| Activity cycle | `scenarios/eight_core_cycle/` | Fixed/dynamic plans, recipes, drivers and Docker runners migrated; new live campaigns deferred. |
| CPU offload/capacity | L120 worktree: `cpu_offload/` | Untracked tools and local guard changes preserved; recipe adaptation required. |
| Daemon-era probes | `gil_ramp.py`, `sr_counter.py`, `wire_counter.py`, `gunicorn_*.conf.py` | Removed; source recoverable from Git, methodology retained in historical notes. |

Worktree locations relative to the repository:

- `worktrees/l120-population-comparison/benchmarks/`
- `worktrees/worker-sensitivity/benchmarks/`

The preservation import contains benchmark files from every available worktree,
branch/commit/status metadata, tracked binary diffs and a Git bundle. The file
snapshots include untracked tools, which a Git bundle alone cannot preserve.
Choose an authoritative revision before any integration; do not merge similar
folders by copying one over another.

Use the [catalogue](../catalogue/README.md) for the detailed audit and exact
preserved locations.
