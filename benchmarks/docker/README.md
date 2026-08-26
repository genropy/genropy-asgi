# The measurement lab

Four containers, one purpose: the same benchmarks of `benchmarks/`, on Linux,
with declared and equal resources — reproducible by anyone with the three
source trees, not only on the machine they were born on.

| Service | What it is | Port |
|---|---|---|
| `postgres` | PostgreSQL 17, two databases: `test_invoice_pg` (from the dump) and `test_invoice_bridge` (template copy) | — |
| `legacy` | the classic stack, whole: `gnrdaemon` + gunicorn (`LEGACY_WORKERS`, default 1) | 8099 |
| `bridge` | `gnrasgiserve` with the SPA pool, inspector on (`/_server/inspector/census`) | 8098 |
| `bench` | the driver (profile `bench`, run on demand) | — |

## Why a lab

- **`/proc` exists**: `rss_bytes` reads real memory, so the occupancy's memory
  component works — on macOS it is None and the pool is memory-blind.
- **cgroup limits are real**: `cpus: 4` / `mem_limit: 2g` per stack in the
  compose file are the declared parity, and a memory quota can actually be hit.
- **The macOS-only defects stay outside**: the libpq/Kerberos fork segfault
  (`PGGSSENCMODE`), the SQLite fork death, the 16k ephemeral-port ceiling.
- **The instrument is a separate container**: the bench drives over the network
  like a real client and its CPU does not pollute the stacks' measures. Read
  each stack's CPU/memory with `docker stats`, per container, from the kernel.

Numbers measured in the lab and numbers measured natively on macOS never mix
in the same table: same order of magnitude, different platform.

## What is image and what is mount

The images carry the ENVIRONMENT only (python 3.12, uv, libpq — plus genropy,
see below). The three source trees arrive as read-only mounts, so what runs is
always the mounted tree's commit:

- genropy is installed **in the image**: it is the pinned baseline and its
  build drags five sibling directories (~340 MB). When the baseline tree
  moves: `docker compose build legacy bridge`.
- genro-asgi and genropy-asgi are installed **at container start** from their
  mounts (seconds, thanks to the uv cache volume): a container restart is
  enough to run their current commit.

Host paths live in `.env` (copy `.env.example`).

## Bring it up

```bash
cp .env.example .env            # once, adjust paths
./scripts/dump_db.sh            # photograph the local test db into runtime/initdb/
docker compose up -d postgres legacy bridge
```

First boot is slow (image build, dependency download, site build); wait for
`curl localhost:8099/` and `curl localhost:8098/` to answer 200.

## Drive a run

```bash
docker compose run --rm bench python3 single_record_bench.py \
    --base http://bridge:8098 --levels 1,2,4,8 --duration 15
```

Every script in `benchmarks/` works the same way — `--base http://legacy:8099`
for the other stack. Pool knobs ride the environment in `compose.yaml`
(`GNR_ASGI_WORKER_MAX_USERS`, `LEGACY_WORKERS`).

## The lab instances

`projects/lab_bench/instances/{legacy_lab,bridge_lab}` — instanceconfig with
`host="postgres"`, packages resolved by explicit `path` into the mounted
trees, plus the `root.py` the site resolver requires. Their `site/` content is
runtime material, gitignored like `runtime/`.

## Traps this lab already paid for

- `cryptography`'s embedded OpenSSL probes ARM crypto extensions the Docker
  Desktop VM does not expose and dies of SIGILL: `OPENSSL_armcap=0` in the
  image disables the probe.
- gnrpy's build writes `egg-info` into its source and packages five sibling
  dirs: it cannot install from a read-only mount — hence baked into the image.
- A package `path` in instanceconfig names the directory CONTAINING the
  package folder, not the package folder itself.
- The site resolver accepts an instance as a site only if `root.py` sits next
  to `instanceconfig.xml`.
- `gnrasgiserve` must bind `-H 0.0.0.0` or the published port answers nothing.
- First bridge boot in the container: the first worker births can fail their
  fork dialogue and the pool retries until one lands (~2 minutes worst seen).
  Under observation — container-only, first boot only.
