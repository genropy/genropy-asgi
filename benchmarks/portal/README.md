# Genro ASGI portal (minimal)

Target domain: `genroasgi.genropy.net`. Three mounted applications: home,
`/benchmarks/`, and the read-only `/api/benchmarks/`. Gramlot presentation is
intentionally later. The first page lists the newest 200 imports; detail pages
show completion checks and the recorded phase metrics without deriving rankings.

## Local Docker

From the repository root, set `BENCH_DB_PASSWORD` to a generated URL-safe password
(for example a hex value; never commit it), then:

```sh
docker compose -f benchmarks/portal/docker/compose.yaml build portal
docker compose -f benchmarks/portal/docker/compose.yaml up -d --wait db
docker compose -f benchmarks/portal/docker/compose.yaml run --rm portal \
    python -m benchmarks.portal.store init
docker compose -f benchmarks/portal/docker/compose.yaml up -d --no-build --wait portal
```

Open `http://127.0.0.1:19080/`. The database has no host port. The persistent
`results` volume belongs only to this Compose project. `docker compose down`
preserves it; do not use `down -v` on a real archive. Workload containers are in a
separate Compose project and are never started by portal deployment.

## Import explicitly selected results

Install the optional `portal` extra to run the CLI outside Docker. The importer
requires `BENCH_DATABASE_URL`; the web server never exposes a write endpoint.
For Docker, mount the selected input directory read-only:

```sh
docker compose -f benchmarks/portal/docker/compose.yaml run --rm \
    -v /absolute/campaign:/input:ro portal python -m benchmarks.portal.store import \
    /input/run_bridge --campaign example --environment hetzner-base \
    --purpose functional --revision COMMIT_OR_DIRTY_SNAPSHOT_HASH
```

Inputs are `<prefix>_outcome.json` and `<prefix>_windows.json`. PostgreSQL retains
both JSON documents, source hashes, campaign, environment, purpose and revision.
Identical inputs and metadata yield the same ID; repeated import is a no-op.
Changed inputs create a new record. No historical directories are scanned and
no historical result is imported automatically. Missing/incomplete measurements
are retained and labeled; functional and performance purposes remain explicit.

Raw SQLite recordings, CSV, logs and HTML artifacts remain in their preserved
external archive. This first schema does not ingest or serve them. Artifact
cataloguing and selected historical imports are later data work. Never ingest
secrets into window metrics: those are readable through the portal API.

## CI and Hetzner base

`benchmark-portal.yml` tests real PostgreSQL persistence and HTTP contracts,
then builds and smoke-tests Docker. Merges to `main` (the repository default)
publish `ghcr.io/genropy/genropy-asgi-portal:<commit>`. Deployment uses the
published immutable digest. Pull requests never deploy.

Provision `/opt/genro-asgi-portal` separately from Sourcerer and the workload
lab. Place a mode-600 `.env` there with a generated `BENCH_DB_PASSWORD` and
`PORTAL_PORT=19080`. The host needs registry pull access if the image is private.
The deploy account needs access to this directory and Docker. Configure GitHub:

- Environment: `hetzner-base`.
- Secrets: `BENCH_PORTAL_SSH_KEY`, `BENCH_PORTAL_KNOWN_HOSTS` (verified host key).
- Variable: `BENCH_PORTAL_SSH_DESTINATION` (`user@host`).
- Repository variable: `BENCH_PORTAL_DEPLOY_ENABLED=true` only after provisioning.

The script pulls the image, ensures PostgreSQL is healthy, applies the additive
v1 schema, and waits for portal health. On application failure it restores the
previous image when available. It does not roll back database migrations;
future incompatible migrations need an explicit migration/backup design.

DNS, TLS and reverse-proxy setup are separate from image rollout. The live
bootstrap below provisions them for the first deployment. The workflow does not
modify nginx or renew certificates. Off-host backup remains to be configured.

## Live bootstrap — 2026-09-09

The minimal portal is deployed at https://genroasgi.genropy.net/ on the Sourcerer
host. Route 53 A record points to 46.62.203.133 (TTL 300). Dedicated nginx config
is provided in `docker/nginx.conf`; HTTP redirects to HTTPS. The Let's Encrypt
certificate expires 2026-12-08 and renewal has a dedicated nginx reload hook.

Remote directory: `/opt/genro-asgi-portal`, Compose project `genro-asgi-portal`,
image `genro-asgi-portal:bootstrap-20260909`. PostgreSQL uses its own results
volume with no published port. The application binds host loopback 19080.
The generated database password lives only in the mode-600 remote `.env`.
The initial source snapshot is uncommitted worktree code, archive SHA-256:
`8d53f883c9f78dd001cb5645c29c935ba40a7bccdd171770adf9eaf1f86a0b0a`.

Externally verified: health and results page 200, valid TLS, HTTP redirect 301,
internal `/_server/` route 404. The archive starts empty. Existing Sourcerer
nginx duplicate-name warnings predate this deployment and were left unchanged.
CI automatic deployment and off-host backups are still not activated. This
bootstrap was built on the server; subsequent CI deploys use the tested image
from the registry. No historical benchmark data has been imported.

## Online technical documentation

`/internals/` is a fourth genro-asgi application serving a prebuilt MkDocs
snapshot, mounted read-only at `/docs`. Source checkout: genro-asgi commit
`9da1e92f655788985bd0d5e034684a08e132e7e9` (2026-09-09 publication).
The build passed strict mode and the link checker found zero broken references
across 277 HTML pages. This is the technical Internals dossier, not the Sphinx
user guide; its own review/proposal labels remain visible.

Build a fresh snapshot using an environment with genro-asgi's `internals` extra:

```sh
python -m benchmarks.portal.build_internals /absolute/genro-asgi \
    --out /absolute/fresh-webroot/internals
python /absolute/genro-asgi/.mkdocs/check_links.py /absolute/fresh-webroot
```

`PORTAL_DOCS_HOST_DIR` selects the host directory mounted into Docker. On Hetzner
it defaults to `/opt/genro-asgi-portal/internals` via the Compose directory.
Only the generated HTML/assets are deployed; the docs endpoint cannot browse
the source filesystem. Diagram rendering uses the dossier's Mermaid CDN.
Documentation is a versioned manual snapshot for now; image CI updates do not
silently rebuild it from a different genro-asgi revision.

## User guide and API reference (Sphinx)

`/docs/` serves the HTML generated from genro-asgi's `docs/conf.py`, using the
same Sphinx configuration and Python 3.12 selected by `.readthedocs.yaml`.
It is separate from `/internals/`, and both are linked from the portal home.
`PORTAL_USER_DOCS_HOST_DIR` selects the read-only host mount (default `./docs`
relative to Compose), exposed to the application as `/user-docs`.

Build from the intended genro-asgi checkout:

```sh
python -m sphinx -b html -W --keep-going -c docs docs /absolute/new-site
python .mkdocs/check_links.py /absolute/new-site
```

The 2026-09-09 snapshot generated 105 HTML pages with zero broken local
references. Strict Sphinx exited nonzero due to two existing `toc.not_included`
warnings for `docs/internal/opaque_transport.md` and
`docs/internal/opaque_transport_benchmark.md`. These warnings were not suppressed
or fixed in the source repository. The generated site was published with this
known navigation limitation; this is not a passing strict Read the Docs build.
Publication does not include Read the Docs hosting features such as its version
selector. Updating this snapshot is currently manual, like Internals.

Sphinx release/version were explicitly set to `0.46.0`, matching the selected
checkout's pyproject.toml; the host's installed metadata still said `0.36.0`.
This override corrects the label without changing documentation source files.

## Database protection now enabled

As of 2026-09-09 the benchmark PostgreSQL cluster has off-host physical backups
and continuous WAL archiving to Hetzner S3. A restore beyond the base backup was
verified. See `backup/README.md` for retention, timer and recovery evidence.
This supersedes earlier bootstrap notes about the absence of off-host backups;
Sourcerer and raw artifact archiving remain separate work.

Historical imports may pass `--assessment public-assessment.json`. This JSON object
is stored inside provenance, participates in immutable import identity, and is
shown on the public detail page/API. Include limitations and private artifact
references, never credentials or signed URLs. Changing the assessment creates a
new identity, so reuse the original assessment for retries. The first curated
historical batch is documented in `../docs/historical-import-20260909.md`.
