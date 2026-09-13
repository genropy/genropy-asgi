# Hetzner base: functional laboratory

Owner decision: use the always-on Sourcerer host for Docker functional campaigns;
reserve a dedicated Hetzner machine for stress and performance measurements.
Local development and fast tests remain available.

## Read-only inventory, 2026-09-09

Source documentation: `genro-sourcerer/docs/ricognizione_produzione_20260828.md`
and `docs/faq.md`. SSH access was verified using the documented root account
and the existing trusted host key; no credentials were copied.

Host `ubuntu-4gb-hel1-1` (`46.62.203.133`): four CPUs, 7745 MiB RAM,
4518 MiB available at inspection, no swap; 43 GiB free on the root filesystem.
Docker 29.2.1 and Compose v5.0.2 respond. Existing running containers are
`sourcerer-db-1`, `sourcerer-mcp-1`, and `seaweedfs`. Host services also consume
resources. Sourcerer and pagspett share the existing PostgreSQL service.
These numbers describe a snapshot, not reserved capacity.

## Proposed isolated layout

- Compose project: `genro-bench-base`.
- Root: `/opt/genro-bench-base` (absent at inspection).
- Separate source snapshots, configuration, plans, runtime and campaign output.
- Dedicated PostgreSQL container and volume, without a published database port.
  Initialize it from the demo benchmark dump, never from Sourcerer data.
- Bridge/Gunicorn ports: loopback-only 19098/19099, currently no listeners.
  Access via SSH forwarding; no nginx changes. Existing nginx documentation
  mentions routes to 8099/18088, so those host ports should not be reused.
- Initially one tested stack at a time: 1 CPU / 1536 MiB; database 0.5 CPU /
  512 MiB; driver 0.5 CPU / 256 MiB. These are proposed container ceilings,
  subject to functional validation; builds also need controlled resource use.
- A small population plan with an explicit worker count. Preserve the eight-core
  plans and recipes for their original scenario; do not relabel them as base.
- Mark every campaign `environment=hetzner-base` and `purpose=functional`.
  Timings help diagnose failures but do not establish performance superiority.

## Implementation prerequisites

Current cycle runners hard-code the project name, container names and host
ports. Parameterize these consistently with Compose and lifecycle cleanup,
then verify isolation with sequencing tests. Provide a reduced, certified plan
and matching worker policy; merely reducing the CPU limit is insufficient.
Record source revisions plus dirty-tree content hashes when deploying the
uncommitted reorganization. Keep outputs outside source snapshots.

No files, containers, databases, services or firewall rules on the remote host
were changed during this inventory. Remote deployment has not happened yet.
