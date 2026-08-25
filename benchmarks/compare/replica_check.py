"""Isolation checks for the replica: the trace it reads, the exchanges it leaves
out, the identifiers it rewrites, the header it stamps, and the parity gate that
stops it before it starts.

No site, no server, no site database: a throwaway archive and two throwaway
source trees under `temp/`. The replay itself is not exercised here — driving a
live stack is what the phase's own smoke does, and a mocked HTTP server would
assert the mock. What IS asserted is everything that decides WHAT goes on the
wire, which is where a replica gets a comparison wrong.

It runs on the bridge interpreter, because `genropy_parity_check` imports `gnr`
to see which tree this process resolves it from.

Run: python benchmarks/compare/replica_check.py
"""

import os
import shutil
import sys

from genropy_parity_check import GNR_FOLDER_ENV, GenropyParity
from replica import IdentityMap, Replica, ReplicaClient, TraceReader
from run_archive import RunArchive

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMP = os.path.join(REPO_ROOT, "temp")
ARCHIVE = os.path.join(TEMP, "replica_check.sqlite")
TREES = os.path.join(TEMP, "replica_check_trees")

CONDITIONS = {"stack": "legacy", "sitename": "test_invoice_pg_legacy"}

FRAME_HTML = "<html>var g = {page_id:'AAAAAAAAAAAAAAAAAAAAAA',baseUrl:'/'}</html>"
TARGET_FRAME_HTML = "<html>var g = {page_id:'BBBBBBBBBBBBBBBBBBBBBB',baseUrl:'/'}</html>"
IFRAME_HTML = "<html>var g = {page_id:'CCCCCCCCCCCCCCCCCCCCCC',baseUrl:'/'}</html>"
TARGET_IFRAME_HTML = "<html>var g = {page_id:'DDDDDDDDDDDDDDDDDDDDDD',baseUrl:'/'}</html>"

failures = []


def check(label, condition):
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


def drop_archive():
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(ARCHIVE + suffix):
            os.remove(ARCHIVE + suffix)


def write_tree(root, files):
    if os.path.exists(root):
        shutil.rmtree(root)
    for name, content in files.items():
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as target:
            target.write(content)


# 1. the trace: every HTTP line, oldest first, whatever order they were written
drop_archive()
archive = RunArchive(ARCHIVE, run_id="replica-check", conditions=CONDITIONS)
archive.append_record("http", {"exchange_id": "e3", "ts": "2026-08-23T10:00:03",
                               "method": "POST", "path": "/", "rpc_method": "main",
                               "status": 200, "form": {"page_id": "AAAAAAAAAAAAAAAAAAAAAA"}})
archive.append_record("http", {"exchange_id": "e1", "ts": "2026-08-23T10:00:01",
                               "method": "GET", "path": "/", "status": 200,
                               "resp_body": FRAME_HTML})
archive.append_record("http", {"exchange_id": "e2", "ts": "2026-08-23T10:00:02",
                               "method": "GET", "path": "/_rsrc/common/public.css",
                               "status": 200, "filtered": "static"})
archive.append_record("http", {"exchange_id": "e4", "ts": "2026-08-23T10:00:04",
                               "method": "POST", "path": "/_ping", "status": 200,
                               "filtered": "empty_ping"})
archive.append_record("http", {"exchange_id": "e5", "ts": "2026-08-23T10:00:05",
                               "method": "POST", "path": "/_ping", "status": 412,
                               "resp_body": "<GenRoBag><dataChanges/></GenRoBag>"})
archive.append_record("http", {"exchange_id": "e6", "ts": "2026-08-23T10:00:06.000",
                               "method": "POST", "path": "/", "rpc_method": "doLogin",
                               "status": 200, "duration_ms": 40.0, "form": {},
                               "req_headers": {"Cookie": "site=old"}})
archive.append_record("http", {"exchange_id": "e7", "ts": "2026-08-23T10:00:06.020",
                               "method": "POST", "path": "/", "rpc_method": "doLogin",
                               "status": 400, "duration_ms": 7.0, "form": {},
                               "req_headers": {"Cookie": "site=old"},
                               "resp_body": "ERROR REASON : The connection is not "
                                            "longer valid; PATH_INFO='/'"})
archive.append_record("http", {"exchange_id": "e8", "ts": "2026-08-23T10:00:09.000",
                               "method": "POST", "path": "/", "rpc_method": "main",
                               "status": 400, "duration_ms": 5.0, "form": {},
                               "req_headers": {"Cookie": "site=stale"},
                               "resp_body": "ERROR REASON : The connection is not "
                                            "longer valid; PATH_INFO='/'"})
archive.append_record("register", {"exchange_id": "e1", "verb": "getItem",
                                   "ts": "2026-08-23T10:00:01"})

trace = TraceReader(ARCHIVE)
check("the trace reads only HTTP lines, never the register ones",
      len(trace.records) == 8)
check("the trace reads them oldest first, not in the order they were written",
      [record["exchange_id"] for record in trace.records]
      == ["e1", "e2", "e3", "e4", "e5", "e6", "e7", "e8"])

# 2. what is left out, and why: the two declared rules
check("a static is left out", trace.get_skip_reason(trace.records[1]) == "static")
check("an empty ping is left out", trace.get_skip_reason(trace.records[3]) == "ping")
check("a ping that carried datachanges is left out too — the rule is the path",
      trace.get_skip_reason(trace.records[4]) == "ping")
check("everything else is replayed", trace.get_skip_reason(trace.records[0]) is None)
check("the exchanges to replay are the trace minus the skipped ones",
      [record["exchange_id"] for record in trace.exchanges] == ["e1", "e3", "e6", "e7", "e8"])

# 2b. the race of the reference: a status only concurrency could have produced
by_id = {record["exchange_id"]: record for record in trace.records}
check("an exchange overlapping an earlier one on the same pre-rotation cookie, "
      "answered with the site's connection-rotated error, is a race of the reference",
      "doLogin" in (trace.get_race_reason(by_id["e7"]) or ""))
check("the race names the exchange that rotated the connection under it",
      trace.get_overlapped_exchange(by_id["e7"])["exchange_id"] == "e6")
check("the same reply with no overlap is NOT a race — a stale tab makes one too, "
      "and that one replays",
      trace.get_race_reason(by_id["e8"]) is None)
check("an exchange that answered normally is no race at all",
      trace.get_race_reason(by_id["e6"]) is None)

# 3. identifiers: learned from the HTML, rewritten wherever they appear
identity = IdentityMap()
check("no page id is invented before one is seen",
      identity.get_adapted("page_id=AAAAAAAAAAAAAAAAAAAAAA")
      == "page_id=AAAAAAAAAAAAAAAAAAAAAA")
identity.learn_page_id(FRAME_HTML, TARGET_FRAME_HTML)
check("the frame page of the trace is paired with the one the target minted",
      identity.tokens == {"AAAAAAAAAAAAAAAAAAAAAA": "BBBBBBBBBBBBBBBBBBBBBB"})
identity.learn_page_id(IFRAME_HTML, TARGET_IFRAME_HTML)
check("a second page is learned without losing the first",
      identity.tokens == {"AAAAAAAAAAAAAAAAAAAAAA": "BBBBBBBBBBBBBBBBBBBBBB",
                          "CCCCCCCCCCCCCCCCCCCCCC": "DDDDDDDDDDDDDDDDDDDDDD"})
identity.learn_page_id(FRAME_HTML, "<html>no page here</html>")
check("a reply with no page id teaches nothing", len(identity.tokens) == 2)
check("a token nobody minted is left alone",
      identity.get_adapted("pkey=beDqiRjkNXe_LHB5ySyYdw")
      == "pkey=beDqiRjkNXe_LHB5ySyYdw")

PIN = os.path.join(TREES, "pin", "gnrpy", "gnr")
FROZEN = os.path.join(TREES, "frozen")
CONFIG = os.path.join(TREES, "config")


def write_config(home):
    os.makedirs(CONFIG, exist_ok=True)
    with open(os.path.join(CONFIG, "environment.xml"), "w") as target:
        target.write('<?xml version="1.0"?><GenRoBag><environment>'
                     f'<gnrhome value="{home}"/></environment></GenRoBag>')


def pinned_parity():
    """A parity whose pinned tree is read the way a run reads it: from the config."""
    os.environ[GNR_FOLDER_ENV] = CONFIG
    return GenropyParity(legacy_root=FROZEN, bridge_root=PIN)


write_tree(PIN, {"__init__.py": ""})
write_tree(FROZEN, {"__init__.py": ""})
write_config(os.path.join(TREES, "pin"))

replica = Replica(trace, "127.0.0.1", 8099, parity=pinned_parity())
replica.identity = identity
check("the identifiers inside a form value are rewritten",
      replica.get_adapted_form({"form": {"page_id": "AAAAAAAAAAAAAAAAAAAAAA",
                                         "callcounter": "7"}})
      == {"page_id": "BBBBBBBBBBBBBBBBBBBBBB", "callcounter": "7"})
check("a form value that is a list is rewritten item by item",
      replica.get_adapted_form({"form": {"ids": ["AAAAAAAAAAAAAAAAAAAAAA", "x"]}})
      == {"ids": ["BBBBBBBBBBBBBBBBBBBBBB", "x"]})
check("the identifiers inside a query string are rewritten too — the TH page "
      "carries _calling_page_id there",
      replica.get_adapted_path({"path": "/sys/thpage/invc/customer",
                                "query": "th_from_package=invc&_calling_page_id="
                                         "AAAAAAAAAAAAAAAAAAAAAA"})
      == "/sys/thpage/invc/customer?th_from_package=invc&"
         "_calling_page_id=BBBBBBBBBBBBBBBBBBBBBB")
check("a path with no query string is left as it is",
      replica.get_adapted_path({"path": "/"}) == "/")

# 4. the pairing header: what makes the replica run joinable to the reference one
client = ReplicaClient("127.0.0.1", 8099)
check("a client that is not replaying stamps nothing",
      "X-Bench-Replica-Of" not in client._headers())
client.replaying = "e3"
check("a client replaying an exchange names it in the request header",
      client._headers()["X-Bench-Replica-Of"] == "e3")
client.cookies["spa_connection_id"] = "cid"
check("the cookie jar still rides on the same request",
      client._headers()["Cookie"] == "spa_connection_id=cid")

# 5. the pin: which tree the bench declared, and whether this process is on it
check("the pinned tree is read from the bench's own environment.xml",
      pinned_parity().pinned_root == PIN)
os.environ.pop(GNR_FOLDER_ENV, None)
try:
    GenropyParity(legacy_root=FROZEN, bridge_root=PIN).pinned_root
    unset = None
except RuntimeError as exc:
    unset = str(exc)
check("with no configuration folder the bench cannot say what it pinned, and says so",
      unset is not None and GNR_FOLDER_ENV in unset)

parity = pinned_parity()
check("an interpreter importing the pinned tree is on the pin", parity.bridge_on_pin)
off_pin = GenropyParity(legacy_root=FROZEN, bridge_root=FROZEN)
check("an interpreter importing anything else is not on the pin",
      not off_pin.bridge_on_pin and not off_pin.aligned)
check("the report of a bridge off the pin names PYTHONPATH as the remedy",
      "PYTHONPATH" in off_pin.report)

# 6. the frozen copy: same content as the pin, or the legacy stack is not on it
same = {"__init__.py": "", "web/gnrwsgisite.py": "one\ntwo\n",
        "web/__pycache__/gnrwsgisite.pyc": "binary junk",
        "web/notes.txt": "not python"}
write_tree(PIN, same)
write_tree(FROZEN, same)
check("a frozen copy identical to the pin is in parity", parity.aligned)
check("the report of an aligned bench says both stacks run the pinned genropy",
      "both stacks run the pinned genropy" in parity.report)

write_tree(FROZEN, dict(same, **{"web/gnrwsgisite.py": "one\nTWO\n"}))
check("a differing file breaks parity", not parity.aligned)
check("the report NAMES the differing file", "web/gnrwsgisite.py" in parity.report)
check("the report carries the re-freeze remedy, not only the verdict",
      "legacy_venv" in parity.report)
check("the difference is reported as differing, not as missing",
      parity.differences == (["web/gnrwsgisite.py"], [], []))

write_tree(FROZEN, dict(same, **{"web/extra.py": "x"}))
check("a file only the frozen copy carries breaks parity too",
      parity.differences == ([], [], ["web/extra.py"]))

write_tree(PIN, dict(same, **{"resources/adm/menu.py": "packaged copy",
                              "projects/demo/main.py": "packaged copy",
                              "webtools/tool.py": "packaged copy"}))
write_tree(FROZEN, same)
check("the subtrees the wheel copies but no runtime reads are not compared",
      parity.aligned)

write_tree(PIN, dict(same, **{"web/resources/inner.py": "deep, and real code"}))
check("a subtree of the same name deeper down IS compared — only the top level "
      "is packaging",
      parity.differences == ([], ["web/resources/inner.py"], []))

# 7. the refusal: the replica never reaches the wire while the bench is not pinned
write_tree(PIN, dict(same, **{"web/gnrwsgisite.py": "one\nTWO\n"}))
write_tree(FROZEN, same)
refused = Replica(trace, "127.0.0.1", 8099, parity=parity)
try:
    refused.run()
    raised = None
except SystemExit as exc:
    raised = str(exc)
check("the replica refuses to run while the two stacks differ", raised is not None)
check("the refusal names the file and the remedy",
      raised is not None and "web/gnrwsgisite.py" in raised and "legacy_venv" in raised)

shutil.rmtree(TREES)
drop_archive()
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
