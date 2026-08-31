"""Direct checks of the population and freeze tools, without Docker and without a lab.

The five things that, if wrong, would make this run report a freeze that never
happened:

- a plan whose rest does not outlast the freeze band, so a user still active
  looks frozen;
- the four population counts, where a frozen user would otherwise be counted
  twice — once as frozen and once as unplaced;
- a resident that keeps a thread after leaving the working set, so a "silent"
  user is not silent;
- a freeze certification that accepts ``null``, which means the freeze is OFF;
- the account file, taken on trust.

    python3 test_population_tools.py
"""

import ast
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIO = os.path.abspath(os.path.join(HERE, os.pardir))
BENCHMARKS = os.path.abspath(os.path.join(SCENARIO, os.pardir))
sys.path.insert(0, BENCHMARKS)
sys.path.insert(0, SCENARIO)

import make_population_trace                                                     # noqa: E402
from bench_common.bridge_eyes import BridgeEyes                                  # noqa: E402
from bench_common.stop_guard import StopFlag                                     # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got!r}"
          + ("" if ok else f"  atteso {want!r}"))
    if not ok:
        failures.append(label)


def check_exits(label, call):
    try:
        call()
    except SystemExit:
        print(f"  [ok  ] {label}")
        return
    print(f"  [FAIL] {label}: non si e' fermato")
    failures.append(label)


SHORT = ["--users", "12", "--working-set", "4", "--freeze-minutes", "1",
         "--rest-seconds", "150", "--rest2-seconds", "150", "--wake-spread", "8",
         "--work-seconds", "20", "--rotate-seconds", "60", "--swap-every", "20",
         "--swap-count", "2", "--observe-seconds", "20", "--bursts", "10",
         "--batch", "6", "--batch-settle", "2"]

print("\n== il riposo deve superare la banda di incertezza del freeze ==")
with tempfile.TemporaryDirectory() as sandbox:
    good = os.path.join(sandbox, "buono.json")
    make_population_trace.main(["--out", good] + SHORT)
    check("un riposo di 150s con freeze a 60s va bene", os.path.isfile(good), True)
    check_exits("un riposo di 100s con freeze a 60s viene rifiutato",
                lambda: make_population_trace.main(
                    ["--out", os.path.join(sandbox, "corto.json")]
                    + [arg if arg != "150" else "100" for arg in SHORT]))
    check_exits("un working set piu' grande della popolazione viene rifiutato",
                lambda: make_population_trace.main(
                    ["--out", os.path.join(sandbox, "grosso.json")] + SHORT
                    + ["--working-set", "99"]))

    print("\n== il piano e' lo stesso per le due gambe ==")
    twin = os.path.join(sandbox, "gemello.json")
    other = os.path.join(sandbox, "altro.json")
    make_population_trace.main(["--out", twin] + SHORT)
    make_population_trace.main(["--out", other, "--seed", "7"] + SHORT)
    plan = json.load(open(good))
    check("lo stesso seed produce lo stesso piano", plan == json.load(open(twin)), True)
    check("un seed diverso produce un piano diverso",
          plan == json.load(open(other)), False)
    check("il piano porta tutti gli ingressi", len(plan["entries"]), 12)
    check("il working set ha la taglia dichiarata", len(plan["working_set"]), 4)
    check("il working set non ha ripetizioni",
          len(set(plan["working_set"])), len(plan["working_set"]))
    check("gli scambi entrano ed escono in pari numero",
          all(len(swap["in"]) == len(swap["out"]) for swap in plan["rotation"]), True)
    check("chi esce era nel working set del momento",
          all(swap["out"] and swap["in"] for swap in plan["rotation"]), True)
    check("nessuno scambio riporta dentro chi e' appena uscito",
          all(not (set(swap["in"]) & set(swap["out"])) for swap in plan["rotation"]), True)
    check("il piano registra il file degli account con il suo hash",
          set(plan["accounts"]) >= {"sha256", "rows", "unique"}, True)
    check("e dichiara la banda di incertezza del freeze",
          "freeze_granularity_note" in plan["protocol"], True)

    print("\n== il file degli account non si prende sulla fiducia ==")
    bad = os.path.join(sandbox, "doppi.txt")
    with open(bad, "w") as handle:
        handle.write("loaduser0001\nloaduser0001\nloaduser0002\n")
    check_exits("un file con duplicati viene rifiutato",
                lambda: make_population_trace.main(
                    ["--out", os.path.join(sandbox, "x.json"), "--accounts", bad,
                     "--users", "3", "--working-set", "1", "--freeze-minutes", "1",
                     "--rest-seconds", "150"]))
    leaky = os.path.join(sandbox, "segreti.txt")
    with open(leaky, "w") as handle:
        handle.write("loaduser0001\nloaduser0002:password=abc\nloaduser0003\n")
    check_exits("un file che sembra contenere un segreto viene rifiutato",
                lambda: make_population_trace.main(
                    ["--out", os.path.join(sandbox, "y.json"), "--accounts", leaky,
                     "--users", "3", "--working-set", "1", "--freeze-minutes", "1",
                     "--rest-seconds", "150"]))

print("\n== i quattro conteggi: un congelato non si conta due volte ==")
CENSUS = {
    "user_map": {
        "alice": {"frozen": False}, "bob": {"frozen": True},
        "carol": {"frozen": True}, "dave": {"frozen": False},
        "guest_abc": {"frozen": False},
    },
    "connection_user_map": {"c1": "alice", "c2": "dave", "c3": "guest_abc"},
    "page_connection_map": {"p1": "c1", "p2": "c1", "p3": "c2"},
    "groups": {"pool": {
        "user_worker_map": {"alice": "pool_0001", "bob": None, "carol": None,
                            "dave": None, "guest_abc": "pool_0001"},
        "living_workers": ["pool_0001", "pool_0002"],
        "memory_occupied_percent": 41.5, "memory_accounting": "pss",
    }},
    "workers": {"pool_0001": {"pid": 41, "user_register": {
        "alice": {"state": "active", "last_user_ts": 100.0, "last_rpc_ts": 101.0,
                  "last_refresh_ts": 102.0}}}},
}
eyes = BridgeEyes("http://finto")
population = eyes.population(census=CENSUS)
check("gli autenticati escludono i guest", population["authenticated"], 4)
check("i guest sono contati a parte", population["guest"], 1)
check("i congelati vengono dal census", population["frozen"], 2)
check("i collocati sono quelli con un worker", population["placed"], 1)
check("i NON collocati escludono i congelati", population["unplaced"], 1)
check("i quattro numeri quadrano con gli autenticati",
      population["placed"] + population["frozen"] + population["unplaced"],
      population["authenticated"])
check("i congelati sono nominati", population["frozen_users"], ["bob", "carol"])
check("il conteggio per worker ignora i guest",
      population["per_worker"], {"pool_0001": 1})
check("i worker vivi sono elencati", population["worker_count"], 2)
clocks = eyes.user_clocks(census=CENSUS)
check("gli orologi per utente vengono dal register del worker",
      clocks["alice"]["last_user_ts"], 100.0)
check("e portano lo stato dell'item", clocks["alice"]["state"], "active")

print("\n== il deposito congelato: una misura assente si dichiara assente ==")
deposit = BridgeEyes("http://finto").frozen_deposit()
check("senza container non si inventa uno zero", deposit["available"], False)
check("e si dice perche'", "non dichiarati" in deposit["reason"], True)

print("\n== il residente silenzioso non ha un thread ==")
sys.argv = ["population_probe.py"]
import population_probe                                                          # noqa: E402


class FakeUser:
    def __init__(self):
        self.netloc = "127.0.0.1:9"
        self.headers = {}
        self.username = "loaduser0001"
        self.calls = 0

    def get_call_form(self, lookup, counter):
        return {"lookup": lookup, "counter": counter}


class FakeProbe:
    def __init__(self):
        self.stop_flag = StopFlag()
        self.lookups = ["a", "b"]
        self.phase = "work"
        self.recorded = []

    def send(self, user, body):
        user.calls += 1
        time.sleep(0.01)
        return 200, None, None

    def record_call(self, row):
        self.recorded.append(row)


probe = FakeProbe()
user = FakeUser()
resident = population_probe.Resident(probe, "user_1", user, [0.05, 0.05])
check("un residente nasce senza thread", resident.active, False)
check("e non ha ancora chiamato niente", user.calls, 0)
resident.activate(measure_reentry=True)
time.sleep(0.25)
check("attivato, lavora", user.calls > 0, True)
check("e si dichiara attivo", resident.active, True)
check("la prima chiamata dopo il thaw e' misurata",
      resident.first_call_ms is not None, True)
check("e cosi' l'intero rientro", resident.reentry_ms is not None, True)
check("la prima chiamata e' marcata nel registro",
      probe.recorded[0]["kind"], "first_after_thaw")
check("le successive no", probe.recorded[-1]["kind"], "burst")
calls_before = user.calls
stopped = resident.deactivate(timeout=5)
check("disattivato, il thread e' davvero finito", stopped, True)
check("e non ha piu' un thread", resident.active, False)
time.sleep(0.3)
check("e da quel momento non chiama piu' niente", user.calls, calls_before)

print("\n== il driver non fa ping, e non deve ==")


def executable_code(path):
    """The module's code with every docstring and comment removed.

    The assertion has to look at what the driver DOES, not at what it says: the
    docstring of this driver names ``/_ping`` precisely to explain why it never
    calls it, and a grep on the raw text would read that explanation as a
    violation.
    """
    tree = ast.parse(open(path).read())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            body.pop(0)
    return ast.unparse(tree)


code = executable_code(os.path.join(SCENARIO, "population_probe.py"))
check("nessun /_ping nel driver", "/_ping" in code, False)
check("nessun ping_forever nel driver", "ping_forever" in code, False)
check("nessun _lastUserEventTs mandato dal driver", "_lastUserEventTs" in code, False)
check("nessun _lastRpc mandato dal driver", "_lastRpc" in code, False)
check("nessun GET anonimo sulla radice", "urlopen(self.base + '/')" in code, False)
# Ogni lettura del bridge passa da BridgeEyes: il driver non apre URL da se',
# cosi' un percorso che tocca il sito non puo' entrare di straforo.
check("il driver non apre URL per conto proprio", "urlopen" in code, False)
eyes_code = executable_code(os.path.join(BENCHMARKS, "bench_common", "bridge_eyes.py"))
paths = sorted({literal for literal in re.findall(r"'(/[a-z_/]+)'", eyes_code)})
check("le sole letture HTTP del bridge sono quelle che non toccano il sito",
      paths, ["/_orchestration/apply", "/_orchestration/status",
              "/_server/inspector/census"])

print("\n== il freeze spento non passa per un freeze ==")


class FakeEyes:
    def __init__(self, live):
        self.live = live

    def live_settings(self):
        return {"effective_settings": {"user_idle_freeze_minutes": self.live},
                "generation": 3, "active_profile": None}


class Bare:
    pass


def certify_with(live, expected):
    probe = Bare()
    probe.eyes = FakeEyes(live)
    probe.arguments = Bare()
    probe.arguments.expect_freeze_minutes = expected
    return population_probe.PopulationProbe.certify_freeze(probe)


record = certify_with(5.0, 5.0)
check("un freeze vivo pari all'atteso e' certificato", record["verdict"], "certificato")
try:
    certify_with(None, 5.0)
    check("un freeze null con 5 attesi ferma la corsa", "nessuna eccezione", "InvalidRun")
except population_probe.InvalidRun as failure:
    check("un freeze null con 5 attesi ferma la corsa", "spento" in str(failure), True)
try:
    certify_with(2.0, 5.0)
    check("un freeze diverso dall'atteso ferma la corsa", "nessuna eccezione", "InvalidRun")
except population_probe.InvalidRun as failure:
    check("un freeze diverso dall'atteso ferma la corsa", "2.0" in str(failure), True)

print("\n" + "=" * 50)
if failures:
    print(f"FALLITI {len(failures)}:")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("tutti i controlli passati")
