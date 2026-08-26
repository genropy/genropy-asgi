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
import time

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
BENCH_ROOT = os.path.dirname(os.path.dirname(BENCH_DIR))
sys.path.insert(0, BENCH_DIR)

from replica import REPLICA_HEADER, TraceReader   # noqa: E402
from structural_diff import ReplyShape            # noqa: E402
from run_archive import RUN_NAME_ENV, RunArchive  # noqa: E402
from twin_proxy import (ERROR, HOP_BY_HOP, REPLY_OWNED, TWIN_COOKIE,  # noqa: E402
                        TWIN_HEADER, WARNING, DatabaseCopy, ShadowLeg,
                        TwinInstances, TwinProxy)

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


class Launched:
    """A stack that was never launched, answering the one thing a shadow asks."""

    def __init__(self, archive_path):
        self.archive_path = archive_path


def build_proxy():
    """A proxy whose two stacks are their archives and nothing else."""
    return TwinProxy(instances=None, legacy=Launched(LEGACY_ARCHIVE),
                     bridge=Launched(BRIDGE_ARCHIVE), port=8097,
                     legacy_port=8099, bridge_port=8098, run_name="check run")


def build_reader_pair():
    """The two readers, in THIS thread — what a shadow builds in its own."""
    return TraceReader(LEGACY_ARCHIVE), TraceReader(BRIDGE_ARCHIVE)


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

print("\n4. one shadow per browser, and the identifiers each one rewrites")
proxy.instances = TwinInstances("sandbox")
first, minted = proxy.get_identity({})
check("a browser with no mark gets one, and it travels back to it",
      minted and len(first) == 16)
again, reminted = proxy.get_identity({"Cookie": f"{TWIN_COOKIE}={first}"})
check("a browser that has the mark keeps it", (again, reminted) == (first, False))
check("the mark survives the site's own cookie changing under it — which is what "
      "the login does when it rotates the connection",
      proxy.get_identity({"Cookie": f"sandbox=ROTATED; {TWIN_COOKIE}={first}"})
      == (first, False))
check("neither site ever sees the bench's cookie",
      proxy.get_browser_headers(
          {"Cookie": f"{TWIN_COOKIE}={first}; sandbox=SITE", "Accept": "*/*"})
      == {"Cookie": "sandbox=SITE", "Accept": "*/*"})
check("and a browser carrying only that cookie sends no Cookie header at all",
      "Cookie" not in proxy.get_browser_headers({"Cookie": f"{TWIN_COOKIE}={first}"}))

alice = proxy.get_shadow("alice")
bob = proxy.get_shadow("bob")
check("two browsers get two shadows, each with its own connection and jar",
      alice is not bob and alice.client is not bob.client)
check("the same browser comes back to the same shadow",
      proxy.get_shadow("alice") is alice)

legacy_page = "aBcDeFgHiJkLmNoPqRsTuv"
bridge_page = "ZyXwVuTsRqPoNmLkJiHgFe"
alice.identity_map.learn_page_id(f"page_id:'{legacy_page}'",
                                 f"page_id:'{bridge_page}'")
check("the page the bridge minted replaces the one the legacy did",
      alice.identity_map.get_adapted(f"/invc/index?_calling_page_id={legacy_page}")
      == f"/invc/index?_calling_page_id={bridge_page}")
check("the body is rewritten too, and comes back as bytes",
      proxy.get_adapted_body(alice, f"page_id={legacy_page}&method=x".encode())
      == f"page_id={bridge_page}&method=x".encode())
check("another browser's tokens are NOT rewritten into this one's requests",
      proxy.get_adapted_body(bob, f"page_id={legacy_page}".encode())
      == f"page_id={legacy_page}".encode())
check("a body with no identifier in it is unchanged, byte for byte",
      proxy.get_adapted_body(alice, b"user=admin&password=\xc3\xa8")
      == b"user=admin&password=\xc3\xa8")
check("an empty body stays empty", proxy.get_adapted_body(alice, b"") == b"")

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
reference_reader, shadow_reader = build_reader_pair()
found = proxy.get_marked_exchange(reference_reader, TWIN_HEADER, "twin-00002")
check("the legacy exchange is found by the mark the proxy put on it",
      found is not None and found["exchange_id"] == "leg0002")
replayed = proxy.get_marked_exchange(shadow_reader, REPLICA_HEADER, "leg0002")
check("the bridge exchange is found by the legacy exchange id it replays",
      replayed is not None and replayed["exchange_id"] == "brg0001")
check("a mark nobody sent finds nothing, and says so by waiting no longer",
      proxy.get_marked_exchange(reference_reader, TWIN_HEADER, "twin-09999") is None)

print("\n5b. a static waits for nothing: it is dispatched and not compared")
proxy = build_proxy()


class StubClient:
    """A bridge that answers at once, and remembers what it was told to join on."""

    def __init__(self):
        self.replaying = "untouched"
        self.sent = []

    def send_shadow(self, method, target, headers, body):
        self.sent.append((method, target))
        return 200, b""


proxy.instances = TwinInstances("sandbox")
stub = StubClient()
shadow = proxy.get_shadow("carol")
shadow.client = stub
static_leg = ShadowLeg("GET", "/_rsrc/js/gnr.js", {}, b"", "twin-09999", 200,
                       [("Content-Type", "text/javascript")], b"", "carol")
started = time.time()
proxy.follow(shadow, static_leg)
elapsed = time.time() - started
check("the static reached the bridge", stub.sent == [("GET", "/_rsrc/js/gnr.js")])
check("it carried no join, because nothing will be joined",
      stub.replaying is None)
check("no reference line was waited for — the recorder writes a static as a stub",
      elapsed < 1.0)
check("a static is never a divergence", proxy.divergences == 0)

print("\n6. the cold start, decided exchange by exchange")
proxy = build_proxy()
check("an exchange before the first RPC is not compared",
      proxy.is_cold_start({"exchange_id": "leg0001", "rpc_method": None}))
check("the first RPC itself IS compared",
      not proxy.is_cold_start({"exchange_id": "leg0002", "rpc_method": "doLogin"}))
check("and nothing after it is cold again, RPC or not",
      not proxy.is_cold_start({"exchange_id": "leg0003", "rpc_method": None}))

print("\n7. the two weights, written down and never stopping the run")
proxy = build_proxy()
first = ShadowLeg("POST", "/", {}, b"", "twin-00042", 200, [], b"", "alice")
second = ShadowLeg("POST", "/", {}, b"", "twin-00099", 200, [], b"", "bob")
proxy.record_divergence(first, ERROR, "the replies differ at character 118")
proxy.record_divergence(second, WARNING, "different call at register call 5")
check("each divergence gets a file of its own, numbered and weighed",
      os.path.exists(proxy.get_report_path(1, ERROR))
      and os.path.exists(proxy.get_report_path(2, WARNING)))
report = open(proxy.get_report_path(1, ERROR)).read()
check("the report opens on the weight", report.startswith("ERROR #1"))
check("it names the call by the mark the comparison is written in",
      "twin-00042" in report)
check("and the browser it came from, so several users stay told apart",
      "alice" in report)
check("it names the run and both archives",
      "check run" in report and LEGACY_ARCHIVE in report and BRIDGE_ARCHIVE in report)
check("the second is a finding of its own, not a sequel to the first",
      open(proxy.get_report_path(2, WARNING)).read().startswith("WARNING #2"))
check("the summary counts the two weights apart",
      "1 ERROR, 1 WARNING" in proxy.summary)
check("and lists the errors before the warnings",
      proxy.summary.index("twin-00042") < proxy.summary.index("twin-00099"))
os.remove(proxy.get_report_path(1, ERROR))
os.remove(proxy.get_report_path(2, WARNING))

print("\n8. what the browser would have seen: the reply, whole")
identical = ReplyShape("<GenRoBag><result>page_id:'aBcDeFgHiJkLmNoPqRsTuv'</result>"
                       "<ts>2026-08-26T05:57:08</ts></GenRoBag>")
other_ids = ReplyShape("<GenRoBag><result>page_id:'ZyXwVuTsRqPoNmLkJiHgFe'</result>"
                       "<ts>2026-08-26T06:12:44</ts></GenRoBag>")
check("two replies differing only in minted identifiers and clock are the same reply",
      identical.get_difference(other_ids) is None)
changed = ReplyShape("<GenRoBag><result>page_id:'ZyXwVuTsRqPoNmLkJiHgFe'</result>"
                     "<ts>2026-08-26T06:12:44</ts><dataChanges/></GenRoBag>")
difference = identical.get_difference(changed)
check("a piggybacked datachange one stack sent and the other did not IS a difference",
      difference is not None and "dataChanges" in difference)
check("the report says where, and how long each reply was",
      difference.startswith("the replies differ at character"))
check("a shorter reply is a difference too",
      identical.get_difference(ReplyShape("<GenRoBag/>")) is not None)

drop_archives()
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
