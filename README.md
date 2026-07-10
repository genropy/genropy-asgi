# genropy-asgi

Serve legacy (synchronous) **GenroPy** sites on an ASGI server — no register
daemon. genropy-asgi is the GenroPy-specific bridge on top of
[genro-asgi](https://github.com/genropy/genro-asgi): it hosts an unmodified
`GnrWsgiSite` behind uvicorn and, on demand, spreads the load over a supervised
pool of worker processes.

- **GitHub**: https://github.com/genropy/genropy-asgi
- **Status**: Alpha
- **Package**: `genropy-asgi` (PyPI) · **import**: `genropy_asgi`
- **Python**: >= 3.11 · **License**: Apache-2.0

## What it replaces

- **`gnrwsgiserve`** (werkzeug/WSGI) → **`gnrasgiserve`** (uvicorn/ASGI). Same
  site, same options, unmodified code — plus native WebSocket support.
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

**Single process** — the drop-in for `gnrwsgiserve`:

```bash
gnrasgiserve mysite -p 8080
# site on http://0.0.0.0:8080/index
```

**Pool** — one commander supervising N workers, sticky per user:

```bash
gnrasgiserve mysite --workers 2 -p 8080
```

`mysite` is the GenroPy instance name (or a site path). With `--workers N` the
same command runs the commander/worker model: each user is routed to a stable
worker and the pool grows under load.

Watch the pool:

```bash
curl -s http://127.0.0.1:8080/_server/monitor_state | python3 -m json.tool
```

## How it works

A GenroPy site is synchronous WSGI. genropy-asgi converts each ASGI request to a
PEP 3333 environ and runs the site in a thread executor, so uvicorn is never
blocked. The site's register — connections, pages, sessions, datachanges,
stores — is served **in-process**, not by a daemon.

- **Single** (`GenropySpaApplication`): one process hosts the site and is the
  commander of itself.
- **Pool**: a commander (`SpaMultiWorkerApplication`, from genro-asgi) supervises
  N workers (`GenropyWorkerApplication`), forwards every request to the right
  worker by an opaque `sticky_cid` cookie, and grows the pool when every worker
  crosses 80% of its user cap. Datachanges live locally on the page's own worker
  (the *switch model*); cross-worker changes arrive via the commander.

See [`docs/`](docs/) for the full architecture, single-vs-multi guide,
configuration, CLI reference, FAQ and troubleshooting.

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
