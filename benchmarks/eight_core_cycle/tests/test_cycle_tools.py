"""Direct checks of the eight-core cycle's own tools, with no laboratory and no Docker.

Three things are exercised, and they are exactly the three this scenario adds to
what ``bench_common`` already provides and already tests:

1. THE PLAN'S PER-USER PACE. The whole point of this run is that the offered rate
   is the count of active users, so when fifty stop it falls by fifty requests a
   second. The check is arithmetic on the generated plan: the paused users own no
   row at all during the pause, and the per-second histogram of every phase is the
   shape the mandate describes.

2. THE ADMISSION GUARD. Fifteen consecutive evaluations over the limit close the
   door; fourteen do not; a single spike does not; a window with too few samples
   neither closes it nor resets the count; and once closed it never reopens, even
   when the latency comes back well inside the limit.

3. THE ENGINE'S WITHHOLDING. A row whose user is not admitted must be counted, not
   dropped in silence: the plan is fixed, so what the run did not send is a number.

The load engine, the memory guard, the role classifiers and the lifecycle are NOT
retested here — they are ``bench_common``'s, they have their own tests, and this
scenario uses them unchanged.

    python3 tests/test_cycle_tools.py
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIO = os.path.abspath(os.path.join(HERE, os.pardir))
BENCHMARKS = os.path.abspath(os.path.join(SCENARIO, os.pardir))
sys.path.insert(0, BENCHMARKS)
sys.path.insert(0, os.path.join(BENCHMARKS, "bench_common"))
sys.path.insert(0, SCENARIO)

from admission_guard import AdmissionGuard                                        # noqa: E402
from bench_common.container_probe import COLUMNS                                  # noqa: E402
from bench_common.stop_guard import StopFlag                                      # noqa: E402
from cycle_probe import (                                                          # noqa: E402
    CYCLE_COLUMNS, LOGIN_ATTEMPTS_MAX, LOGIN_RETRY_SECONDS, CycleEngine,
    CycleProbe, InvalidRun)                                # noqa: E402

failures = []

SETTINGS = {"p95_limit_ms": 1500.0, "consecutive_buckets": 15, "minimum_samples": 5}


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got!r}"
          + ("" if ok else f"  atteso {want!r}"))
    if not ok:
        failures.append(label)


def make_plan(directory, name, *extra):
    """The real generator, run as the campaign runs it."""
    out = os.path.join(directory, name)
    done = subprocess.run(
        [sys.executable, os.path.join(SCENARIO, "make_cycle_trace.py"),
         "--out", out, "--seed", "20260831", *extra],
        capture_output=True, text=True, cwd=SCENARIO)
    if done.returncode != 0:
        raise SystemExit(f"il generatore non ha prodotto il piano:\n{done.stderr}")
    return json.load(open(out)), out


def per_second(rows, phase):
    """How many requests each whole second of a phase carries."""
    counts = {}
    for row in rows:
        if row["phase"] != phase:
            continue
        second = int(row["t_rel"])
        counts[second] = counts.get(second, 0) + 1
    return counts


def rows_of_user(rows, phase, user):
    return [row for row in rows if row["phase"] == phase and row["user"] == user]


SANDBOX = tempfile.mkdtemp(prefix="cycle_tools_")
try:
    print("== il piano: il ritmo e' per utente, non globale ==")
    plan, plan_path = make_plan(SANDBOX, "cycle_plan.json")
    rows = plan["calls"]
    users, paused_count = plan["users"], plan["paused"]
    paused = set(plan["paused_order"])
    active_count = users - paused_count

    check("centoventi utenti", users, 120)
    check("cinquanta in pausa", paused_count, 50)
    check("i cinquanta sono distinti", len(paused), 50)
    check("e sono tutti utenti del piano",
          all(name in {f"user_{i + 1}" for i in range(users)} for name in paused), True)

    print("\n  -- full_warmup: tutti attivi, un utente una richiesta al secondo --")
    stabilize = per_second(rows, "full_warmup")
    check("sessanta secondi", len(stabilize), 60)
    check("ogni secondo porta 120 richieste", sorted(set(stabilize.values())), [120])
    check("ogni utente ha una riga per secondo",
          {len(rows_of_user(rows, "full_warmup", f"user_{i + 1}")) for i in range(users)},
          {60})

    print("\n  -- pause_50: i fermi non hanno NESSUNA riga, e il ritmo scende di 50 --")
    pause = per_second(rows, "pause_50")
    check("sessanta secondi", len(pause), 60)
    check("ogni secondo porta 70 richieste", sorted(set(pause.values())), [active_count])
    check("i cinquanta fermi non hanno righe",
          sum(len(rows_of_user(rows, "pause_50", name)) for name in paused), 0)
    check("i settanta attivi hanno una riga per secondo",
          {len(rows_of_user(rows, "pause_50", f"user_{i + 1}")) for i in range(users)
           if f"user_{i + 1}" not in paused}, {60})
    check("il calo e' esattamente di 50 richieste al secondo",
          stabilize[10] - pause[10], paused_count)

    print("\n  -- login_ramp: uno ogni secondo, il carico sale --")
    ramp = per_second(rows, "login_ramp")
    check("il primo secondo e' vuoto: nessuno ha ancora chiamato", ramp.get(0), None)
    check("al secondo 1 chiama un utente", ramp[1], 1)
    check("al secondo 10 chiamano dieci utenti", ramp[10], 10)
    check("al secondo 119 chiamano centodiciannove utenti", ramp[119], 119)
    check("l'utente k comincia un secondo dopo il suo login",
          [round(rows_of_user(rows, "login_ramp", f"user_{k}")[0]["t_rel"] - (k - 1) - 1, 6)
           for k in (1, 7, 60, 119)],
          [round((k - 1) / 120.0, 6) for k in (1, 7, 60, 119)])

    print("\n  -- return_ramp: il ritmo sale da 70 a 120, uno al secondo --")
    ret = per_second(rows, "return_ramp")
    check("cinquantuno secondi", len(ret), paused_count + 1)
    check("apre a 70, l'istante prima del primo rientro", ret[0], active_count)
    check("al secondo 1 sono 71", ret[1], active_count + 1)
    check("all'ultimo secondo sono 120", ret[paused_count], users)
    check("sale di uno al secondo, senza salti",
          [ret[s + 1] - ret[s] for s in range(paused_count)], [1] * paused_count)

    print("\n  -- nessuna collisione di istanti, in nessuna fase --")
    for phase in ("login_ramp", "full_warmup", "full_measure_1", "pause_50",
                  "return_ramp", "full_measure_2"):
        instants = [row["t_rel"] for row in rows if row["phase"] == phase]
        check(f"{phase}: istanti tutti distinti", len(instants), len(set(instants)))

    print("\n  -- le righe di una fase sono ordinate per istante --")
    for phase in ("login_ramp", "return_ramp"):
        instants = [row["t_rel"] for row in rows if row["phase"] == phase]
        check(f"{phase}: gia' ordinate", instants, sorted(instants))

    print("\n== il piano e' deterministico, e il seed conta ==")
    os.makedirs(os.path.join(SANDBOX, "again"), exist_ok=True)
    again, again_path = make_plan(os.path.join(SANDBOX, "again"), "cycle_plan.json")
    check("gli stessi byte con lo stesso seed",
          open(plan_path, "rb").read() == open(again_path, "rb").read(), True)
    os.makedirs(os.path.join(SANDBOX, "other"), exist_ok=True)
    other = subprocess.run(
        [sys.executable, os.path.join(SCENARIO, "make_cycle_trace.py"),
         "--out", os.path.join(SANDBOX, "other", "cycle_plan.json"), "--seed", "1"],
        capture_output=True, text=True, cwd=SCENARIO)
    check("un seed diverso genera", other.returncode, 0)
    other_plan = json.load(open(os.path.join(SANDBOX, "other", "cycle_plan.json")))
    check("un seed diverso pesca altri utenti in pausa",
          other_plan["paused_order"] == plan["paused_order"], False)
    check("ma sempre cinquanta", len(set(other_plan["paused_order"])), 50)

    print("\n== la guardia di ammissione: bucket di un secondo, non sovrapposti ==")

    def guard_for(path_name):
        return AdmissionGuard(SETTINGS, os.path.join(SANDBOX, path_name),
                              lambda: {"phase": "full_measure_1",
                                       "population_authenticated": 120,
                                       "population_active": 120,
                                       "completed": 9000, "pending": 3})

    def play(guard, latencies, first=1000.0, per_second=20):
        """One second per element, judged when that second has closed.

        The guard files a call under the whole second it completed in, and judges
        a second only once nothing can still land in it. Driving it means filling
        second N and then letting the clock reach N+1 — which is what a real run
        does, one pass a second.
        """
        guard.judge_closed_buckets(first)
        for step, latency in enumerate(latencies):
            second = first + step
            for slot in range(per_second):
                guard.record_latency(second + slot / float(per_second + 1), latency)
            guard.judge_closed_buckets(second + 1.0)

    print("\n  -- cinque secondi cattivi: nessuno stop --")
    guard = guard_for("g_5.json")
    play(guard, [3000.0] * 5 + [100.0] * 3)
    check("porta aperta", guard.admission_open, True)
    check("cinque bucket oltre soglia", guard.bad, 5)
    check("sequenza massima cinque", guard.peak_consecutive, 5)

    print("\n  -- quattordici secondi cattivi: nessuno stop --")
    guard = guard_for("g_14.json")
    play(guard, [3000.0] * 14)
    check("porta aperta", guard.admission_open, True)
    check("sequenza massima quattordici", guard.peak_consecutive, 14)
    check("nessun evento", guard.event, None)

    print("\n  -- quindici secondi cattivi: STOP --")
    guard = guard_for("g_15.json")
    play(guard, [3000.0] * 15)
    check("porta chiusa", guard.admission_open, False)
    check("scattata al quindicesimo", guard.event["consecutive_buckets"], 15)
    check("l'evento e' ADMISSION_STOP", guard.event["event"], "ADMISSION_STOP")
    check("scritto su disco appena scattato",
          json.load(open(os.path.join(SANDBOX, "g_15.json")))["event"]["event"],
          "ADMISSION_STOP")
    for field in ("event", "ts", "bucket", "reason", "p50_ms", "p95_ms", "p99_ms",
                  "phase", "population_authenticated", "population_active",
                  "completed", "pending", "consecutive_buckets",
                  "samples_in_bucket", "p95_limit_ms"):
        check(f"    campo {field}", field in guard.event, True)

    print("\n  -- dieci cattivi, uno buono, quattordici cattivi: nessuno stop --")
    guard = guard_for("g_10_1_14.json")
    play(guard, [3000.0] * 10 + [100.0] + [3000.0] * 14)
    check("porta aperta", guard.admission_open, True)
    check("la sequenza piu' lunga e' quattordici", guard.peak_consecutive, 14)
    check("ventiquattro bucket oltre soglia in totale", guard.bad, 24)

    print("\n  -- un bucket con pochi campioni azzera la sequenza --")
    guard = guard_for("g_thin.json")
    play(guard, [3000.0] * 10)
    check("dieci consecutivi", guard.consecutive, 10)
    # Un secondo con soli tre campioni: sotto il minimo di cinque.
    guard.record_latency(1010.1, 9000.0)
    guard.record_latency(1010.2, 9000.0)
    guard.record_latency(1010.3, 9000.0)
    record = guard.judge_closed_buckets(1011.0)[0]
    check("il verdetto e' pochi campioni", record["verdict"], "pochi campioni")
    check("e azzera la sequenza", guard.consecutive, 0)
    play(guard, [3000.0] * 14, first=1011.0)
    check("altri quattordici non bastano", guard.admission_open, True)

    print("\n  -- un secondo senza traffico azzera la sequenza --")
    guard = guard_for("g_gap.json")
    play(guard, [3000.0] * 12)
    check("dodici consecutivi", guard.consecutive, 12)
    guard.judge_closed_buckets(1014.0)
    check("il buco ha azzerato", guard.consecutive, 0)
    check("contato fra i bucket magri", guard.thin > 0, True)

    print("\n  -- uno spike isolato: nessuno stop, e una sola violazione --")
    guard = guard_for("g_spike.json")
    play(guard, [100.0] * 5 + [3000.0] + [100.0] * 20)
    check("porta aperta", guard.admission_open, True)
    check("una sola violazione", guard.bad, 1)
    check("sequenza massima uno", guard.peak_consecutive, 1)

    print("\n  -- esattamente sulla soglia non e' una violazione --")
    guard = guard_for("g_edge.json")
    play(guard, [1500.0] * 20)
    check("porta aperta", guard.admission_open, True)
    check("nessuna violazione", guard.bad, 0)

    print("\n  -- la porta non si riapre mai piu' --")
    guard = guard_for("g_latch.json")
    play(guard, [3000.0] * 15)
    check("chiusa", guard.admission_open, False)
    play(guard, [40.0] * 40, first=1100.0)
    check("quaranta secondi ottimi non la riaprono", guard.admission_open, False)
    check("l'evento resta quello di prima", guard.event["consecutive_buckets"], 15)

    print("\n  -- la guardia non tocca la bandiera di stop del memory guard --")
    flag = StopFlag()
    guard = guard_for("g_flag.json")
    play(guard, [9000.0] * 20)
    check("la porta e' chiusa", guard.admission_open, False)
    check("ma la corsa non e' fermata", flag.stopped, False)
    check("nessuna ragione di stop registrata", flag.reason_list, [])
    check("il verdetto dichiara admission_stop, non memory_stop",
          sorted(key for key in guard.verdict if "stop" in key), ["admission_stop"])

    print("\n== il motore trattiene le righe degli utenti non ammessi ==")

    class FakeInbox:
        """A queue that only remembers, so the test can count what arrived."""

        def __init__(self):
            self.items = []

        def put(self, item):
            self.items.append(item)

        def qsize(self):
            return 0

    class FakeRunner:
        def __init__(self):
            self.inbox = FakeInbox()
            self.pending = 0

    trace = [{"phase": "p", "rate": 2.0, "t_rel": 0.0, "user": "user_1",
              "lookup_index": 0, "op": "x", "seq": 0},
             {"phase": "p", "rate": 2.0, "t_rel": 0.02, "user": "user_2",
              "lookup_index": 0, "op": "x", "seq": 1},
             {"phase": "p", "rate": 2.0, "t_rel": 0.04, "user": "user_3",
              "lookup_index": 0, "op": "x", "seq": 2}]
    engine = CycleEngine(trace, lambda *a: b"", ["x"], StopFlag())
    for label in ("user_1", "user_2", "user_3"):
        engine.runners[label] = FakeRunner()
    engine.admit("user_1")
    engine.admit("user_3")
    record = engine.play_cycle_window("p")
    check("due righe offerte su tre", record["offered"], 2)
    check("una trattenuta", record["withheld"], 1)
    check("le righe dichiarate sono tre", record["declared_rows"], 3)
    check("il contatore globale delle offerte concorda", engine.state["offered"], 2)
    check("l'utente non ammesso non ha ricevuto nulla",
          len(engine.runners["user_2"].inbox.items), 0)
    check("gli ammessi hanno ricevuto una riga ciascuno",
          [len(engine.runners[label].inbox.items) for label in ("user_1", "user_3")], [1, 1])
    check("il trattenuto e' registrato per fase", engine.withheld["p"], 1)

    print("\n== la prima chiamata di un rientro e' cronometrata a parte ==")
    engine = CycleEngine([], lambda *a: b"", ["x"], StopFlag())
    engine.expect_return("user_9", 3)
    engine.record_call({"phase": "return_ramp", "user": "user_9", "latency_ms": 812.5,
                        "lateness_s": 0.01, "status": 200, "app_error": "",
                        "transport_error": "", "worker": "pool_0002",
                        "completed_at": 1.0, "scheduled_at": 0.0, "started_at": 0.5})
    engine.record_call({"phase": "return_ramp", "user": "user_9", "latency_ms": 40.0,
                        "lateness_s": 0.01, "status": 200, "app_error": "",
                        "transport_error": "", "worker": "pool_0002",
                        "completed_at": 2.0, "scheduled_at": 1.0, "started_at": 1.5})
    check("una sola prima chiamata registrata", len(engine.return_calls), 1)
    check("con la sua latenza", engine.return_calls[0]["latency_ms"], 812.5)
    check("e la sua posizione nel rientro", engine.return_calls[0]["return_position"], 3)
    check("il worker che l'ha servita", engine.return_calls[0]["worker"], "pool_0002")

    print("\n== il login si ritenta: l'attesa completata non e' una rinuncia ==")

    class ContaAttese(StopFlag):
        """A real StopFlag that also counts how many times it was waited on.

        The bug this guards against was a misread return value, so the test must
        exercise the REAL wait — not a stand-in that returns whatever the test
        expects. Only the counting is added.
        """

        def __init__(self):
            super().__init__()
            self.waits = 0

        def wait(self, seconds, where):
            self.waits += 1
            return super().wait(seconds, where)

    class FintoLogin:
        """A LoggedUser stand-in that fails the first ``failures`` constructions.

        Each construction is one new connection: the real ``LoggedUser`` builds
        its own opener and its own ``HTTPConnection``, so counting constructions
        counts connections.
        """

        costruzioni = 0
        falliranno = 0

        def __init__(self, *args, **kwargs):
            type(self).costruzioni += 1
            if type(self).costruzioni <= type(self).falliranno:
                raise urllib.error.HTTPError(
                    "http://finto/", 500, "INTERNAL SERVER ERROR", {}, None)
            self.username = args[3] if len(args) > 3 else "ignoto"

        def get_call_form(self, lookup, counter):
            return {}

    class FintoRunner:
        avviati = 0

        def __init__(self, engine, label, logged):
            self.label = label
            self.user = logged
            self.pending = 0

        def start(self):
            type(self).avviati += 1

    def login_probe(flag, failures, retry_seconds=0.02):
        """A probe whose login policy is the real one, only faster to wait.

        ``attempts_max`` is NOT overridden: the number of attempts is the thing
        under test, so it comes from the module. Only the pause between them is
        shortened, and the tests assert the real pause separately — a test that
        actually slept five seconds five times would take half a minute.
        """
        FintoLogin.costruzioni = 0
        FintoLogin.falliranno = failures
        FintoRunner.avviati = 0
        probe = CycleProbe.__new__(CycleProbe)
        probe.arguments = argparse.Namespace(base="http://finto", password="a")
        probe.protocol = {}
        probe.accounts = [f"conto.{i}" for i in range(200)]
        probe.lookups = ["conto.0"]
        probe.stop_flag = flag
        probe.login_attempts = []
        probe.cold_start_errors = 0
        probe.engine = CycleEngine([], lambda *a: b"", ["x"], flag)
        return probe

    import cycle_probe as modulo
    vero_logged, vero_runner = modulo.LoggedUser, modulo.UserRunner
    vera_attesa = modulo.LOGIN_RETRY_SECONDS
    modulo.LoggedUser, modulo.UserRunner = FintoLogin, FintoRunner
    try:
        print("\n  -- la politica dichiarata copre la finestra fredda misurata --")
        check("cinque tentativi", LOGIN_ATTEMPTS_MAX, 5)
        check("cinque secondi fra i tentativi", LOGIN_RETRY_SECONDS, 5.0)
        check("venti secondi coperti",
              (LOGIN_ATTEMPTS_MAX - 1) * LOGIN_RETRY_SECONDS, 20.0)
        check("e venti contiene i quattordici osservati",
              (LOGIN_ATTEMPTS_MAX - 1) * LOGIN_RETRY_SECONDS > 14.0, True)
        check("tre tentativi a cinque secondi NON basterebbero",
              (3 - 1) * LOGIN_RETRY_SECONDS > 14.0, False)

        modulo.LOGIN_RETRY_SECONDS = 0.02

        print("\n  -- 1. successo al primo tentativo --")
        flag = ContaAttese()
        probe = login_probe(flag, failures=0)
        check("l'utente e' entrato", probe.build_user(0, [], []), "user_1")
        check("un solo tentativo", len(probe.login_attempts), 1)
        check("una sola connessione", FintoLogin.costruzioni, 1)
        check("nessuna attesa", flag.waits, 0)
        check("nessun cold_start", probe.cold_start_errors, 0)

        print("\n  -- 2. successo al secondo, dopo 5 secondi --")
        flag = ContaAttese()
        probe = login_probe(flag, failures=1)
        check("l'utente e' entrato", probe.build_user(1, [], []), "user_2")
        check("due tentativi", len(probe.login_attempts), 2)
        check("due connessioni", FintoLogin.costruzioni, 2)
        check("una attesa", flag.waits, 1)
        check("cioe' 5 secondi coperti", flag.waits * vera_attesa, 5.0)
        check("un cold_start", probe.cold_start_errors, 1)
        check("il primo e' cold_start, il secondo ok",
              [a["outcome"] for a in probe.login_attempts], ["cold_start", "ok"])

        print("\n  -- 3. successo al quarto, dopo 15 secondi --")
        flag = ContaAttese()
        probe = login_probe(flag, failures=3)
        check("l'utente e' entrato", probe.build_user(2, [], []), "user_3")
        check("quattro tentativi", len(probe.login_attempts), 4)
        check("quattro connessioni", FintoLogin.costruzioni, 4)
        check("tre attese", flag.waits, 3)
        check("cioe' 15 secondi coperti", flag.waits * vera_attesa, 15.0)
        check("tre cold_start", probe.cold_start_errors, 3)
        check("gli esiti in ordine", [a["outcome"] for a in probe.login_attempts],
              ["cold_start", "cold_start", "cold_start", "ok"])

        print("\n  -- 4. successo al quinto, dopo 20 secondi --")
        flag = ContaAttese()
        probe = login_probe(flag, failures=4)
        check("l'utente e' entrato", probe.build_user(3, [], []), "user_4")
        check("cinque tentativi", len(probe.login_attempts), 5)
        check("cinque connessioni", FintoLogin.costruzioni, 5)
        check("quattro attese", flag.waits, 4)
        check("cioe' 20 secondi coperti", flag.waits * vera_attesa, 20.0)
        check("quattro cold_start", probe.cold_start_errors, 4)
        check("l'ultimo e' ok", probe.login_attempts[-1]["outcome"], "ok")

        print("\n  -- 5. cinque fallimenti: l'esecuzione fallisce --")
        flag = ContaAttese()
        probe = login_probe(flag, failures=5)
        try:
            probe.build_user(4, [], [])
            check("deve sollevare", "non ha sollevato", "InvalidRun")
        except InvalidRun as failure:
            check("solleva", "non e' entrato" in str(failure), True)
            check("e dichiara cinque tentativi", "dopo 5 tentativi" in str(failure), True)
        check("cinque tentativi registrati", len(probe.login_attempts), 5)
        check("cinque connessioni", FintoLogin.costruzioni, 5)
        check("nessun cold_start: non c'e' stato successo", probe.cold_start_errors, 0)
        check("tutti failed", {a["outcome"] for a in probe.login_attempts}, {"failed"})
        check("non e' entrato nel motore", probe.engine.runners, {})
        check("quattro attese, non piu'", flag.waits, 4)

        print("\n  -- 6. uno stop durante l'attesa: nessun tentativo successivo --")
        flag = ContaAttese()
        flag.ask_stop("prova", "stop durante l'attesa")
        probe = login_probe(flag, failures=5)
        try:
            probe.build_user(6, [], [])
            check("deve sollevare", "non ha sollevato", "InvalidRun")
        except InvalidRun as failure:
            check("solleva dopo UN tentativo", "dopo 1 tentativi" in str(failure), True)
        check("un tentativo", len(probe.login_attempts), 1)
        check("una connessione", FintoLogin.costruzioni, 1)
        check("una attesa, interrotta", flag.waits, 1)

        print("\n  -- 7. una connessione nuova per OGNI tentativo --")
        flag = ContaAttese()
        probe = login_probe(flag, failures=4)
        probe.build_user(8, [], [])
        check("connessioni = tentativi", FintoLogin.costruzioni,
              len(probe.login_attempts))
        check("e sono cinque", FintoLogin.costruzioni, 5)

        print("\n  -- 8. conteggio e classificazione cold_start --")
        flag = ContaAttese()
        probe = login_probe(flag, failures=2)
        probe.build_user(10, [], [])
        cold = [a for a in probe.login_attempts if a["outcome"] == "cold_start"]
        check("due classificati cold_start", len(cold), 2)
        check("il contatore concorda", probe.cold_start_errors, len(cold))
        check("ognuno porta il suo status", [a["status"] for a in cold], [500, 500])
        check("ognuno porta il suo numero di tentativo",
              [a["attempt"] for a in cold], [1, 2])
        check("nessun cold_start dopo il successo",
              probe.login_attempts[-1]["outcome"], "ok")
        check("il successo non e' contato fra gli errori a freddo",
              probe.cold_start_errors, 2)
    finally:
        modulo.LoggedUser, modulo.UserRunner = vero_logged, vero_runner
        modulo.LOGIN_RETRY_SECONDS = vera_attesa

    print("\n== la mappa utente -> worker traduce le etichette ==")
    probe = CycleProbe.__new__(CycleProbe)
    probe.engine = CycleEngine([], lambda *a: b"", ["x"], StopFlag())
    probe.engine.username_of = {"user_1": "alexander.king", "user_2": "amelia.martin",
                                "user_3": "ava.brown"}
    placement = {"alexander.king": "pool_0001", "amelia.martin": "pool_0002",
                 "sconosciuto": "pool_0008"}
    mapped = probe.get_worker_of_labels(placement)
    check("l'etichetta prende il worker del suo username",
          mapped, {"user_1": "pool_0001", "user_2": "pool_0002"})
    check("un utente non collocato resta assente, non vuoto", "user_3" in mapped, False)
    check("un username che il runner non conosce non entra",
          "pool_0008" in mapped.values(), False)
    check("senza la traduzione la ricerca per etichetta fallirebbe",
          placement.get("user_1"), None)

    print("\n== i worker attesi: regola diversa per i due stack ==")

    class FakeEyes:
        def __init__(self, placed):
            self.placed = placed

        def population(self):
            return {"placed": self.placed}

    def expected_for(stack, placed, per_worker=15, maximum=8):
        probe = CycleProbe.__new__(CycleProbe)
        probe.eyes = None if stack == "legacy" else FakeEyes(placed)
        probe.arguments = argparse.Namespace(
            expect_workers=maximum, expect_per_worker=per_worker)
        return probe.expected_workers_now

    check("legacy: gli otto worker ci sono anche a popolazione zero",
          expected_for("legacy", 0), 8)
    check("legacy: e anche a popolazione piena", expected_for("legacy", 120), 8)
    check("bridge: a pool vuoto se ne attende uno, quello di partenza",
          expected_for("bridge", 0), 1)
    check("bridge: quindici utenti collocati, un worker",
          expected_for("bridge", 15), 1)
    check("bridge: sedici utenti collocati, due worker",
          expected_for("bridge", 16), 2)
    check("bridge: centoventi utenti collocati, otto worker",
          expected_for("bridge", 120), 8)
    check("bridge: mai oltre il massimo configurato",
          expected_for("bridge", 500), 8)
    check("bridge nello smoke: sedici utenti a due per worker, otto",
          expected_for("bridge", 16, per_worker=2), 8)
    check("le due regole NON coincidono a popolazione zero",
          expected_for("legacy", 0) == expected_for("bridge", 0), False)

    print("\n== il verdetto delle fasi: una mancante ferma tutto ==")

    def verdict_probe(played, reached, withheld=None, admission_open=True):
        probe = CycleProbe.__new__(CycleProbe)
        probe.protocol = {"phases": [{"phase": name} for name in
                                     ("login_ramp", "full_warmup", "full_measure_1",
                                      "pause_50", "return_ramp", "full_measure_2")]}
        probe.phases_played = list(played)
        probe.reached_full = reached
        probe.plan = {"users": 120}
        probe.checkpoints = []
        probe.engine = CycleEngine([], lambda *a: b"", ["x"], StopFlag())
        probe.engine.withheld = withheld or {}
        for index in range(120 if reached else 13):
            probe.engine.runners[f"user_{index + 1}"] = None
        probe.admission = AdmissionGuard(SETTINGS, os.path.join(SANDBOX, "v.json"),
                                         lambda: {})
        if not admission_open:
            probe.admission.closed.set()
        return probe

    every = ["login_ramp", "full_warmup", "full_measure_1", "pause_50",
             "return_ramp", "full_measure_2"]
    probe = verdict_probe(every, True)
    check("tutte le fasi e popolazione piena: nessun problema",
          probe.require_every_phase()["problemi"], [])

    print("\n  -- 1. trattenute nella RAMPA: registrate, non bloccanti --")
    probe = verdict_probe(every, True, withheld={"login_ramp": 12})
    record = probe.require_every_phase()
    check("nessun problema", record["problemi"], [])
    check("ma le dodici sono registrate", record["withheld_in_login_ramp"], 12)
    check("e non compaiono fra le bloccanti", record["withheld_after_ramp_blocking"], {})

    print("\n  -- 2. trattenute DOPO la rampa, senza ADMISSION_STOP: bloccanti --")
    probe = verdict_probe(every, True, withheld={"login_ramp": 12, "pause_50": 7})
    try:
        probe.require_every_phase()
        check("devono fallire", "non ha sollevato", "InvalidRun")
    except InvalidRun as failure:
        check("sollevano", "trattenute dopo la rampa" in str(failure), True)
        check("e nominano la fase e il numero", "'pause_50': 7" in str(failure), True)
        check("la rampa NON e' fra le colpevoli", "login_ramp" in str(failure), False)

    print("\n  -- 3. trattenute dopo ADMISSION_STOP: registrate a parte --")
    probe = verdict_probe(every, True, withheld={"return_ramp": 7, "full_measure_2": 40},
                          admission_open=False)
    record = probe.require_every_phase()
    check("nessun problema", record["problemi"], [])
    check("nessuna bloccante", record["withheld_after_ramp_blocking"], {})
    check("registrate come attese dopo la porta chiusa",
          record["withheld_expected_after_admission_stop"],
          {"return_ramp": 7, "full_measure_2": 40})
    check("e la porta risulta chiusa nel record", record["admission_open"], False)

    print("\n  -- 4. popolazione incompleta: fallisce comunque --")
    probe = verdict_probe(every, False)
    try:
        probe.require_every_phase()
        check("deve fallire", "non ha sollevato", "InvalidRun")
    except InvalidRun as failure:
        check("solleva per la popolazione", "popolazione incompleta" in str(failure), True)
    probe = verdict_probe(["login_ramp", "full_measure_1"], False)
    try:
        probe.require_every_phase()
        check("due fasi su sei devono fallire", "non ha sollevato", "InvalidRun")
    except InvalidRun as failure:
        check("nomina le fasi non eseguite", "fasi non eseguite" in str(failure), True)
        check("e la popolazione incompleta", "popolazione incompleta" in str(failure), True)
    probe = verdict_probe(every, False, withheld={"full_measure_1": 9})
    try:
        probe.require_every_phase()
        check("popolazione incompleta con trattenute deve fallire",
              "non ha sollevato", "InvalidRun")
    except InvalidRun as failure:
        check("fallisce per la popolazione, non per le trattenute",
              ("popolazione incompleta" in str(failure)
               and "trattenute dopo la rampa" not in str(failure)), True)

    print("\n  -- 5. tutte le fasi, sole trattenute nella rampa: PASS --")
    probe = verdict_probe(every, True, withheld={"login_ramp": 12, "full_warmup": 0,
                                                 "full_measure_1": 0, "pause_50": 0,
                                                 "return_ramp": 0, "full_measure_2": 0})
    record = probe.require_every_phase()
    check("nessun problema: e' un PASS", record["problemi"], [])
    check("dodici registrate nella rampa", record["withheld_in_login_ramp"], 12)
    check("zero bloccanti", record["withheld_after_ramp_blocking"], {})
    check("porta aperta", record["admission_open"], True)
    check("tutte e sei le fasi giocate", record["played"], every)

    print("\n== le colonne condivise non cambiano ==")
    check("CYCLE_COLUMNS aggiunge esattamente tre colonne",
          [name for name in CYCLE_COLUMNS if name not in COLUMNS],
          ["users_active", "users_paused", "admission_stop"])
    check("e non ne toglie nessuna", [name for name in COLUMNS if name not in CYCLE_COLUMNS], [])
    check("l'ordine relativo delle condivise e' intatto",
          [name for name in CYCLE_COLUMNS if name in COLUMNS], COLUMNS)
finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)

print("\n" + "=" * 50)
if failures:
    print(f"FALLITI {len(failures)}:")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("tutti i controlli passati")
