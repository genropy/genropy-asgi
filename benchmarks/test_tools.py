"""A few direct checks of the drivers, runnable without a laboratory.

Not a suite: the four things that, if wrong, would make the campaign produce
numbers that look fine and mean nothing.

    python3 test_tools.py
"""

import argparse
import inspect
import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import population_bench as pb  # noqa: E402
import session_bench as sb  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got!r}"
          + ("" if ok else f"  atteso {want!r}"))
    if not ok:
        failures.append(label)


def make_user(quiet_seconds=3600):
    user = sb.EmulatedUser.__new__(sb.EmulatedUser)
    user.username = "loaduser0001"
    user.identity = sb.SessionIdentityMap()
    old = time.time() - quiet_seconds
    user.last_user_event_ts = user.last_rpc_ts = old
    return user, old


print("\n== l'attività: il lavoro la aggiorna, il ping no ==")
user, old = make_user()
user.touch_activity({"rpc_method": "app.getSelection"})
check("una richiesta di lavoro aggiorna l'attività", user.last_rpc_ts > old, True)

user, old = make_user()
before = (user.last_user_event_ts, user.last_rpc_ts)
user.get_client_clock("_lastRpc")          # what a ping sends
check("il ping riporta e non aggiorna",
      (user.last_user_event_ts, user.last_rpc_ts), before)

print("\n== i timestamp registrati non vengono rigiocati ==")
user, _ = make_user()
fossil = "2026-08-27 08:18:06::DH"
form = user.get_adapted_form({"rpc_method": "app.getSelection",
                              "form": {"method": "app.getSelection",
                                       "_lastRpc": fossil,
                                       "_lastUserEventTs": fossil}})
check("il valore del 27 agosto è sostituito", form["_lastRpc"] != fossil, True)
check("gli altri campi restano", form["method"], "app.getSelection")

print("\n== la rampa si ferma dopo due finestre sopra il p95 ==")
arguments = argparse.Namespace(saturation_window=3, p95_limit=1000.0)
bench = types.SimpleNamespace(arguments=arguments, started_at=time.time())
guard = sb.SaturationGuard(bench, "/tmp/guard_check.csv")
for _ in range(3):
    guard.take_second([0.010] * 50, 0, [], 100)
check("una finestra veloce non ferma nulla", guard.saturated.is_set(), False)
for _ in range(3):
    guard.take_second([2.0] * 50, 0, [], 120)
check("una sola finestra lenta non basta", guard.saturated.is_set(), False)
for _ in range(3):
    guard.take_second([2.0] * 50, 0, [], 130)
check("due finestre lente fermano", guard.saturated.is_set(), True)
check("il massimo sotto soglia è ricordato", guard.last_under, 100)

print("\n== il CSV al secondo porta il p95, ed è davvero il p95 ==")
check("l'intestazione dichiara p95", "p50_ms,p95_ms,p99_ms" in sb.SecondSampler.COLUMNS, True)
check("l'intestazione non dichiara più p90", "p90_ms" in sb.SecondSampler.COLUMNS, False)

# Ninety-one values at 10 ms and nine at 1000: the p90 sits in the fast block
# and the p95 in the slow one. A header renamed over a p90 computation would
# answer about 10 ms here, so the two figures cannot be confused.
sampler = sb.SecondSampler.__new__(sb.SecondSampler)
latencies = sorted([10.0] * 91 + [1000.0] * 9)
check("il p90 di questa serie è veloce", float(sampler.percentile(latencies, 90)) < 100, True)
check("il p95 di questa serie è lento", float(sampler.percentile(latencies, 95)) > 900, True)

written = inspect.getsource(sb.SecondSampler.run)
check("la riga scritta chiede il percentile 95", "milliseconds, 95" in written, True)
check("la riga scritta non chiede più il 90", "milliseconds, 90" in written, False)
check("population_bench usa lo STESSO campionatore", pb.SecondSampler is sb.SecondSampler, True)

print("\n== la Prova 3: operazioni in volo e prima chiamata ==")
resident = pb.ResidentUser.__new__(pb.ResidentUser)
threading.Thread.__init__(resident, daemon=True)
resident.bench = types.SimpleNamespace(body_rows=[None] * 6)
resident.work_event = threading.Event()
resident.exit_event = threading.Event()
resident.in_flight = False
resident.requested = resident.started = resident.completed = resident.coalesced = 0
resident.operations = 0
resident.first_call_after_wake_ms = resident.operation_after_wake_ms = None
resident.awaiting_first_call = False


def replay_call(record, round_number, sequence, late_ms):
    if not resident.awaiting_first_call:
        time.sleep(0.05)
        return
    began = time.time()
    time.sleep(0.05)
    resident.first_call_after_wake_ms = round((time.time() - began) * 1000, 1)
    resident.awaiting_first_call = False


resident.replay_call = replay_call
resident.play = lambda rows, number: [resident.replay_call(row, number, index, 0)
                                      for index, row in enumerate(rows)]
resident.work_once(after_wake=True)
resident.work_event.clear()
began = time.time()
resident.play_one_operation()
whole = (time.time() - began) * 1000
print(f"        raffica intera {whole:.0f} ms, prima chiamata "
      f"{resident.first_call_after_wake_ms} ms")
check("la prima chiamata non è la raffica intera",
      resident.first_call_after_wake_ms < whole / 3, True)
check("in volo torna falso a fine operazione", resident.in_flight, False)
resident.in_flight = True
check("una richiesta durante un'operazione è coalescente",
      (resident.work_once(), resident.coalesced), (False, 1))

print("\n" + "=" * 50)
if failures:
    print(f"FALLITI {len(failures)}:")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("tutti i controlli passati")
