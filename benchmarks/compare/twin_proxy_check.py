"""Isolation checks for the twin proxy: everything it decides BEFORE the wire.

No stacks, no site, no site database. What can be asserted without them is the
part that decides how a session is compared, and it is exactly the part that is
silent when it is wrong: the instance the bridge serves, the two database
commands, what is dispatched against what is compared, the identifiers rewritten
on the way to the shadow, and the join between the two archives.

The join is asserted against REAL archives — two throwaway `RunArchive` files in
`temp/`, written with the lines the two recorders would write. A join asserted
against hand-made dictionaries proves that the test agrees with itself; written
into the archive and read back through `TraceReader`, it proves the query works
on the shape the recorders actually produce.

What is NOT here, because it needs two live stacks: the launch, the readiness
tokens, the copy actually running, and a divergence met on the wire. Those are
the phase's own run, in the browser.

It runs on the BRIDGE interpreter, with the bench environment, because the proxy
imports genropy to read the instance configuration:

  GENRO_GNRFOLDER=$PWD/temp/gnr \\
      PYTHONPATH=<pinned genropy>/gnrpy \\
      python benchmarks/compare/twin_proxy_check.py
"""

import os
import sys

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
BENCH_ROOT = os.path.dirname(os.path.dirname(BENCH_DIR))
sys.path.insert(0, BENCH_DIR)

from replica import REPLICA_HEADER, TraceReader   # noqa: E402
from run_archive import RUN_NAME_ENV, RunArchive  # noqa: E402
from twin_proxy import (HOP_BY_HOP, REPLY_OWNED, TWIN_HEADER,  # noqa: E402
                        DatabaseCopy, TwinInstances, TwinProxy)

LEGACY_ARCHIVE = os.path.join(BENCH_ROOT, "temp", "twin_proxy_check_legacy.sqlite")
BRIDGE_ARCHIVE = os.path.join(BENCH_ROOT, "temp", "twin_proxy_check_bridge.sqlite")

failures = []


def check(label, condition):
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


def drop_archives():
    for path in (LEGACY_ARCHIVE, BRIDGE_ARCHIVE):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


def build_proxy():
    """A proxy with its two readers and nothing else: no stacks, no client."""
    proxy = TwinProxy(instances=None, legacy=None, bridge=None, port=8097,
                      legacy_port=8099, bridge_port=8098, run_name="check run")
    proxy.reference = TraceReader(LEGACY_ARCHIVE)
    proxy.shadow = TraceReader(BRIDGE_ARCHIVE)
    return proxy


print("\n1. the two instances of a twin run")
instances = TwinInstances("sandbox")
check("the bridge serves the legacy instance plus _asgi",
      instances.bridge_name == "sandbox_asgi")
check("a name already ending in _asgi is not special-cased",
      TwinInstances("sandbox_asgi").bridge_name == "sandbox_asgi_asgi")
check("an instance that does not exist is not ready, and does not raise",
      instances.ready is False)

print("\n2. the database copy")
copy = DatabaseCopy({"dbname": "site_pg", "host": "localhost", "port": "5432"},
                    {"dbname": "site_pg_asgi"})
dropped, created = copy.commands
check("the copy is dropped before it is made",
      dropped[0] == "dropdb" and created[0] == "createdb")
check("the drop tolerates an absent copy",
      "--if-exists" in dropped and dropped[-1] == "site_pg_asgi")
check("the copy is made FROM the legacy database",
      created[-3:] == ["-T", "site_pg", "site_pg_asgi"])
check("host and port are the ones the instance declares",
      ["-h", "localhost", "-p", "5432"] == created[1:5])
check("nothing is invented for a connection the instance does not declare",
      DatabaseCopy({"dbname": "a"}, {"dbname": "b"}).commands[0]
      == ["dropdb", "--if-exists", "b"])

print("\n3. what reaches the two stacks, and what does not")
proxy = TwinProxy(instances=None, legacy=None, bridge=None, port=8097,
                  legacy_port=8099, bridge_port=8098, run_name="check run")
check("a hop-by-hop header is never relayed", "connection" in HOP_BY_HOP)
check("the proxy writes its own content length, date and server",
      REPLY_OWNED == frozenset(["content-length", "date", "server"]))
check("a javascript reply is a static",
      proxy.is_static("/_rsrc/js/gnr.js", [("Content-Type", "text/javascript")]))
check("a stylesheet is a static",
      proxy.is_static("/_rsrc/css/base.css", [("Content-Type", "text/css")]))
check("the favicon is a static whatever it answers",
      proxy.is_static("/favicon.ico", [("Content-Type", "text/html")]))
check("an RPC answer is not a static",
      not proxy.is_static("/invc/index", [("Content-Type", "text/xml")]))
check("a page is not a static",
      not proxy.is_static("/invc/index", [("Content-Type", "text/html")]))

print("\n4. the identifiers rewritten on the way to the shadow")
legacy_page = "aBcDeFgHiJkLmNoPqRsTuv"
bridge_page = "ZyXwVuTsRqPoNmLkJiHgFe"
proxy.identity.learn_page_id(f"page_id:'{legacy_page}'", f"page_id:'{bridge_page}'")
check("the page the bridge minted replaces the one the legacy did",
      proxy.identity.get_adapted(f"/invc/index?_calling_page_id={legacy_page}")
      == f"/invc/index?_calling_page_id={bridge_page}")
check("the body is rewritten too, and comes back as bytes",
      proxy.get_adapted_body(f"page_id={legacy_page}&method=x".encode())
      == f"page_id={bridge_page}&method=x".encode())
check("a body with no identifier in it is unchanged, byte for byte",
      proxy.get_adapted_body(b"user=admin&password=\xc3\xa8")
      == b"user=admin&password=\xc3\xa8")
check("an empty body stays empty", proxy.get_adapted_body(b"") == b"")

print("\n5. the join between the two archives")
drop_archives()
os.environ[RUN_NAME_ENV] = "check run"
legacy = RunArchive(LEGACY_ARCHIVE, run_id="legacy-check",
                    conditions={"stack": "legacy", "sitename": "sandbox"})
bridge = RunArchive(BRIDGE_ARCHIVE, run_id="bridge-check",
                    conditions={"stack": "bridge", "sitename": "sandbox_asgi"})
check("the run name the owner declared is in the archive as data",
      legacy.conditions["run_name"] == "check run")
check("and promoted to a column of the run row, a copy of it",
      legacy.connection.execute("SELECT name FROM run").fetchone()[0] == "check run")
check("both archives of the pair carry the same name",
      bridge.connection.execute("SELECT name FROM run").fetchone()[0] == "check run")

legacy.append_record("http", {"exchange_id": "leg0001", "path": "/invc/index",
                              "ts": "2026-08-25T18:00:00", "status": 200,
                              "duration_ms": 41.0, "rpc_method": None,
                              "req_headers": {TWIN_HEADER: "twin-00001"}})
legacy.append_record("http", {"exchange_id": "leg0002", "path": "/invc/index",
                              "ts": "2026-08-25T18:00:01", "status": 200,
                              "duration_ms": 12.0, "rpc_method": "doLogin",
                              "req_headers": {TWIN_HEADER: "twin-00002"}})
bridge.append_record("http", {"exchange_id": "brg0001", "path": "/invc/index",
                              "ts": "2026-08-25T18:00:02", "status": 200,
                              "duration_ms": 58.0, "rpc_method": "doLogin",
                              "req_headers": {REPLICA_HEADER: "leg0002"}})
proxy = build_proxy()
found = proxy.get_marked_exchange(proxy.reference, TWIN_HEADER, "twin-00002")
check("the legacy exchange is found by the mark the proxy put on it",
      found is not None and found["exchange_id"] == "leg0002")
shadow = proxy.get_marked_exchange(proxy.shadow, REPLICA_HEADER, "leg0002")
check("the bridge exchange is found by the legacy exchange id it replays",
      shadow is not None and shadow["exchange_id"] == "brg0001")
check("a mark nobody sent finds nothing, and says so by waiting no longer",
      proxy.get_marked_exchange(proxy.reference, TWIN_HEADER, "twin-09999") is None)

print("\n6. the cold start, decided exchange by exchange")
proxy = build_proxy()
check("an exchange before the first RPC is not compared",
      proxy.is_cold_start({"exchange_id": "leg0001", "rpc_method": None}))
check("the first RPC itself IS compared",
      not proxy.is_cold_start({"exchange_id": "leg0002", "rpc_method": "doLogin"}))
check("and nothing after it is cold again, RPC or not",
      not proxy.is_cold_start({"exchange_id": "leg0003", "rpc_method": None}))

print("\n7. the stop, and what it leaves standing")
proxy = build_proxy()
proxy.legacy = type("Stack", (), {"archive_path": LEGACY_ARCHIVE})()
proxy.bridge = type("Stack", (), {"archive_path": BRIDGE_ARCHIVE})()
proxy.stop_shadowing("DIVERGENCE: the two register item key sets differ")
check("the divergence is remembered, so nothing else is dispatched to the bridge",
      proxy.divergence is not None)
check("the report is written down, not only printed",
      os.path.exists(proxy.report_path))
report = open(proxy.report_path).read()
check("it names the run, both archives and the difference",
      "check run" in report and LEGACY_ARCHIVE in report
      and BRIDGE_ARCHIVE in report and "key sets differ" in report)
os.remove(proxy.report_path)

drop_archives()
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
