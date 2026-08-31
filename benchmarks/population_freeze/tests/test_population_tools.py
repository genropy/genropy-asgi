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
import types
import argparse
import inspect
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


SHORT = ["--users", "40", "--working-set", "10", "--max-active", "40",
         "--freeze-minutes", "1", "--rest-seconds", "150",
         "--baseline-seconds", "20", "--settle-seconds", "5",
         "--measure-seconds", "15", "--observe-seconds", "20",
         "--batch", "10", "--batch-settle", "2"]

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
    check("il piano porta tutti gli ingressi", len(plan["entries"]), 40)
    check("il working set ha la taglia dichiarata", len(plan["working_set"]), 10)
    check("il working set non ha ripetizioni",
          len(set(plan["working_set"])), len(plan["working_set"]))
    check("i gradini sono gli stessi nelle due copie",
          plan["steps"], json.load(open(twin))["steps"])
    check("e un seed diverso risveglia altri utenti",
          plan["steps"] == json.load(open(other))["steps"], False)
    check("ogni gradino ha la sua popolazione bersaglio",
          all(step["target_active"] for step in plan["steps"]), True)
    check("le popolazioni bersaglio sono crescenti",
          [s["target_active"] for s in plan["steps"]]
          == sorted(s["target_active"] for s in plan["steps"]), True)
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
        # Il periodo lo decide la sonda, non il residente: qui e' cortissimo per
        # non far durare il test, e resta l'unica cosa che il finto dichiara.
        self.request_period = 0.05

    def send(self, user, body):
        user.calls += 1
        time.sleep(0.01)
        return 200, None, None

    def record_call(self, row):
        self.recorded.append(row)


probe = FakeProbe()
user = FakeUser()
resident = population_probe.Resident(probe, "user_1", user)
check("un residente nasce senza thread", resident.active, False)
check("e non ha ancora chiamato niente", user.calls, 0)
resident.activate(measure_reentry=True)
time.sleep(0.25)
check("attivato, lavora", user.calls > 0, True)
check("e a un periodo di 0,05s ha fatto piu' di una chiamata", user.calls > 1, True)
check("e si dichiara attivo", resident.active, True)
check("la prima chiamata dopo il thaw e' misurata",
      resident.first_call_ms is not None, True)
check("e cosi' l'intero rientro", resident.reentry_ms is not None, True)
check("la prima chiamata e' marcata nel registro",
      probe.recorded[0]["kind"], "first_after_thaw")
check("le successive no", probe.recorded[-1]["kind"], "call")
check("ogni riga porta la sua lateness",
      all("lateness_s" in row for row in probe.recorded), True)
check("la prima non e' in ritardo", probe.recorded[0]["lateness_s"] < 0.05, True)
stopped = resident.deactivate(timeout=5)
check("disattivato, il thread e' davvero finito", stopped, True)
check("e non ha piu' un thread", resident.active, False)
# Il conteggio si prende DOPO il join, non prima: una chiamata gia' in volo quando
# arriva la richiesta di uscita si completa, ed e' giusto che si completi. Cio' che
# deve valere e' che da quel momento non ne parta nessun'altra.
calls_after_stop = user.calls
time.sleep(0.3)
check("e da quel momento non chiama piu' niente", user.calls, calls_after_stop)

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

print("\n== i conteggi dopo il riposo: bloccanti, e diversi per i due stack ==")


class EyesFinti:
    def __init__(self, population, deposit=None, clocks=None):
        self.pop = population
        self.dep = deposit if deposit is not None else {"available": True, "bytes": 1234,
                                                       "user_folders": 1920, "pickles": 1920}
        self.clocks = clocks or {}

    def population(self):
        return self.pop

    def frozen_deposit(self):
        return self.dep

    def user_clocks(self):
        return self.clocks


def counts_probe(stack, population=None, deposit=None, active=80, residents=2000,
                 users=2000, working=80):
    probe = population_probe.PopulationProbe.__new__(population_probe.PopulationProbe)
    probe.arguments = argparse.Namespace(stack=stack)
    probe.eyes = None if stack == "legacy" else EyesFinti(population or {}, deposit)
    probe.plan = {"users": users}
    probe.initial_working_set = {f"user_{i + 1}" for i in range(working)}
    probe.phase_log = []
    # active_users conta i residenti col thread vivo: qui si finge il conteggio.
    probe.residents = {}
    for i in range(residents):
        finto = types.SimpleNamespace(active=(i < active))
        probe.residents[f"user_{i + 1}"] = finto
    return probe


SANA = {"authenticated": 2000, "placed": 80, "frozen": 1920, "unplaced": 0,
        "guest": 0, "worker_count": 6, "per_worker": {"pool_0001": 80}}

record = counts_probe("bridge", SANA).certify_population_after_rest()
check("bridge sano: nessun problema", record["problemi"], [])
check("gli attesi congelati sono 1920", record["expected_frozen"], 1920)
check("e il deposito e' leggibile", record["frozen_store"]["available"], True)

record = counts_probe("legacy").certify_population_after_rest()
check("legacy: nessun freezer per costruzione",
      "nessun freezer" in record["verdict"], True)
check("e zero congelati", record["frozen"], 0)

print("\n  -- cio' che deve bloccare --")
for guasto, atteso in (
        ({"authenticated": 1999}, "autenticati"),
        ({"placed": 80, "frozen": 1900, "unplaced": 0, "authenticated": 2000},
         "conteggi incoerenti"),
        ({"guest": 5}, "guest"),
        ({"frozen": 1000}, "congelati invece di circa"),
        ({"placed": 40, "frozen": 1960}, "collocati")):
    pop = dict(SANA, **guasto)
    try:
        counts_probe("bridge", pop).certify_population_after_rest()
        check(f"    {atteso} deve bloccare", "non ha sollevato", "InvalidRun")
    except population_probe.InvalidRun as failure:
        check(f"    {atteso} blocca", atteso in str(failure), True)

try:
    counts_probe("bridge", SANA, deposit={"available": False,
                                          "reason": "path non dichiarato"}
                 ).certify_population_after_rest()
    check("    un deposito illeggibile deve bloccare", "non ha sollevato", "InvalidRun")
except population_probe.InvalidRun as failure:
    check("    un deposito illeggibile blocca",
          "deposito congelato non leggibile" in str(failure), True)

try:
    counts_probe("bridge", SANA, active=70).certify_population_after_rest()
    check("    settanta attivi invece di ottanta deve bloccare",
          "non ha sollevato", "InvalidRun")
except population_probe.InvalidRun as failure:
    check("    una popolazione attiva sbagliata blocca",
          "utenti attivi invece di" in str(failure), True)

try:
    counts_probe("legacy", residents=1999).certify_population_after_rest()
    check("    e sul legacy un residente mancante blocca",
          "non ha sollevato", "InvalidRun")
except population_probe.InvalidRun as failure:
    check("    un residente mancante blocca", "residenti invece di" in str(failure), True)

print("\n  -- una tolleranza dichiarata, non un ordine di grandezza --")
check("1920 congelati esatti passano",
      counts_probe("bridge", SANA).certify_population_after_rest()["problemi"], [])
check("1850 passano: dentro il 5%",
      counts_probe("bridge", dict(SANA, frozen=1850, unplaced=70))
      .certify_population_after_rest()["problemi"], [])

print("\n== il memory stop raggiunge il driver in ogni fase ==")
# Ogni fase attende attraverso la bandiera, non con time.sleep: e' cio' che fa
# arrivare uno stop dentro un riposo di sette minuti o dentro un gradino.
sorgente = inspect.getsource(population_probe.PopulationProbe)
for metodo, atteso in (("rest", "self.stop_flag.wait"),
                       ("measure_window", "self.stop_flag.wait"),
                       ("wake_group", "self.stop_flag.wait"),
                       ("run_step", "self.stop_flag.wait"),
                       ("hold_after_stop", "self.stop_flag.wait"),
                       ("populate", "self.stop_flag.raise_if_stopped"),
                       ("ramp", "self.stop_flag.raise_if_stopped")):
    codice = inspect.getsource(getattr(population_probe.PopulationProbe, metodo))
    check(f"  {metodo} passa dalla bandiera", atteso in codice, True)
check("nessuna fase dorme con time.sleep",
      "time.sleep" in sorgente, False)
check("e il residente attende sull evento di uscita, non su un sonno",
      "self.leave.wait" in inspect.getsource(population_probe.Resident.work_loop), True)

print("\n== la rampa: gradini di dieci, monotona, con un tetto ==")
with tempfile.TemporaryDirectory() as sandbox:
    path = os.path.join(sandbox, "rampa.json")
    make_population_trace.main(["--out", path] + SHORT)
    plan = json.load(open(path))
    steps = plan["steps"]
    check("il piano dichiara la rampa", plan["kind"], "population_ramp_plan")
    check("tre gradini da 10 fino al tetto di 40", len(steps), 3)
    check("le popolazioni salgono di dieci",
          [s["target_active"] for s in steps], [20, 30, 40])
    check("ogni gradino risveglia dieci utenti",
          sorted({len(s["wake"]) for s in steps}), [10])
    woken = [label for s in steps for label in s["wake"]]
    check("nessun utente risvegliato due volte", len(woken), len(set(woken)))
    check("e nessuno di loro e' nel working set iniziale",
          set(woken) & set(plan["working_set"]), set())
    check("il tetto non viene superato", steps[-1]["target_active"] <= 40, True)
    check("un gradino porta tutto cio' che serve a giocarlo",
          sorted(set(steps[0])),
          sorted(["measure_seconds", "settle_seconds", "step", "target_active",
                  "thaw_seconds", "wake", "wake_gap_s"]))
    check("il risveglio e' uno al secondo", steps[0]["wake_gap_s"], 1.0)
    check("dieci utenti fanno dieci secondi di risveglio", steps[0]["thaw_seconds"], 10.0)
    print("\n  -- il tetto e' un tetto, non un obiettivo --")
    basso = os.path.join(sandbox, "basso.json")
    make_population_trace.main(["--out", basso] + SHORT[:4] + ["--max-active", "25"]
                               + SHORT[6:])
    check("con il tetto a 25 i gradini si fermano prima",
          [s["target_active"] for s in json.load(open(basso))["steps"]], [20])
    print("\n  -- il piano porta cio' che il mandato chiede --")
    for chiave in ("seed", "users", "working_set", "steps", "max_active",
                   "group_size", "steps_declared"):
        check(f"    {chiave}", chiave in plan, True)
    for chiave in ("baseline_seconds", "hold_after_stop_seconds", "admission",
                   "generator", "login_attempts_max", "login_retry_seconds"):
        check(f"    protocol.{chiave}", chiave in plan["protocol"], True)

print("\n== il verdetto sul generatore: e' il driver o lo stack? ==")
RULES = {"started_ratio_min": 0.99, "lateness_drift_limit_s": 5.0,
         "server_fast_p95_ms": 500.0}


def verdict(started_ratio, drift, p95):
    probe = population_probe.PopulationProbe.__new__(population_probe.PopulationProbe)
    probe.protocol = {"generator": RULES}
    return probe.generator_verdict({"started_ratio": started_ratio,
                                    "late_drift_s": drift, "p95_ms": p95})


check("tutto avviato, nessuna deriva, server lento: NON e' il generatore",
      verdict(1.0, 0.1, 1400.0)["limit"], False)
check("avviato il 95%: e' il generatore",
      verdict(0.95, 0.0, 100.0)["limit"], True)
check("e lo dice nel motivo",
      "sotto il 99%" in verdict(0.95, 0.0, 100.0)["reason"], True)
check("deriva grande MA server lento: NON e' il generatore",
      verdict(1.0, 12.0, 1400.0)["limit"], False)
check("deriva grande e server veloce: E' il generatore",
      verdict(1.0, 12.0, 120.0)["limit"], True)
check("e lo dice in chiaro",
      "e' il driver, non lo stack" in verdict(1.0, 12.0, 120.0)["reason"], True)
check("deriva sotto il limite e server veloce: nessun verdetto",
      verdict(1.0, 2.0, 120.0)["limit"], False)
check("esattamente al 99% non e' un limite", verdict(0.99, 0.0, 100.0)["limit"], False)

print("\n== la classificazione: una parola, con una gerarchia dichiarata ==")


def classify(memory=None, structural=None, stop=None):
    probe = population_probe.PopulationProbe.__new__(population_probe.PopulationProbe)
    probe.memory_verdict = memory
    probe.structural_failure = structural
    probe.stop_reason = stop
    return probe.classification


VUOTO = {"memory_stop": False, "pressure_delta": {"oom": 0, "max": 0}}
check("un memory stop vince su tutto",
      classify(memory={"memory_stop": True, "pressure_delta": {"oom": 0}},
               structural="qualcosa", stop="CAPACITY_LIMIT"), "MEMORY_STOP")
check("un OOM cresciuto vince su tutto",
      classify(memory={"memory_stop": False, "pressure_delta": {"oom_kill": 1}},
               stop="MAX_500_REACHED"), "MEMORY_STOP")
check("un fallimento strutturale vince sui verdetti di capacita'",
      classify(memory=VUOTO, structural="popolamento incompleto",
               stop="CAPACITY_LIMIT"), "STRUCTURAL_FAIL")
check("il limite di capacita' passa", classify(memory=VUOTO, stop="CAPACITY_LIMIT"),
      "CAPACITY_LIMIT")
check("il limite del generatore passa",
      classify(memory=VUOTO, stop="GENERATOR_LIMIT"), "GENERATOR_LIMIT")
check("il tetto raggiunto passa", classify(memory=VUOTO, stop="MAX_500_REACHED"),
      "MAX_500_REACHED")
check("senza nessuna ragione e' un fallimento strutturale",
      classify(memory=VUOTO), "STRUCTURAL_FAIL")

print("\n== il residente attivo: una richiesta al secondo, e la sua lateness ==")
check("il residente non ha piu' think_times",
      "think_times" in population_probe.Resident.__init__.__code__.co_varnames, False)
check("ma ha una scadenza propria",
      "due" in population_probe.Resident.__init__.__code__.co_names
      or "self.due" in inspect.getsource(population_probe.Resident.__init__), True)
sorgente = inspect.getsource(population_probe.Resident.work_loop)
check("il ciclo legge il periodo dalla sonda", "self.probe.request_period" in sorgente, True)
check("e avanza la scadenza di un periodo, senza recuperare",
      "self.due += period" in sorgente, True)
check("la lateness passa alla chiamata", "lateness=started - self.due" in sorgente, True)
check("la colonna della lateness e' nel CSV",
      "lateness_s" in population_probe.CALL_COLUMNS, True)

print("\n" + "=" * 50)
if failures:
    print(f"FALLITI {len(failures)}:")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("tutti i controlli passati")
