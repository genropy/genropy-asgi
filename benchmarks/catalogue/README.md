# Evidence catalogue

Updated 2026-09-08. Storage root: `~/genro_bench/genropy-asgi/`.

Open the `README.md` at that root for clickable links to the local files. Paths below are relative to it. These 39 entries identify evidence locations, not 39 independent runs. Failed attempts and superseded measurements remain part of the record.

## Early local and Hetzner evidence

| Evidence and location | Assessment |
|---|---|
| **Historical first implementation**<br>`campaigns/benchmark/README.md` | Historical tools; README references assets that are absent/empty here; current CLI incompatible. |
| **Local indexed-record + elasticity**<br>`imports/2026-09-08-benchmark-preservation/legacy-layout/temp/benchmark_legacy_vs_bridge_2026-08-26.md` | Native macOS, single worker and elastic population; short levels and Pyro port exhaustion. |
| **Local bottleneck diagnosis**<br>`imports/2026-09-08-benchmark-preservation/legacy-layout/temp/handoff_analisi_colli_2026-08-27.md` | Diagnostic ladder; historical core and temporary diagnostic tools. |
| **Local Docker raw runs**<br>`local-runtime/2026-09-08` | Loose raw CSVs and HTML; no single run catalogue. |
| **Hetzner early raw runs**<br>`local-runtime/2026-09-08/hetzner` | Multiple levels and churn; do not assume identical contents to exported report copies. |
| **Hetzner HTML report**<br>`campaigns/report_hetzner_2026-08-27/index.html` | Report snapshot, subset of results; original report sources retained separately. |

## Policy and capacity progression

| Evidence and location | Assessment |
|---|---|
| **CPU policy first evaluation**<br>`campaigns/report_cpu_policy_2026-08-28/README.md` | NO-GO on initial policy; evolving document contains old infrastructure status. |
| **CPU soft admission**<br>`campaigns/report_cpu_policy_2026-08-28/pulse_soft_admission/README.md` | Functional PASS; tuning issues remain, one execution. |
| **CPU rearm30**<br>`campaigns/report_cpu_policy_2026-08-28/pulse_rearm30/README.md` | Progressive growth works; retirement still causes close/grow cycles. |
| **CPU quiet60**<br>`campaigns/report_cpu_policy_2026-08-28/pulse_quiet60/README.md` | Functional PASS; paired Type1 and churn. |
| **CPU quiet60 repeats**<br>`campaigns/report_cpu_policy_2026-08-28/pulse_quiet60/varianza/README.md` | Four balanced pairs, one exploratory; descriptive variation only. |
| **Capacity P1/P2 initial**<br>`campaigns/campagna_2026-08-28/runtime` | Early summary/raw files; historical protocol. |
| **P1 analysis presentation**<br>`campaigns/p1_analisi` | Copies of P1 data and analysis HTML. |
| **Cgroup-accounting run**<br>`campaigns/campagna_2026-08-29/runtime` | Intermediate accounting experiment; superseded by PSS correction. |
| **PSS accounting**<br>`campaigns/campagna_2026-08-29/p2_pss/README.md` | Accounting fix confirmed; structural policy problem remains. |
| **Memory-only restart**<br>`campaigns/campagna_2026-08-29/p2_restart/README.md` | Structural PASS, memory safety stop; not an uncensored capacity result. |
| **Decision journal**<br>`campaigns/campagna_2026-08-29/p2_journal/README.md` | Sufficient diagnosis of 29 unused workers; memory-censored run. |
| **Demand-driven growth**<br>`campaigns/campagna_2026-08-29/p2_demand/README.md` | Functional PASS, zero unused workers; memory/capacity still limited. |
| **Population200 pilot**<br>`campaigns/campagna_2026-08-29/p3_pilot/README.md` | Failed guard and cleanup; failure evidence, no capacity claim. |

## Load, population and stack comparisons

| Evidence and location | Assessment |
|---|---|
| **Failed sensitivity archive**<br>`campaigns/worker_sensitivity_hz2_2026-08-31` | Earlier attempt; imported-source certification problem, separate from valid campaign. |
| **Sensitivity smoke**<br>`campaigns/worker_sensitivity_smoke_hetzner_2026-08-31` | Certification and smoke artifacts precede valid sensitivity. |
| **Sensitivity W1/W2/W4/W8**<br>`campaigns/worker_sensitivity_hetzner_2026-08-31/RISULTATI.md` | Four structural runs; historical generator verdicts require interpretation. |
| **L120 first attempt**<br>`campaigns/l120_hetzner_2026-08-31/README.md` | Invalid; bridge pool failed before comparison. |
| **L120 corrected attempt**<br>`campaigns/l120_hetzner_2026-08-31/run2/README.md` | Complete paired run; growing dispatch delay, corrected legacy process roles. |
| **Eight-core first smoke**<br>`campaigns/e8c_smoke_hetzner_2026-08-31/README.md` | False success: missing legacy phases; full experiment not executed. |
| **Eight-core second smoke**<br>`campaigns/e8c_smoke2_hetzner_2026-08-31/README.md` | Invalid: retry loop did not retry. |
| **Eight-core third smoke**<br>`campaigns/e8c_smoke3_hetzner_2026-08-31/README.md` | Protocol completed but rejected by additional criterion; no full pair. |
| **Eight-core fourth smoke**<br>`campaigns/e8c_smoke4_hetzner_2026-08-31` | Successful smoke reported by subsequent full-run report. |
| **Eight-core first full**<br>`campaigns/e8c_full_hetzner_2026-08-31/README.md` | Legacy login failure; bridge not run. |
| **Eight-core second full**<br>`campaigns/e8c_full2_hetzner_2026-08-31/README.md` | Complete W8 pair with different latency/resource advantages. |
| **Eight-core extra profiles first**<br>`campaigns/e8c_profiles_hetzner_2026-08-31/README.md` | W12 valid; dynamic blocked before measurement; W16 not run. |
| **Eight-core extra profiles second**<br>`campaigns/e8c_profiles2_hetzner_2026-08-31/README.md` | Dynamic and W16 complete; five-profile comparison reuses earlier valid W8/W12 runs. |
| **Population2000 pilot**<br>`campaigns/pop2000_pilot_hetzner_2026-08-31` | Pilot artifacts, no README; do not treat as another full comparison. |
| **Population2000 complete**<br>`campaigns/pop2000_hetzner_2026-08-31/README.md` | Freeze/ramp comparison; stable-level wording needs reconciliation. |
| **Capacity current-to-campaign core**<br>`campaigns/capacity_hetzner_2026-09-01/README.md` | One valid paired threshold experiment; original invalid attempt kept separate. |

## CPU orchestration and offload

| Evidence and location | Assessment |
|---|---|
| **CPU offload**<br>`campaigns/cpu_offload_hetzner_2026-09-01/README.md` | 54 cessions verified; README throughput differs from steps. |
| **CPU temperature candidate**<br>`campaigns/temperature_hetzner_2026-09-01/README.md` | Complete diagnostics; includes duplicated reference corpus and archive copies. |
| **CPU orchestration candidate**<br>`campaigns/temperature_hetzner_2026-09-02/README.md` | Behavior criteria pass; predecessor cession attribution needs correction. |

## Recordings

| Evidence and location | Assessment |
|---|---|
| **Recording archives**<br>`imports/2026-09-08-benchmark-preservation/recordings/runs` | 74 SQLite archives plus WAL/SHM and divergence reports; not a performance table. |

## Tools and supporting context

The preservation import contains `worktrees/`, `legacy-layout/temp/`, `recordings/runs/` and `genropy-asgi.bundle`. The detailed audit is at `campaigns/benchmark_audit_2026-09-08/README.md`.

See [data storage and recovery](../docs/data-storage.md) and [tool map](../docs/tool-map.md).
