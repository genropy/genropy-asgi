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

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIO = os.path.abspath(os.path.join(HERE, os.pardir))
BENCHMARKS = os.path.abspath(os.path.join(SCENARIO, os.pardir))
sys.path.insert(0, BENCHMARKS)
sys.path.insert(0, os.path.join(BENCHMARKS, "bench_common"))
sys.path.insert(0, SCENARIO)

from admission_guard import AdmissionGuard                                        # noqa: E402
from bench_common.container_probe import COLUMNS                                  # noqa: E402
from bench_common.stop_guard import StopFlag                                      # noqa: E402
from cycle_probe import CYCLE_COLUMNS, CycleEngine                                # noqa: E402

failures = []

SETTINGS = {"p95_limit_ms": 1500.0, "consecutive_evaluations": 15,
            "window_seconds": 10.0, "minimum_samples": 30}


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

    print("\n  -- full_stabilize: tutti attivi, un utente una richiesta al secondo --")
    stabilize = per_second(rows, "full_stabilize")
    check("sessanta secondi", len(stabilize), 60)
    check("ogni secondo porta 120 richieste", sorted(set(stabilize.values())), [120])
    check("ogni utente ha una riga per secondo",
          {len(rows_of_user(rows, "full_stabilize", f"user_{i + 1}")) for i in range(users)},
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
    for phase in ("login_ramp", "full_stabilize", "full_measure_1", "pause_50",
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

    print("\n== la guardia di ammissione: quindici valutazioni, non quattordici ==")

    def guard_for(path_name, context=None):
        return AdmissionGuard(SETTINGS, os.path.join(SANDBOX, path_name),
                              lambda: context or {"phase": "full_measure_1",
                                                  "population_authenticated": 120,
                                                  "population_active": 120,
                                                  "completed": 9000, "pending": 3})

    def feed(guard, latency_ms, now, count=40):
        """One second of calls, all stamped inside that second."""
        for index in range(count):
            guard.record_latency(now - 0.001 * index, latency_ms)

    def play(guard, latencies, first=1000.0):
        """A timeline, one latency per second, evaluated once each second.

        The window is mobile, so a second's calls stay in it for ten
        evaluations: this is the only faithful way to drive the guard, and it is
        why a short burst of slowness produces more than one breach.
        """
        for step, latency in enumerate(latencies):
            feed(guard, latency, first + step)
            guard.evaluate(first + step)

    guard = guard_for("g1.json")
    for step in range(14):
        feed(guard, 2000.0, 1000.0 + step)
        guard.evaluate(1000.0 + step)
    check("dopo quattordici valutazioni oltre soglia la porta e' aperta",
          guard.admission_open, True)
    check("il conteggio consecutivo e' quattordici", guard.consecutive, 14)
    feed(guard, 2000.0, 1014.0)
    guard.evaluate(1014.0)
    check("alla quindicesima la porta si chiude", guard.admission_open, False)
    check("l'evento e' ADMISSION_STOP", guard.event["event"], "ADMISSION_STOP")

    print("\n  -- l'evento porta tutto quello che deve portare --")
    for field in ("event", "ts", "epoch", "reason", "p50_ms", "p95_ms", "p99_ms",
                  "phase", "population_authenticated", "population_active",
                  "completed", "pending", "consecutive_evaluations",
                  "samples_in_window", "p95_limit_ms"):
        check(f"    campo {field}", field in guard.event, True)
    check("    e il file e' stato scritto quando e' scattato",
          json.load(open(os.path.join(SANDBOX, "g1.json")))["event"]["event"],
          "ADMISSION_STOP")

    print("\n  -- la porta non si riapre mai piu' --")
    for step in range(30):
        feed(guard, 50.0, 1100.0 + step)
        guard.evaluate(1100.0 + step)
    check("trenta valutazioni ottime non riaprono la porta", guard.admission_open, False)
    check("e l'evento resta quello di prima", guard.event["consecutive_evaluations"], 15)

    print("\n  -- un singolo secondo lento non chiude niente --")
    # Un secondo solo oltre soglia resta nella finestra per dieci valutazioni,
    # quindi produce dieci violazioni, non una. Dieci e' sotto le quindici, e la
    # porta resta aperta. E' la protezione che serve.
    guard = guard_for("g2.json")
    play(guard, [100.0] * 5 + [3000.0] + [100.0] * 30, first=2000.0)
    check("trentasei valutazioni con un secondo lento: porta aperta",
          guard.admission_open, True)
    check("le violazioni prodotte dal secondo lento sono dieci",
          guard.peak_consecutive, 10)
    check("e alla fine il conteggio e' tornato a zero", guard.consecutive, 0)

    print("\n  -- una sequenza breve non chiude, e il conteggio si azzera --")
    # Tre secondi lenti: dodici violazioni consecutive mentre la finestra li
    # smaltisce, sotto le quindici. Poi il conteggio torna a zero da se'.
    # Cinque secondi lenti ne farebbero quindici: e' la soglia effettiva.
    guard = guard_for("g3.json")
    play(guard, [3000.0] * 3 + [100.0] * 20, first=3000.0)
    check("tre secondi lenti: porta aperta", guard.admission_open, True)
    check("dodici violazioni consecutive, non quindici", guard.peak_consecutive, 12)
    check("il conteggio si azzera quando la finestra si e' svuotata",
          guard.consecutive, 0)

    print("\n  -- una lentezza sostenuta chiude: e' il caso che deve chiudere --")
    guard = guard_for("g3b.json")
    play(guard, [3000.0] * 30, first=3500.0)
    check("trenta secondi lenti: porta chiusa", guard.admission_open, False)
    check("scattata alla quindicesima valutazione",
          guard.event["consecutive_evaluations"], 15)

    print("\n  -- esattamente sulla soglia non e' una violazione --")
    guard = guard_for("g4.json")
    for step in range(20):
        feed(guard, 1500.0, 4000.0 + step)
        guard.evaluate(4000.0 + step)
    check("venti valutazioni a 1500 ms esatti: porta aperta", guard.admission_open, True)
    check("nessuna violazione contata", guard.breaches, 0)

    print("\n  -- una finestra illeggibile non chiude e non azzera --")
    guard = guard_for("g5.json")
    for step in range(10):
        feed(guard, 2000.0, 5000.0 + step)
        guard.evaluate(5000.0 + step)
    check("dieci oltre soglia", guard.consecutive, 10)
    guard.samples = []
    feed(guard, 2000.0, 5010.0, count=5)
    record = guard.evaluate(5010.0)
    check("cinque campioni sono troppo pochi", record["verdict"], "illeggibile")
    check("il conteggio non e' azzerato", guard.consecutive, 10)
    check("e non e' incrementato", record["consecutive"], 10)
    for step in range(5):
        feed(guard, 2000.0, 5020.0 + step)
        guard.evaluate(5020.0 + step)
    check("altre cinque oltre soglia chiudono la porta alla quindicesima",
          guard.admission_open, False)

    print("\n  -- la finestra e' mobile: le latenze vecchie escono --")
    guard = guard_for("g6.json")
    feed(guard, 5000.0, 6000.0, count=100)
    record = guard.evaluate(6005.0)
    check("a cinque secondi le vecchie sono ancora dentro", record["samples"], 100)
    record = guard.evaluate(6011.0)
    check("a undici secondi sono uscite tutte", record["samples"], 0)
    check("e la finestra vuota e' illeggibile, non una violazione",
          record["verdict"], "illeggibile")

    print("\n  -- la guardia non tocca la bandiera di stop del memory guard --")
    flag = StopFlag()
    guard = guard_for("g7.json")
    for step in range(20):
        feed(guard, 9000.0, 7000.0 + step)
        guard.evaluate(7000.0 + step)
    check("la porta e' chiusa", guard.admission_open, False)
    check("ma la corsa non e' fermata", flag.stopped, False)
    check("e nessuna ragione di stop e' registrata", flag.reason_list, [])
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
