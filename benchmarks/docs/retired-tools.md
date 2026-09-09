# Retired benchmark tools

The following unchanged historical scripts were removed from the active tree.
Their source remains in Git at `664dda96f6da7715af5eadbc993f3f0896c0c296`; no extra code archive was created.
Recover an individual file with `git show 664dda96f6da7715af5eadbc993f3f0896c0c296:benchmarks/FILE`.

| Files | Reason |
| --- | --- |
| `scenarios.py`, `load_harness.py` | Elastic-pool v1.1 configuration and monitor contracts; superseded by the maintained population-cycle package. |
| `cost_model_ramp.py` | Previous occupancy model and monitor surface; machine-specific PID lookup. |
| `run_grid.py` | Personal absolute paths into temporary assets and implicit process management on a fixed port. |
| `gil_ramp.py`, `wire_counter.py` | Removed `genro_daemon` interfaces. |
| `sr_counter.py`, `gunicorn_count.conf.py` | Old register instrumentation and personal temporary import path; current recording modules provide the maintained capture path. |
| `gunicorn_probe.conf.py` | Imports an unshipped `probe_reg` from `/tmp`. |
| `gunicorn_probe_critsec.conf.py` | Removed daemon register-client instrumentation. |

No remaining Python or shell caller depends on these files. Historical measurement
notes describe their original methodology; they are not current execution guides.
`scaling_probe.py` and earlier HTTP probes remain because other retained tools
still import their session/workload helpers. Compatibility entry points in
`compare/` remain necessary for existing commands and checks.

The five already imported eight-core results are the retained historical baseline.
No bulk historical import or deletion of external archives is part of this cleanup.
