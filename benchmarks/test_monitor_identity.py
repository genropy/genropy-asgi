"""The lab's monitor: closed to anonymous, open to the admin, no literal secret.

The campaign wants the core's own monitor usable during the observation runs
and shut during the measured ones. What that needs from the recipe is exactly
three things, and this file checks all three against a REAL server built from
the lab recipe's own ``monitor_identity``:

- the two secrets arrive from the environment, and a missing one stops the boot
  rather than producing a server anybody can read;
- an anonymous request to ``/_server/monitor/`` is refused;
- the bootstrap admin logs in through the real ``/_server/login`` route and
  carries ``SERVER_ADMIN``, the tag the monitor is gated on.

It also greps the recipe for the two variable names, so a literal password
pasted in during a hurry fails a test instead of travelling to Hetzner.

    python3 test_monitor_identity.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECIPE = HERE / "docker" / "configs" / "cpu_policy_config.py"
sys.path.insert(0, str(HERE))

from genro_asgi import AsgiServer, BaseApplication  # noqa: E402
from genro_asgi.config import AsgiConfigBuilder  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got!r}"
          + ("" if ok else f"  atteso {want!r}"))
    if not ok:
        failures.append(label)


def load_recipe_class():
    """The lab recipe's own ServerConfiguration, imported from its file."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("lab_recipe", RECIPE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ServerConfiguration


class MonitorOnlyConfiguration(AsgiConfigBuilder):
    """The recipe's identity section, over a bare application.

    The lab recipe's ``main`` also builds the SPA pool, which needs a GenroPy
    site. What is under test is the identity, so the same method is called on a
    server that mounts nothing else.
    """

    monitor_identity = None  # replaced below by the recipe's own method

    def main(self, root):
        cfg = root.configuration()
        cfg.server()
        cfg.middleware()
        self.monitor_identity(cfg)
        applications = cfg.applications()
        applications.application(code="bare", mount="", app_class=BaseApplication)


MonitorOnlyConfiguration.monitor_identity = load_recipe_class().monitor_identity


def build_server(storage_dir, key, password):
    """A real AsgiServer from the recipe's identity section, or the boot error."""
    os.environ["GNR_ASGI_MONITOR_STORAGE"] = str(storage_dir)
    for name, value in (("GNR_ASGI_STORAGE_KEY", key),
                        ("GNR_ASGI_ADMIN_PASSWORD", password)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    return AsgiServer(config=MonitorOnlyConfiguration)


async def request(server, method, path, body=None, headers=()):
    """One request through the server at the ASGI level; returns (status, body)."""
    payload = json.dumps(body).encode() if body is not None else b""
    chunks = [(b"content-type", b"application/json")] if body is not None else []
    scope = {"type": "http", "method": method, "path": path,
             "query_string": b"", "headers": chunks + list(headers)}
    sent = []
    received = [{"type": "http.request", "body": payload, "more_body": False}]

    async def receive():
        return received.pop(0) if received else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await server(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    answer = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, answer


FERNET_KEY = "0BQZLB1yZeYcaqA9zCGAxYbNTuxU3fF-nUyPPQuDD1M="
#: A password that breaks everything a shortcut would use: a space breaks an
#: unquoted assignment, `#` starts a comment, `$` interpolates, the two quote
#: kinds and the backslash break both a sourced file and a hand-built JSON
#: string. If the login works with this one, the path carries any password.
PASSWORD = 'pa ss#word$X "quo" \'sing\' back\\slash' 

print("\n== i due segreti vengono dall'ambiente, e senza non si parte ==")
with tempfile.TemporaryDirectory() as directory:
    try:
        build_server(directory, FERNET_KEY, None)
        check("senza GNR_ASGI_ADMIN_PASSWORD il boot fallisce", "il server è nato", "un errore")
    except Exception as error:
        check("senza GNR_ASGI_ADMIN_PASSWORD il boot fallisce",
              type(error).__name__ != "", True)
        print(f"        {type(error).__name__}: {error}")

with tempfile.TemporaryDirectory() as directory:
    try:
        build_server(directory, None, PASSWORD)
        check("senza GNR_ASGI_STORAGE_KEY il boot fallisce", "il server è nato", "un errore")
    except Exception as error:
        check("senza GNR_ASGI_STORAGE_KEY il boot fallisce",
              type(error).__name__ != "", True)
        print(f"        {type(error).__name__}: {error}")

print("\n== il monitor è chiuso a chi non si è autenticato ==")
with tempfile.TemporaryDirectory() as directory:
    server = build_server(directory, FERNET_KEY, PASSWORD)
    for path in ("/_server/monitor/", "/_server/monitor/snapshot",
                 "/_server/monitor/panels"):
        status, _ = asyncio.run(request(server, "GET", path))
        check(f"anonimo su {path}", status, 401)

    print("\n== l'admin del boot entra dalla rotta vera e porta SERVER_ADMIN ==")
    status, answer = asyncio.run(request(
        server, "POST", "/_server/login",
        {"identity": "admin", "password": PASSWORD}))
    check("login admin", status, 200)
    payload = json.loads(answer or b"{}")
    tags = payload.get("tags") or payload.get("avatar", {}).get("tags") or []
    check("l'admin porta SERVER_ADMIN", "SERVER_ADMIN" in tags, True)

    # A wrong password is not an HTTP error: the handler answers 200 with an
    # error shape and no tags, and that is what "refused" means here.
    _, answer = asyncio.run(request(
        server, "POST", "/_server/login",
        {"identity": "admin", "password": "sbagliata"}))
    refused = json.loads(answer or b"{}")
    check("password sbagliata: nessun tag", refused.get("tags"), None)
    check("password sbagliata: un errore", bool(refused.get("error")), True)

    print("\n== il record dell'admin sta nello storage del monitor, cifrato ==")
    files = sorted(Path(directory).rglob("*.json"))
    check("un file per l'admin", len(files) >= 1, True)
    if files:
        raw = files[0].read_bytes()
        check("il record non è leggibile in chiaro", b'"identity"' in raw, False)

print("\n== nella recipe non c'è nessun segreto scritto ==")
source = RECIPE.read_text()
for name in ("GNR_ASGI_STORAGE_KEY", "GNR_ASGI_ADMIN_PASSWORD"):
    check(f"{name} arriva da EnvResolver",
          f'EnvResolver("{name}")' in source, True)
check("nessuna password letterale", PASSWORD in source, False)
check("nessun carattere della password difficile nella recipe",
      'back\\slash' in source, False)
check("nessuna chiave letterale", FERNET_KEY in source, False)

print("\n" + "=" * 50)
if failures:
    print(f"FALLITI {len(failures)}:")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("tutti i controlli passati")
