# Benchmark data storage and recovery

Reorganized on 2026-09-08. Permanent local root:
`~/genro_bench/genropy-asgi/`, outside the repository and its temporary tree.
No raw campaign or recording is added to public Git.

## Preserved material

- Original campaign directories now live under `campaigns/`, retaining names,
  internal structure, failed attempts and manifests.
- Former `benchmarks/docker/runtime/` lives under `local-runtime/2026-09-08/`.
- Original paths remain symlinks so existing local references still resolve.
- Original temporary context is captured in
  `imports/2026-09-08-benchmark-preservation/legacy-layout/temp/`, excluding the
  virtualenv, disposable caches and the migration workspace.
- Exact benchmark copies and tracked diffs are under the import's `worktrees/`.
  Branches, commits and status are in `preservation.json`.
- The import's `recordings/runs/` preserves recordings with WAL/SHM companions.
  The original recording store remains `~/genro_bench/runs/`.
- `genropy-asgi.bundle` preserves this repository's Git refs/history. It is not
  a backup of every dependency repository; their revisions are recorded in the
  campaign evidence.

The import contains 6,504 copied source files (880,539,559 bytes). Its complete
manifest covers 6,514 files including generated metadata and the Git bundle.
Seventy-four SQLite databases passed integrity checks on copied database/WAL
sets. The separate compressed snapshot was verified against every manifest
entry before originals were relocated.

## Navigation

The repo's [catalogue](../catalogue/README.md) groups evidence by purpose and
records validity qualifications. `campaigns.json` uses paths relative to the
permanent root, with original paths retained as provenance. Entries may overlap;
they are not independent statistical repetitions.

The permanent root's `README.md` links directly to the local collections. The
audit lives in `campaigns/benchmark_audit_2026-09-08/`. Loose historical files
which were not relocated are preserved in the import and indexed there.

## Recovery

The separate compressed copy is:

```text
~/genro_bench/genropy-asgi/backups/2026-09-08-benchmark-preservation.tar.gz
```

Its adjacent `.sha256` verifies the compressed file. The archive contains its
own `MANIFEST.sha256`, excluding self-references. Extract into a **new empty
directory**, then verify that manifest; do not extract over live data.

`relocation.json` maps old directories to canonical locations. Rebuild
compatibility links from that map if `temp/` is cleaned. Recover uncommitted
benchmark tools from `worktrees/` and tracked history from the Git bundle in a
separate repository. The `maintenance/` directory in the permanent root retains
the migration scripts and receipts.

A SQLite file with a nonempty `-wal` is not a complete standalone recording.
Use a consistent database backup or preserve the complete quiescent file set.
Inspect archival copies without modifying the originals.

## Limits and new measurements

The expanded snapshot and compressed copy are both on this Mac. They protect
against accidental cleanup of temporary files, not disk loss. An off-machine
backup destination was not configured by this work.

Use new output directories for new runs. Preserve effective configuration,
workload hash, stop reason and raw data. Correct reports in separately identified
derived analyses; do not rewrite raw evidence. The migration did not certify
old launchers against the current core or launch a benchmark/cloud machine.
