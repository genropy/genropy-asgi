# genro-asgi 0.46.3 bridge live acceptance

Reproducible procedure: the shared `test_invoice_pg` site, no private path, no
uncommitted tree. Date: 2026-09-13. Bridge branch `feat/opaque-transport-issue72`
at `b701b54` (pin `genro-asgi>=0.46.3,<0.47`, proxy test declared as a class).

## What the release changed for the bridge (0.46.1 contract)

- `AsgiServer` builds every application from its configuration: no instance.
- Shutdown turns the server state at the signal: SSE sources end, no worker is
  born while leaving, a request on an open connection reads 503 + `Retry-After`.
- A first path segment starting with a dot answers 404 in the server.
- Applications with core routes answer 400 to a refused value, 415 to a
  content-type without decoder (strict reading; 422 only under `error_codes="fastapi"`).

## Environment

Python 3.12.9, venv built with `uv`: `genro-asgi==0.46.3` from PyPI with its
dependencies, the bridge editable with `--no-deps`, GenroPy editable from its
checkout (`genropy 26.9.1`), `psycopg2-binary`, `psycopg`, PostgreSQL local.

| package | version |
|---|---|
| genro-asgi | 0.46.3 |
| genro-bag | 0.22.0 |
| genro-builders | 0.23.2 |
| genro-routes | 0.30.0 |
| genro-storage | 0.8.1 |
| genro-toolbox | 0.14.0 |
| genro-tytx | 0.15.0 |
| uvicorn | 0.52.4 |
| psycopg2-binary | 2.9.13 |

Test suite: `pytest tests/` → 272 passed, 2 skipped (benchmark portal and smoke
site, both gated on environment variables).

## Launch

```sh
GNR_DAEMON_PROVIDER=genropy-asgi GNR_ASGI_ORCHESTRATION_PROFILES=1 \
PGGSSENCMODE=disable gnrasgiserve test_invoice_pg -p 8091
```

Startup: template process built the site, forked `pool_0001`, uvicorn on
`127.0.0.1:8091`.

## Results

| probe | result |
|---|---|
| `GET /metrics` | 200, three counters |
| `GET /.hidden/x`, `GET /.well-known/foo` | 404 |
| `GET /` | 200, `Set-Cookie` of the site and `spa_connection_id` |
| `GET /_sysop/configuration` | 200 (profiles page) |
| `GET /_sysop/configuration/read` (no `name`) | 400 |
| `GET /_sysop/configuration/read?name=` | 400 |
| `GET /_sysop/configuration/read?name=nope` | 404 |
| `POST /_sysop/configuration/save` with `text/csv` | 415 |
| `POST /_sysop/configuration/save` with broken JSON | 400 |
| `GET /_sysop/mcp` | stream stays open (MCP endpoint) |
| browser login `amelia.martin`, Invoice page | ok; `/metrics` counters populated |
| SIGTERM with the `/_sysop/mcp` stream open | process gone in **0.31 s** |
| request on a keep-alive connection during the grace window | **503**, `Retry-After: 5` |
| template and worker after exit | none alive, pool socket removed |
| shutdown log | one `ResourceWarning` (known), no SSE `CancelledError` |

On 0.46.0 the same shutdown took 5.4 s (see `issue72-live-browser.md`).

Observation for the core, not for the bridge: the 503 of the grace window
carries `Set-Cookie: session_id=...`; the server mints a session while leaving.

## SIGTERM probe

```python
import os, signal, socket, subprocess, sys, time
pid = int(sys.argv[1]); HOST, PORT = "127.0.0.1", 8091
stream = subprocess.Popen(["curl", "-sN", f"http://{HOST}:{PORT}/_sysop/mcp"], stdout=subprocess.DEVNULL)
time.sleep(1.0)
ka = socket.create_connection((HOST, PORT)); ka.settimeout(5)
ka.sendall(b"GET /metrics HTTP/1.1\r\nHost: x\r\n\r\n"); ka.recv(65536)
t0 = time.monotonic(); os.kill(pid, signal.SIGTERM); time.sleep(0.05)
ka.sendall(b"GET /metrics HTTP/1.1\r\nHost: x\r\n\r\n"); grace = ka.recv(65536)
while True:
    try: os.kill(pid, 0); time.sleep(0.02)
    except ProcessLookupError: break
print(grace.split(b"\r\n")[0], f"{time.monotonic() - t0:.2f}s", stream.wait(timeout=10))
```
