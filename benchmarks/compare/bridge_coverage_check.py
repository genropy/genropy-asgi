"""Coverage and isolation checks for the bridge side of the bench.

The legacy recorder is a wrapper that catches every attribute, so it cannot
miss a command: whatever the client grows, the wrapper records. The bridge
recorder is a mixin over an explicit list of verbs, which CAN silently fall
behind the client it shadows. This is the tripwire for that, in the spirit of
the anti-drift check that already guards the daemon contract — and it also
guards the second copy this side needs: the bench recipe, transcribed from the
one the package ships with one line changed.

What it asserts:

1. the daemon override is the one in force, so the checks below look at the
   bridge's client and not at the legacy Pyro one;
2. every public command the client declares is recorded, and nothing is
   recorded that the client does not declare;
3. every recorded verb really is an override on the recording client, bound to
   the parent implementation it shadows;
4. the store surface the recorder reads as properties is the store's own;
5. the bench recipe and the shipped recipe build the SAME pool, except for the
   worker class;
6. the recorder writes one line for the call the site made and none for the
   calls the client makes on itself, and hands back a wrapped store.

No site, no server, no database: a throwaway archive in `temp/` and the real
client on a stub site. It runs on the bridge's own interpreter, where genropy,
genro-asgi and genropy-asgi are installed.

Run, from the repository root:

  GNR_DAEMON_PROVIDER=genropy-asgi PYTHONPATH=benchmarks/compare \
      python benchmarks/compare/bridge_coverage_check.py
"""

import inspect
import json
import os
import sys
from types import SimpleNamespace

from genro_asgi.config.handler import ConfigurationHandler
from gnr.web import gnrwsgisite
from gnr.web.daemon.siteregister_client import SiteRegisterClient

from bridge_recipe import RECORDING_WORKER
from register_recorder import STORE_READ_PROPERTIES, ServerStore, StoreRecorder
from register_recorder_mixin import RECORDED_VERBS, RecordedVerb, RecordingRegisterClient
from run_archive import RunArchive

BENCH_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ARCHIVE = os.path.join(BENCH_ROOT, "temp", "bridge_coverage_check.sqlite")

SHIPPED_RECIPE = os.path.join(BENCH_ROOT, "src", "genropy_asgi", "spa", "config.py")
BENCH_RECIPE = os.path.join(BENCH_ROOT, "benchmarks", "compare", "bridge_recipe.py")

CONDITIONS = {"stack": "bridge", "sitename": "test_invoice_pg"}

EXCHANGE = "0123456789abcdef"

failures = []


def check(label, condition):
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


def public_methods(client_class):
    return {name for name, value in vars(client_class).items()
            if not name.startswith("_") and inspect.isroutine(value)}


def drop_archive():
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(ARCHIVE + suffix):
            os.remove(ARCHIVE + suffix)


def fresh_client():
    """The real recording client on a stub site carrying one exchange header."""
    drop_archive()
    archive = RunArchive(ARCHIVE, run_id="bridge-check", conditions=CONDITIONS)
    request = SimpleNamespace(headers={"X-Bench-Exchange-Id": EXCHANGE})
    site = SimpleNamespace(currentRequest=request, spa_worker=None)
    return RecordingRegisterClient(site, archive=archive), archive


def lines(archive):
    return [json.loads(row[0]) for row in archive.connection.execute(
        "SELECT line FROM record ORDER BY id").fetchall()]


def built_pool(recipe_path):
    """The pool the recipe declares, read back through the runtime's own door."""
    return ConfigurationHandler(recipe_path).group_kwargs("site")


# 1. one client class in the process, and it is the bridge's
#
# The daemon override aliases genropy_asgi.siteregister as gnr.web.daemon, and a
# module reached under BOTH dotted names can be executed twice: the same file
# then yields two distinct classes, and an isinstance between them is False.
# Measured here on 2026-08-24 — importing the client by its own package name
# after the alias had loaded it produced a second copy, and the store handed
# back by one was invisible to the other. Every bench module therefore imports
# the client the way the SITE imports it, and this is where that is pinned.
check("the daemon override is in force (GNR_DAEMON_PROVIDER=genropy-asgi)",
      SiteRegisterClient.__name__ == "GenropyRegisterClient")
check("the site's own module holds the very class the mixin subclasses",
      gnrwsgisite.SiteRegisterClient is SiteRegisterClient)
check("the store the recorder tests for is the one the client makes",
      ServerStore is sys.modules[SiteRegisterClient.__module__].ServerStore)

# 2. the recorded surface IS the client's surface — the tripwire
declared = public_methods(SiteRegisterClient)
recorded = set(RECORDED_VERBS)
check(f"every command the client declares is recorded ({len(declared)} of them)",
      not declared - recorded)
if declared - recorded:
    print(f"     the mixin has fallen behind: {sorted(declared - recorded)}")
check("nothing is recorded that the client does not declare",
      not recorded - declared)
if recorded - declared:
    print(f"     the mixin shadows names that are gone: {sorted(recorded - declared)}")
check("the tuple has no duplicates", len(RECORDED_VERBS) == len(recorded))

# 3. each recorded verb is an override bound to the parent it shadows
check("the recording client is a real subclass of the bridge's client",
      issubclass(RecordingRegisterClient, SiteRegisterClient))
overrides = {verb: vars(RecordingRegisterClient).get(verb) for verb in RECORDED_VERBS}
check("every verb is an override on the recording client",
      all(isinstance(value, RecordedVerb) for value in overrides.values()))
check("every override delegates to the parent's own implementation",
      all(value.parent_method is getattr(SiteRegisterClient, verb)
          for verb, value in overrides.items()))

# 4. the store properties the recorder reads by name are the store's own
store_properties = {name for name, value in vars(ServerStore).items()
                    if not name.startswith("_") and isinstance(value, property)}
check("the store's read properties are the ones the recorder reads as calls",
      store_properties == set(STORE_READ_PROPERTIES))

# 5. the bench recipe differs from the shipped one in the worker class alone
shipped, bench = built_pool(SHIPPED_RECIPE), built_pool(BENCH_RECIPE)
check("both recipes declare the same single pool", set(shipped) == set(bench) == {"pool"})
check("the bench recipe names the recording worker",
      bench["pool"].get("worker_class") == RECORDING_WORKER)
difference = {key for key in set(shipped["pool"]) | set(bench["pool"])
              if shipped["pool"].get(key) != bench["pool"].get(key)}
check("nothing else in the pool differs — the transcription has not drifted",
      difference == {"worker_class"})
if difference != {"worker_class"}:
    print(f"     the copy has drifted on: {sorted(difference)}")

# 6. one line per call the SITE made, and none for the client's own inner calls
client, archive = fresh_client()
client.dump()
written = lines(archive)
check("a command the site called leaves exactly one line", len(written) == 1)
check("the line names the verb, on the client surface",
      written[0].get("verb") == "dump" and written[0].get("surface") == "client")
check("the line carries the exchange read from the request header",
      written[0].get("exchange_id") == EXCHANGE)
check("the in-process call counts as one, with no wire error",
      written[0].get("wire_calls") == 1 and written[0].get("wire_error") is None)

client, archive = fresh_client()
client.recording.perform_recorded_call(client.dump, "outer_command", "client", {}, (), {})
written = lines(archive)
check("a command the client calls on itself leaves no line of its own",
      [line.get("verb") for line in written] == ["outer_command"])

client, archive = fresh_client()
store = client.pageStore("page_0001")
check("a store handed back comes wrapped", isinstance(store, StoreRecorder))
with store:
    pass
written = lines(archive)
check("the store's own conversation is recorded, its inner client calls are not",
      [line.get("verb") for line in written] == ["pageStore", "__enter__", "__exit__"])
check("a store line names the register and the item it happened on",
      all(line.get("register_name") == "page"
          and line.get("register_item_id") == "page_0001"
          for line in written[1:]))
check("the ordinals within the exchange are unbroken",
      [line.get("ordinal") for line in written] == [1, 2, 3])

drop_archive()
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
