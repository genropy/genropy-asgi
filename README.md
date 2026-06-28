# genropy-asgi

Daemon-less **commander/worker** model that serves GenroPy legacy (synchronous) sites on
top of [genro-asgi](https://github.com/genropy/genro-asgi). No central daemon: session
state lives in the workers, global affinity in the commander.

- **GitHub**: https://github.com/genropy/genropy-asgi
- **Status**: Alpha
- **Package**: `genropy-asgi` (PyPI) · **import**: `genropy_asgi`
- **License**: Apache-2.0

## Installation

```bash
pip install genropy-asgi
```

`genro-asgi` is a dependency and is installed automatically. **GenroPy** must be available
in the environment at runtime (the worker runs a `GnrWsgiSite`).

## What it is

A standard multi-app `AsgiServer` (from genro-asgi) on which a **commander** app is mounted.
The commander:

- spawns and supervises N **worker** processes (each a minimal `GenroAsgiWorker` hosting a
  `GenropyProxy` that runs the GnrWsgiSite in a thread),
- forwards every request to the right worker — it is an application-level reverse proxy,
- keeps the **affinity registries** (`cid_to_user` + `user_registry`) and routes by our
  opaque `gnr_cid` cookie: a user always returns to the same worker (sticky-per-user).

Alongside the commander you can mount native async apps (e.g. the `MonitorApp` live
dashboard on `/_monitor`).

## Running

### Single site (debug / one process)

```bash
gnrasgiserve <site_name> -p 8000
```

Serves one GenroPy site directly through `genropy_config.py` (no commander).

### Commander + workers

The front server boots from `commander_config.py`, which reads launch parameters from
environment variables:

| variable | default | controls |
|----------|---------|----------|
| `GNR_ASGI_SITE` | *required* | the GenroPy site served by the workers |
| `GNR_ASGI_HOST` | `127.0.0.1` | front-server bind host |
| `GNR_ASGI_PORT` | `8080` | front-server port |
| `GNR_ASGI_WORKERS` | `1` | how many pool workers (the first also serves guests) |
| `GNR_ASGI_MAX_USERS_FIRST` | `20` | user cap of the first worker |
| `GNR_ASGI_MAX_USERS_OTHER` | `30` | user cap of the other workers |
| `GNR_ASGI_METRICS_INTERVAL` | `2.0` | metrics pull cadence (s) |

For debugging, a worker can be launched standalone:

```bash
python -m genropy_asgi.worker_entry <site> -p <port> --name pool_01 --group pool --nodebug
```

## Development

```bash
pip install -e .[dev]
pytest tests/
ruff check src/
```

## Architecture

See the vision document `architettura_daemonless.html` (in the genro-asgi repo) for the
target model. Cemented decisions: routing via the `gnr_cid` cookie (the GenroPy
session-cookie decoder is dead); a single `pool` group with the first worker as welcome;
user migration via `move_user`/`pop_user`/`add_user` with an opaque pickled blob.
