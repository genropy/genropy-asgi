# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Components the two comparison scenarios genuinely share.

Three modules, and nothing that exists only to look general:

- ``stop_guard`` — the stop flag every phase reads, and the memory guard that
  raises it from the host's view of the container's cgroup;
- ``container_probe`` — one kernel read per sample, with a role map per stack;
- ``load_engine`` — the rate-paced generator, identical for bridge and legacy.

``lab_lifecycle.sh`` sits beside them: the runner's half of the same contract.

These are benchmark tools. Nothing here is imported by ``src/``, and nothing
here belongs in a distributed package: they exist to measure the bridge against
the legacy daemon and they live under ``benchmarks/`` for that reason alone.
"""
