# Benchmark PostgreSQL physical backup and WAL archive

Scope: only `genro-asgi-portal-db-1`. Sourcerer/pagspett are not included.
Repository: private bucket `genro-backups` in Falkenstein, prefix
`postgres/benchmark`. pgBackRest manages base backups, WAL and expiration
as a single chain. Do not add independent S3 lifecycle deletion rules to this
prefix: deleting required WAL would break recovery.

- PostgreSQL 17 image with pgBackRest and CA certificates; TLS verification on.
- Archive mode enabled, `archive_timeout=300` seconds. This is a segment-switch
  interval under activity, not a guaranteed recovery-point SLA during outages.
- Daily differential backup at 00:30 UTC (up to two minutes randomized delay).
- Sunday full backup; retain two full backups and their dependent chains.
  Before two full backups exist there is no complete two-cycle retention window.
- One backup worker, existing DB CPU and memory ceilings retained.
- Configuration file lives on the host in `backup/pgbackrest.conf`, owner 999,
  mode 600, mounted read-only. Actual S3 values come from the 1Password item
  `Hetzner bucket` and are never committed.

The host `.env` selects both Compose files through
`COMPOSE_FILE=compose.yaml:backup/compose.backup.yaml`. Future portal deploys
must preserve this selection. The custom database image is built on the host;
portal CI neither builds nor replaces it. Preserve the results volume.

Useful read-only verification:

```sh
docker exec --user postgres genro-asgi-portal-db-1 pgbackrest --stanza=benchmark info
docker exec --user postgres genro-asgi-portal-db-1 pgbackrest --stanza=benchmark check
systemctl status genro-benchmark-backup.timer
journalctl -u genro-benchmark-backup.service
```

`check` also forces WAL activity to verify archiving. Failed archiving retains
WAL locally and can fill the disk: backup service failures and archive failures
need attention. Systemd records failures; no external alert delivery is configured.

Restore only to a fresh volume/container; never use the running database volume
as a test target. Restore a selected backup with pgBackRest and the required WAL,
then query the recovered data. A base backup alone does not establish PITR.

Hetzner server backups remain a separate recovery layer. The new object archive
does not yet cover raw benchmark artifacts, other databases, or server settings.

## Verified installation — 2026-09-09

pgBackRest 2.59.1. First full backup: `20260909-111806F` (29.4 MB
uncompressed cluster). A marker table was created after that backup, followed
by named restore point `benchmark_backup_verified_20260909` and an archived
WAL switch. A fresh volume/container was restored to that point. SQL returned
`after-base-backup`, `pg_is_in_recovery() = false`, and zero benchmark runs,
proving WAL replay beyond the base backup. The marker and restore-test resources
were removed after verification. The first systemd differential backup passed.
Next scheduled execution: 2026-09-10 at approximately 00:30 UTC.

Three initial archiver failures came from a missing CA bundle during setup.
The image now includes ca-certificates and verified TLS archiving succeeds;
the cumulative failure counter is historical and was not reset to hide it.
The S3 credential was transferred from 1Password directly over SSH stdin, with
no plaintext key written in the local repository or command arguments.
