"""Direct checks of the L120 comparison tools, without Docker and without a lab.

Four things that, if wrong, would make the comparison produce numbers that look
fine and mean nothing: a plan that is not the same on the two legs, a role map
that calls the wrong process a worker, a window that counts a request that never
came back, and an error put in the wrong bucket.

    python3 test_l120_tools.py
"""

import json
import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIO = os.path.abspath(os.path.join(HERE, os.pardir))
BENCHMARKS = os.path.abspath(os.path.join(SCENARIO, os.pardir))
sys.path.insert(0, BENCHMARKS)
sys.path.insert(0, SCENARIO)

import make_trace                                                                # noqa: E402
from bench_common.container_probe import (                                        # noqa: E402
    BridgeRoles, ContainerProbe, LegacyRoles)
from bench_common.load_engine import LoadEngine, UserRunner                       # noqa: E402
from bench_common.stop_guard import StopFlag                                      # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got!r}"
          + ("" if ok else f"  atteso {want!r}"))
    if not ok:
        failures.append(label)


# --------------------------------------------------------------------- il piano
print("\n== il piano e' lo stesso per le due gambe, o non e' un confronto ==")
with tempfile.TemporaryDirectory() as sandbox:
    first = os.path.join(sandbox, "a.json")
    second = os.path.join(sandbox, "b.json")
    third = os.path.join(sandbox, "c.json")
    common = ["--users", "8", "--warmup-seconds", "2", "--stabilize-seconds", "2",
              "--measure-seconds", "4", "--level-rate", "10", "--warmup-rate", "5"]
    make_trace.main(["--out", first] + common)
    make_trace.main(["--out", second] + common)
    make_trace.main(["--out", third, "--seed", "1"] + common)
    plan_a = json.load(open(first))
    plan_b = json.load(open(second))
    plan_c = json.load(open(third))
    check("lo stesso seed produce lo stesso piano", plan_a == plan_b, True)
    check("un seed diverso produce un piano diverso", plan_a == plan_c, False)
    check("il piano dichiara il proprio seed", plan_a["seed"], 20260831)
    check("lo .sha256 nasce accanto al piano", os.path.isfile(first + ".sha256"), True)
    counts = {}
    for call in plan_a["calls"]:
        counts[call["phase"]] = counts.get(call["phase"], 0) + 1
    check("le tre finestre hanno le richieste dichiarate",
          counts, {"warmup": 10, "stabilize_L120": 20, "measure_L120": 40})
    check("il totale coincide con quanto dichiarato",
          plan_a["calls_total"], len(plan_a["calls"]))
    check("ogni richiesta ha un utente della popolazione",
          all(call["user"] in {f"user_{n + 1}" for n in range(8)} for call in plan_a["calls"]),
          True)
    owners = {call["user"] for call in plan_a["calls"]}
    check("nessun utente resta senza richieste", len(owners), 8)
    check("il piano registra la cattura da cui deriva",
          set(plan_a["capture"]) >= {"sha256", "rows", "login_calls"}, True)
    check("il protocollo viaggia col piano, non nei runner",
          set(plan_a["protocol"]) >= {"baseline_seconds", "login_period_seconds",
                                      "apply_wait_seconds", "observe_seconds", "windows"},
          True)

# ---------------------------------------------------------------------- i ruoli
print("\n== i ruoli: il campionatore non deve chiamare worker un estraneo ==")
BRIDGE_TEXT = "\n".join([
    "P|1|0|79312|67041,1384,55924,22004,0|171|/usr/local/bin/python /usr/local/bin/gnrasgiserve bridge_lab -H 0.0.0.0 -p 8098 --nodebug",
    "P|40|1|140772|79574,12384,8680,25392,94316|228|/usr/local/bin/python -m genro_asgi.spa.orchestration.template_entry",
    "P|41|40|111116|58732,0,8724,8076,94316|5|/usr/local/bin/python -m genro_asgi.spa.orchestration.template_entry",
    "P|42|40|111000|58000,0,8000,8000,94000|7|/usr/local/bin/python -m genro_asgi.spa.orchestration.template_entry",
    "C|234438656|236589056|2147483648",
    "E|low 0;high 0;max 0;oom 0;oom_kill 0;oom_group_kill 0;",
    "S|anon|171970560",
    "U|1234567",
])
roles = BridgeRoles(worker_pids={"41": "pool_0001", "42": "pool_0002"})
probe = ContainerProbe("finto", roles)
processes, cgroup, memory_stat, cpu_usec = probe.parse(BRIDGE_TEXT)
by_role = {info["role"] for info in processes.values()}
check("il commander e' riconosciuto", "commander" in by_role, True)
check("il template e' riconosciuto", "template" in by_role, True)
check("i worker prendono il nome dal census", {"pool_0001", "pool_0002"} <= by_role, True)
check("i gauge del cgroup sono letti", cgroup["max"], "2147483648")
check("gli eventi del cgroup sono letti", "oom_kill 0" in cgroup["events"], True)
check("la CPU del cgroup e' letta", cpu_usec, 1234567)
check("la forma dichiarata e' certificata", probe.certify(processes, 2), [])
check("una forma sbagliata e' un elenco di problemi",
      len(probe.certify(processes, 4)), 1)

LEGACY_TEXT = "\n".join([
    "P|1|0|9000|8000,0,0,0,0|10|/bin/bash /lab/entrypoints/legacy.sh",
    "P|7|1|60000|50000,0,0,0,0|50|/usr/local/bin/python /usr/local/bin/gnrdaemon legacy_lab",
    "P|9|1|120000|90000,0,0,0,0|80|/usr/local/bin/python -m gnr.web.cli.gnrserveprod legacy_lab -b 0.0.0.0:8099 -w 4 -k gthread --threads 16",
    "P|11|9|110000|70000,0,0,0,0|60|/usr/local/bin/python -m gnr.web.cli.gnrserveprod legacy_lab -b 0.0.0.0:8099 -w 4 -k gthread --threads 16",
    "P|12|9|110000|70000,0,0,0,0|61|/usr/local/bin/python -m gnr.web.cli.gnrserveprod legacy_lab -b 0.0.0.0:8099 -w 4 -k gthread --threads 16",
    "C|300000000|310000000|2147483648",
    "E|low 0;high 0;max 0;oom 0;oom_kill 0;oom_group_kill 0;",
    "U|999",
])
legacy = ContainerProbe("finto", LegacyRoles())
processes, _, _, _ = legacy.parse(LEGACY_TEXT)
named = {info["pid"]: info["role"] for info in processes.values()}
check("il daemon e' riconosciuto", named["7"], "daemon")
check("il master di gunicorn e' il figlio di init", named["9"], "gunicorn_master")
check("i worker di gunicorn sono i figli del master",
      (named["11"], named["12"]), ("gunicorn_worker_01", "gunicorn_worker_02"))
check("l'entrypoint non e' un worker", named["1"], "other")
problems = legacy.certify(processes, 2)
check("la forma legacy con due worker ha un solo problema: l'entrypoint",
      len(problems), 1)
check("e il problema nomina i processi non classificati",
      "non classificati" in problems[0], True)

# ------------------------------------------------------------------ le finestre
print("\n== la finestra: offerte, avviate e completate sono tre numeri diversi ==")


class FakeConnection:
    """A socket that answers what the test decides, and can refuse to."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = 0

    def request(self, *args, **kwargs):
        self.sent += 1
        if not self.script:
            raise OSError("connessione chiusa")
        self.pending = self.script.pop(0)
        if isinstance(self.pending, Exception):
            raise self.pending

    def getresponse(self):
        status, payload = self.pending
        return FakeAnswer(status, payload)

    def close(self):
        pass


class FakeAnswer:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    def read(self):
        return self.payload


class FakeUser:
    def __init__(self, script):
        self.connection = FakeConnection(script)
        self.netloc = "127.0.0.1:9"
        self.headers = {}

    def get_call_form(self, lookup, counter):
        return {"lookup": lookup, "counter": counter}


def engine_with(script, rows):
    flag = StopFlag()
    engine = LoadEngine(rows, lambda user, lookup, counter: "body", ["a"], flag)
    user = FakeUser(script)
    runner = UserRunner(engine, "user_1", user)
    engine.runners["user_1"] = runner
    return engine, runner, user


ROWS = [{"phase": "measure", "rate": 10.0, "t_rel": index / 10.0, "user": "user_1"}
        for index in range(4)]

engine, runner, user = engine_with([(200, b"<result/>")] * 4, ROWS)
runner.start()
record = engine.play_window("measure")
engine.stop_runners()
check("offerte quante il piano dichiara", record["offered"], 4)
check("completate quante sono tornate", record["completed"], 4)
check("nessun errore su quattro 200", (record["errors_http"], record["errors_app"],
                                       record["errors_transport"]), (0, 0, 0))
check("nulla resta in coda a fine finestra", record["pending_at_end"], 0)
check("la finestra non emette verdetti sul generatore",
      "generator_valid" in record, False)

engine, runner, user = engine_with(
    [(200, b"<result/>"), (500, b"boom"), (200, b"<error>rotto</error>"),
     OSError("reset")], ROWS)
runner.start()
record = engine.play_window("measure")
engine.stop_runners()
check("un 500 e' un errore http", record["errors_http"], 1)
check("un 200 con <error> e' un errore applicativo", record["errors_app"], 1)
check("un'eccezione e' un errore di trasporto", record["errors_transport"], 1)
check("i tre errori non si sommano nello stesso secchio",
      record["errors_http"] + record["errors_app"] + record["errors_transport"], 3)

print("\n== lo stop taglia la finestra, e la finestra lo dichiara ==")
flag = StopFlag()
engine = LoadEngine([{"phase": "measure", "rate": 2.0, "t_rel": index / 2.0,
                      "user": "user_1"} for index in range(20)],
                    lambda user, lookup, counter: "body", ["a"], flag)
user = FakeUser([(200, b"<result/>")] * 40)
runner = UserRunner(engine, "user_1", user)
engine.runners["user_1"] = runner
runner.start()
threading.Timer(0.4, lambda: flag.ask_stop("prova", "taglio")).start()
record = engine.play_window("measure")
engine.stop_runners()
check("le richieste offerte sono meno di quelle dichiarate",
      record["offered"] < record["offered_declared"], True)
check("la finestra si dichiara troncata", record["truncated_by_stop"], True)

print("\n" + "=" * 50)
if failures:
    print(f"FALLITI {len(failures)}:")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("tutti i controlli passati")
