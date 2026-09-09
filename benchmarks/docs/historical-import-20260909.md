# First historical import — 2026-09-09

Five completed population-cycle runs were imported into the production benchmark
PostgreSQL database. Earlier invalid attempts remain in the local preservation
archive and were not imported. Local campaigns, capacity runs and CPU-policy
experiments still require separate selection; this batch is not the whole archive.

| Run | Configuration | Source campaign |
| --- | --- | --- |
| e8cv5_legacy | Gunicorn 8 workers | e8c_full2_hetzner_2026-08-31 |
| e8cv5_bridge | Bridge fixed 8 workers | e8c_full2_hetzner_2026-08-31 |
| e8cp_legacy_w12 | Gunicorn 12 workers | e8c_profiles_hetzner_2026-08-31 |
| e8cq_bridge_dynamic | Bridge dynamic | e8c_profiles2_hetzner_2026-08-31 |
| e8cq_legacy_w16 | Gunicorn 16 workers | e8c_profiles2_hetzner_2026-08-31 |

All five have six windows and declare the same 120-user plan hash. They are single
historical trials, not a statistically established ranking. Historical
`late_drift_s` and `pending_at_end` have known load-engine calculation/accounting
limitations and have not been recomputed. `completed` means finished calls, not
successful HTTP responses. The original README comparison also has an error-count
discrepancy for legacy W8. Per-run public assessments retain these caveats.

## Storage and verification

- PostgreSQL retains original outcome/window JSON content, input hashes and public
  assessment metadata. There are 5 runs and 30 windows in this first batch.
- The private S3 archive is
  `s3://genro-backups/artifacts/benchmarks/20260909-eightcore/originals.tar.gz`.
  SHA-256: `440141fce09c4f5aeacad0658b6690507ab13cd46bd53975dacb38142dbdbb60`.
- Its manifest is at the same prefix as `manifest.json`, SHA-256
  `637bfd0042e770d533b57d004967cc0d91b968e956674842c99804bd68a25a24`.
- The archive contains 53 selected original files (39,658,114 bytes), plus the
  selection manifest and five assessments; gzip size is 5,901,428 bytes. Original
  files covered by historical SHA manifests were verified. Selected text contains
  no matches for the credential markers checked during curation. Compose files,
  login/session captures and full preservation bundles were deliberately omitted.
- Both uploaded objects were downloaded again and their SHA-256 verified.
- A private server copy is under `/opt/genro-asgi-portal/imports/`.
- Source files remain under `~/genro_bench/genropy-asgi/campaigns/`, unchanged.
- Git stores only the selection manifest, receipt and documentation under
  `benchmarks/catalogue/imports/`; raw benchmark files are not added to Git.

The importer was executed twice per run to verify idempotency, and stored JSON was
compared with the original documents. HTTPS checks verified all five detail/API
pages and their assessment warnings. The portal uses image
`genro-asgi-portal:historical-20260909`; the PostgreSQL WAL-backup override is retained.

The S3 artifact copy is separate from pgBackRest's `/postgres/benchmark` repository.
It does not by itself configure automatic archiving of future benchmark artifacts.

A post-import run of `genro-benchmark-backup.service` completed with
`Result=success` and `ExecMainStatus=0`, protecting the newly imported database
content as well as the separately verified S3 artifact archive.
