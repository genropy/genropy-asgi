# genropy-asgi

Serve legacy (synchronous) **GenroPy** sites on an ASGI server — no register
daemon. genropy-asgi is the GenroPy-specific bridge on top of
[genro-asgi](https://github.com/genropy/genro-asgi): it hosts an unmodified
`GnrWsgiSite` and spreads its users over a supervised pool of worker processes,
each user pinned to one of them.

- **GitHub**: https://github.com/genropy/genropy-asgi
- **Status**: Alpha
- **Package**: `genropy-asgi` (PyPI) · **import**: `genropy_asgi`
- **Python**: >= 3.11 · **License**: Apache-2.0

## What it replaces

- **`gnrwsgiserve`** (werkzeug/WSGI) → **`gnrasgiserve`** (uvicorn/ASGI). Same
  site, same options, unmodified code.
- **The register daemon** (Pyro4, then `genro-nodaemon`) → an **in-process**
  register. There is no daemon to start or connect to.

## Installation

```bash
pip install genropy-asgi
```

Latest development version, straight from GitHub:

```bash
pip install git+https://github.com/genropy/genropy-asgi.git
```

`genro-asgi` is installed automatically. **GenroPy** must be present at runtime
(the worker runs a `GnrWsgiSite`) and configured as usual (`~/.gnr/environment.xml`
plus an existing site). genropy-asgi imports `gnr.*` only at runtime.

## Usage

```bash
gnrasgiserve mysite -p 8080
# site on http://127.0.0.1:8080/index
```

`mysite` is the GenroPy instance name — the same you pass to `gnrwsgiserve` — or
a path to a site directory. That is the whole launch: there is no worker count
to declare and no single/pool selector. The pool always runs, starts with one
worker and adds another when the ones it has have no room for a newcomer.

On macOS export `PGGSSENCMODE=disable`: the workers are born by `fork` and libpq
negotiating Kerberos inside a forked child crashes it.

Watch the site-wide counters, no authentication needed:

```bash
curl -s http://127.0.0.1:8080/metrics
```

## How it works

A GenroPy site is synchronous WSGI. genropy-asgi converts each ASGI request to a
PEP 3333 environ and runs the site on a thread pool, so the event loop is never
blocked. The site's register — connections, pages, sessions, datachanges,
stores — is served **in-process**, not by a daemon: the package declares the
`gnr.web:daemon` entry point, and GenroPy resolves its daemon namespace to it
only when `GNR_DAEMON_PROVIDER` names the provider, which the CLI does for its
own process. The choice is per process, so the classic stack and this one can
share one virtualenv.

- **Every user lives in one worker**, with all his pages. Routing is by identity:
  the `spa_connection_id` cookie carries the connection id the site itself minted
  while serving, and the commander knows whose it is.
- **Workers are born by fork** out of a template process that builds the
  `GnrWsgiSite` once for all of them, so starting one more costs a fork and not a
  cold start.
- **The pool sizes itself** on measured occupancy — the number of processes is a
  reading, never a setting. `GNR_ASGI_WORKER_MAX_USERS` caps how many users one
  worker may hold.
- **A quiet user is frozen** to disk and his worker gets the memory back; his
  next request wakes him. A restart parks everybody the same way, so nobody is
  logged out by it.
- **Changes travel addressed**: what one page writes, or a table event, reaches
  the pages that subscribed it, wherever they sit. The legacy `globalStore()` is
  one master on the commander with no replicas — a worker reads it with a call
  and writes it through an all-or-nothing grant.

See [`docs/`](docs/) for the pool, configuration, CLI reference, FAQ,
troubleshooting — and [`docs/status.rst`](docs/status.rst) for what is built
today.

## Documentation

The documentation is built with Sphinx:

```bash
pip install -e .[docs]
cd docs && make html
# open docs/_build/html/index.html
```

## Development

```bash
pip install -e .[dev]
pytest tests/
ruff check src/
```

## License

Apache License 2.0 — Copyright 2025 Softwell S.r.l. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).
