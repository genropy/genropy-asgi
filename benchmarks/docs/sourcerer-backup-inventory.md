# Sourcerer backup inventory — 2026-09-09

Read-only inspection of the shared Hetzner host.

- `/home/sourcerer/backup_db.sh` defines a localhost PostgreSQL dump of the
  `sourcerer` database, gzip output under `/home/sourcerer/backups`, and deletion
  of `.sql.gz` files older than seven days. No off-host transfer in this script.
- It embeds a database credential (not copied here), and has no `set -e` or
  `pipefail`: the final success message is not evidence of dump success.
- No invocation found in inspected system/user cron directories, cron files,
  or systemd backup units/timers. No `.sql.gz` files in the backup directory.
- Three `.dump` files exist, 5,723,271,454 bytes total: May 6, May 7 and August 28,
  2026. Names associate two with upgrades. They appear to be manual checkpoints;
  this classification is an inference, not a verified schedule.
- Latest file: `sourcerer_20260828.dump`, 1,846,062,931 bytes, mtime August 28 at
  11:41 UTC. This inspection does not constitute a restore test.
- No external copy mechanism was found in the inspected locations. Provider
  backup/snapshot status remains unknown: the available local Hetzner project
  does not list this server. Absence here does not exclude external automation.
- The new benchmark PostgreSQL volume is separate and is not included by the
  Sourcerer-specific script. It has no configured off-host backup yet.

No scripts were executed and no backups, credentials, databases or schedules
were changed. A scheduled backup and successful independent restore should be
established before relying on this host as the sole results archive.
