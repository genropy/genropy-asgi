## Phase 1

- **The install recipe needed the `pgsql` extra.** The plan's Details step 1
  installs `gnrpy` plain, but the Postgres driver (`psycopg2-binary`, `psycopg`)
  is an optional dependency in `gnrpy/pyproject.toml`, so the venv could not
  reach the database and the phase's `Done:` — a successful login — was
  unreachable. Installed `gnrpy[pgsql]`. `gunicorn` is a base dependency, so the
  plan's "install it if not" check was a no-op.
- **The twin instance carries configuration only.** `site/data/` held the live
  runtime state of the other stack (35 connection folders, `_users`,
  `_frozen_users`) and `site/_static/_jslib` a regenerable minified-js cache.
  Copying them would have started the first trace on another run's leftovers,
  so only `instanceconfig.xml`, `root.py` and `site/siteconfig.xml` were copied.
  The site recreates the rest. Owner approved on 2026-08-23.
- **Debug off is the standard declared condition.** Rejected running in debug
  even though it would populate `X-GnrSqlTime` / `X-GnrSqlCount`:
  `gnr web serveprod --debug` wraps the site in werkzeug's debugging middleware,
  which the bridge has no equivalent of, so error responses would diverge
  because of the instrument rather than the stacks. The recorders collect the
  `X-Gnr*` headers either way, so nothing has to be re-instrumented; a debug run
  is the declared variant when those two fields must carry real numbers. Owner
  approved on 2026-08-23.
- **The sitedaemon binds 40004 and the site config cannot change it.** The CLI
  passes no port, so the chain in `gnr/web/daemon/service.py` ends at
  `PYRO_PORT` in `gnr/web/daemon/siteregister.py`. The port 40404 the site
  config reports under `gnrdaemon` is the address of the multi-site daemon and
  is never used here. Worth knowing before anyone tries to run two standalone
  sitedaemons side by side.
- **A stale multi-site `gnrdaemon` (from 2026-08-19) was holding 40004** and
  would have silently prevented the sitedaemon from starting. Terminated with
  the owner's approval; the hygiene check in the README exists for this.
