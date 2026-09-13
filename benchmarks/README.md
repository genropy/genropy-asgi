# Benchmark laboratory

The benchmark tools are being reorganized incrementally. Recording and replay foundations now live in the new
directories, with HTTP transport and GenroPy parity checks in `execution/`,
and structural comparison in `analysis/`. The first population cycle now lives
in `scenarios/eight_core_cycle/`, with its load engine in `execution/` and
samplers and guards in `measurement/`. Existing entry points remain usable through
compatibility imports while their dependencies are migrated.

## Target code layout

| Directory | Responsibility |
|---|---|
| `recording/` | Capture actions, requests and the context needed to reproduce them. |
| `replay/` | Replay recorded sequences, including session state, identity mapping and dependencies. |
| `scenarios/` | Define user journeys, concurrency, pacing, churn and load phases independently of the target environment. |
| `execution/` | Prepare environments and run scenarios locally or on Hetzner, against ASGI or legacy/Gunicorn. |
| `measurement/` | Collect request timings, errors, actual load, resources and orchestration events. |
| `analysis/` | Validate measurements and generate comparable summaries, tables, charts and reports. |

These are responsibility boundaries, not a new public API. The populated directories are repository-local Python packages,
excluded from the release wheel.
`scenarios/` is the maintained scenario package. Superseded scripts have been
removed; see [retired tools and recovery](docs/retired-tools.md).

Migrate one working component at a time: identify its authoritative source
(including development worktrees), inspect its dependencies, move and clean it,
update its callers and instructions, then verify its behavior. Remove the old
copy only after its replacement works. Extract shared code when a concrete need
appears rather than creating a generic utilities directory in advance.

The first integration target is recording, replay, a concurrent scenario,
execution against ASGI and Gunicorn, measurement and a comparison report.
Recorded datasets and generated outputs belong in the external data archive;
these new directories are for code and reusable scenario definitions.
Five historical reference runs are saved in PostgreSQL and the private object
archive; see [the import record](docs/historical-import-20260909.md). Further bulk
imports, new scenarios and presentation work are deferred.

### Migration status

`recording/run_archive.py`, `recording/http_recorder.py` and
`recording/register_recorder.py` are the canonical implementations. Their internal
archive imports are package-relative. The corresponding files in `compare/`
alias these modules, preserving class identity and module-level overrides for
existing launchers and checks. There is only one implementation of each recorder.
The SQLite format, filtering rules and recording behavior are unchanged.

The bridge mixin, engine factory and recording worker now live in `recording/`;
`execution/serve_bridge.py` launches them through `execution/bridge_recipe.py`.
Compatibility files remain in `compare/`. The recipe uses the current SPA module
paths and preserves the shipped CPU retirement setting and optional profile
application. The full-document equivalence check allows only the two recording
class substitutions.

Validated before and after this migration with the laboratory interpreter:

```bash
temp/legacy_venv/bin/python benchmarks/compare/run_archive_check.py
temp/legacy_venv/bin/python benchmarks/compare/http_recorder_check.py
temp/legacy_venv/bin/python benchmarks/compare/register_recorder_check.py
```

These checks use disposable archives under `temp/`, not collected results. They
cover SQLite attachment and forked writers, HTTP body preservation and filtering,
recorder failure isolation, register calls, retries and caller attribution.

### Replay and reusable transport

| Canonical module | Responsibility | Dependency boundary |
|---|---|---|
| `execution/http_client.py` | Persistent HTTP connection and cookie handling (`StickyClient`) | Standard library only; no GenroPy, daemon or orchestration imports. |
| `execution/genropy_parity.py` | Verify pinned and installed GenroPy sources | GenroPy-specific; imports `gnr` only when resolving the runtime source. |
| `replay/replica.py` | Read recordings and replay with target identities and correlation headers | GenroPy replay semantics, including page identifiers and declared skips. |
| `analysis/structural_diff.py` | Compare register traces and explain divergences | GenroPy-specific shapes and comparison rules. |

The historical comparison imports and command paths remain compatibility entry
points. `scaling_probe.py` now imports the shared HTTP client; replay no longer
imports the old scaling probe or `replay_a1.py`. New commands run as modules from
this worktree's root:

```bash
python -m benchmarks.replay.replica --help
python -m benchmarks.execution.genropy_parity
```

The parity command requires the configured pinned GenroPy installation and
legacy environment. This worktree does not automatically inherit the main
checkout's `temp/legacy_venv` or `temp/gnr`. Its help and pure replay imports can
run without GenroPy installed; actual comparisons retain the parity refusal.

Verification includes the historical replica and structural-diff checks, plus
`tests/test_benchmark_replay.py`: a real local HTTP recording replayed against a
second target, verifying new page identifiers and cookies, repeated form fields,
opaque request bodies and exchange correlation. This is a transport/recording
contract, not validation of the current bridge deployment or its daemon parity.

The twin-proxy isolation check now suppresses live consumer startup only while
checking browser identity allocation, and checks the expected startup count.
Its fixture has no launched stacks; this does not validate a live twin session.
Create a disposable report directory and set `GNR_BENCH_ARCHIVE_DIR` when running
it, so reports cannot be written into the collected archive.

For the later extraction into `genro-asgi`, keep generic transport and measurement
code independent of GenroPy. Page/login/register semantics and Gunicorn comparison
remain bridge concerns. Do not move a module into the core merely because it is
in `execution/` or `analysis/` here.

### Recorded bridge integration

The bridge-specific mixin, engine factory and worker have moved to `recording/`.
The canonical launcher and recipe are `execution/serve_bridge.py` and
`execution/bridge_recipe.py`; historical paths still work. The recipe now uses
the current SPA module paths and includes the shipped retirement setting and
optional profile application. The recipe remains an explicit copy guarded by a
whole-document comparison, allowing only the two recording class substitutions.

Run the isolated bridge checks with this worktree's source on the import path:

```bash
GNR_DAEMON_PROVIDER=genropy-asgi PYTHONPATH=src:benchmarks/compare \
    python benchmarks/compare/bridge_coverage_check.py
```

Run the opt-in live contract against a configured demo site:

```bash
GNR_BENCH_SMOKE_SITE=test_invoice_pg PYTHONPATH=src \
    python -m pytest tests/test_benchmark_bridge.py --no-cov -q
```

The test starts the real recorded CLI on a free local port, serves a page and
ping through its template-born worker, and checks the archive has correlated
HTTP/register records without recorder errors. It isolates archives and socket
paths and terminates the process group on exit. This validates basic integration;
it does not establish login parity, cross-worker datachange delivery or load
capacity. Those remain work for the scenario consolidation.

## Population-cycle migration

See [the cycle guide](scenarios/eight_core_cycle/README.md) for the migrated
scenario, fixed Docker runner, verification and remaining live-laboratory work. Source provenance is
recorded in `docs/scenario-code-sources.json`. The source worktree remains intact.
The new load engine corrects in-flight accounting, serialized CSV writes and
chronological delay drift; these corrections apply only to future runs.

## Existing tools and evidence

The [evidence catalogue](catalogue/README.md) covers local and Hetzner
experiments, including incomplete and invalid attempts, with paths to preserved
data. Historical results are not measurements of the current bridge release.

| Purpose | Location |
|---|---|
| Collected results and comparisons | [Campaign catalogue](catalogue/README.md) |
| Preservation, recovery and old paths | [Data storage](docs/data-storage.md) |
| Tools grouped by purpose | [Tool map](docs/tool-map.md) |
| HTTP/register recording and replica | [Comparison bench](compare/README.md) |
| Existing Docker laboratory | [Docker guide](docker/README.md) |
| Original microbenchmark instructions | [Measurement guide](docs/measurement-guide.md) |

## Permanent local data

`~/genro_bench/genropy-asgi/` is outside the repository and its temporary tree:

```text
genropy-asgi/
├── README.md          # local archive entry point
├── campaigns/         # original campaigns, including failed attempts
├── local-runtime/     # former Docker runtime results
├── imports/           # verified context, worktree tools, recordings and Git
├── backups/           # separately verified compressed snapshot
└── relocation.json    # exact old-to-new path map
```

Original campaign paths under `temp/` and `docker/runtime/` are compatibility
symlinks. Removing a symlink does not remove its target; following it and
modifying its contents does. Raw recordings, fixtures and credentials stay out
of public Git. The catalogue and documentation belong in Git.

## Execution tools

Maintained entry points and compatibility imports are preserved. Superseded
daemon and elastic-pool scripts are listed in the retirement record. Later scenarios remain in their development worktrees; exact snapshots of
uncommitted benchmark files have also been preserved.

Run the original root-level scripts **from `benchmarks/`**, not from `docs/`.
Every new measurement should have a fresh output directory. Preserved historical
names are not reusable run identifiers.
