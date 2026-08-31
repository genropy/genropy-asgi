"""Direct checks of the stop lifecycle, with no laboratory and no Docker.

A fabricated ``/proc`` stands in for the host's: one directory per pid, each with
a ``stat`` and a ``root/sys/fs/cgroup`` holding the three gauges and
``memory.events``. Everything else is the real code — the flag, the reader, the
guard's loop, the verdict.

The central thesis, inherited from the previous campaign's guard and worth
keeping: A READ THAT IS NOT A PLAIN BYTE COUNT NEVER BECOMES A ZERO. A zero is a
fact about memory; an unreadable gauge is a fact about the instrument.

The fixture writes a process name containing a space and brackets —
``(gnr asgi (lab))`` — because that is the form that breaks a naive field count
in ``/proc/<pid>/stat``.

    python3 test_stop_guard.py
"""

import os
import shutil
import signal
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                os.pardir, os.pardir)))

from bench_common.stop_guard import (                                            # noqa: E402
    ContainerCgroup, MemoryGuard, StopFlag, StopRequested, Unreadable)

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got!r}"
          + ("" if ok else f"  atteso {want!r}"))
    if not ok:
        failures.append(label)


def wait_until(predicate, label, timeout=15.0, step=0.02):
    """Poll a condition up to a deadline. Never waits forever, never sleeps blind.

    Returns True when the condition held. On a timeout it returns False and the
    caller reports what was still not true, so a failure names the condition
    instead of a number of milliseconds that happened not to be enough.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    print(f"  [ATTESA SCADUTA dopo {timeout:.0f}s] {label}")
    return False


def check_raises(label, exception, call):
    try:
        call()
    except exception as failure:
        print(f"  [ok  ] {label}: {type(failure).__name__}")
        return
    except BaseException as other:                                # noqa: BLE001
        print(f"  [FAIL] {label}: alzata {type(other).__name__} invece di {exception.__name__}")
        failures.append(label)
        return
    print(f"  [FAIL] {label}: nessuna eccezione")
    failures.append(label)


SANDBOX = tempfile.mkdtemp(prefix="stop_guard_")


def make_process(pid=4242, start="998877"):
    """A pid in the fabricated /proc, with a name that contains ') ('.

    Field 22 of ``/proc/<pid>/stat`` is the start time, and the fields before it
    are pid and comm: after the last ``)`` the start time is therefore the
    twentieth token. The name here deliberately carries a space and two nested
    brackets, because that is the shape that breaks a naive field count.
    """
    directory = os.path.join(SANDBOX, "proc", str(pid))
    os.makedirs(os.path.join(directory, "root", "sys", "fs", "cgroup"), exist_ok=True)
    #        stato  ppid  + 17 campi                      + starttime (campo 22)
    tail = ["S", "1"] + ["0"] * 17 + [start] + ["0"] * 30
    with open(os.path.join(directory, "stat"), "w") as handle:
        handle.write(f"{pid} (gnr asgi (lab)) " + " ".join(tail) + "\n")
    return directory


def write_gauges(pid=4242, current=100 * 1048576, limit=2 * 1073741824,
                 peak=None, events=None):
    root = os.path.join(SANDBOX, "proc", str(pid), "root", "sys", "fs", "cgroup")
    os.makedirs(root, exist_ok=True)
    counters = {"low": 0, "high": 0, "max": 0, "oom": 0, "oom_kill": 0, "oom_group_kill": 0}
    counters.update(events or {})
    pairs = [("memory.current", str(current)),
             ("memory.max", "max" if limit is None else str(limit)),
             ("memory.peak", str(peak if peak is not None else current)),
             ("memory.events", "".join(f"{n} {v}\n" for n, v in counters.items()))]
    for name, value in pairs:
        write_atomically(os.path.join(root, name), value)
    return root


def write_atomically(path, value):
    """Replace a gauge file in one step, never leaving it empty.

    The kernel never shows a half-written gauge, so a fixture must not either.
    ``open(path, "w")`` truncates first: a reader arriving between the truncate
    and the write sees an empty file, and the guard — correctly — raises
    ``Unreadable`` rather than inventing a zero. That killed the guard thread in
    six runs out of eight on Linux, where the guard samples twenty times a second
    while this function rewrites the files. The temporary file plus ``os.replace``
    makes the swap atomic, so the race cannot happen and the test measures the
    guard instead of the fixture.
    """
    if not value.endswith("\n"):
        value += "\n"
    temporary = path + ".nuovo"
    with open(temporary, "w") as handle:
        handle.write(value)
    os.replace(temporary, path)


def cgroup_for(pid=4242):
    """A ContainerCgroup bound to the fabricated tree, no docker call."""
    reader = ContainerCgroup.__new__(ContainerCgroup)
    reader.container = "finto"
    reader.proc_dir = os.path.join(SANDBOX, "proc")
    reader.cgroup_dir = "/sys/fs/cgroup"
    reader.pid = pid
    reader.start_time = reader.read_start_time(pid)
    return reader


try:
    print("\n== la bandiera: una condizione, molti scrittori ==")
    flag = StopFlag()
    check("nasce abbassata", flag.stopped, False)
    flag.ask_stop("signal", "SIGTERM")
    check("una richiesta la alza", flag.stopped, True)
    check("la causa e' la prima", flag.first_reason["source"], "signal")
    flag.ask_stop("memory_guard", "soglia")
    check("le richieste successive si accodano", len(flag.reason_list), 2)
    check("la causa resta la prima", flag.first_reason["source"], "signal")
    check_raises("una fase alzata la vede", StopRequested,
                 lambda: flag.raise_if_stopped("prova"))

    print("\n== l'attesa lunga si interrompe subito ==")
    flag = StopFlag()
    started = time.time()
    threading.Timer(0.3, lambda: flag.ask_stop("signal", "SIGINT")).start()
    completed = flag.wait(30.0, "riposo lungo") if not flag.stopped else None
    elapsed = time.time() - started
    check("un riposo da 30s cede in meno di 2s", elapsed < 2.0, True)
    check("l'attesa dichiara di non essere finita", completed, False)

    print("\n== TERM e INT alzano la stessa bandiera ==")
    for number, name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")):
        flag = StopFlag()
        flag.install_signal_handlers()
        os.kill(os.getpid(), number)
        time.sleep(0.1)
        check(f"{name} alza la bandiera", flag.stopped, True)
        check(f"{name} si dichiara", flag.first_reason["detail"], name)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.default_int_handler)

    print("\n== il misuratore legge, o dichiara di non saper leggere ==")
    make_process()
    write_gauges(current=512 * 1048576, limit=2 * 1073741824)
    reader = cgroup_for()
    gauges = reader.gauges
    check("current in byte", gauges["current"], 512 * 1048576)
    check("max in byte", gauges["max"], 2 * 1073741824)
    check("i sei contatori", sorted(gauges["events"]),
          ["high", "low", "max", "oom", "oom_group_kill", "oom_kill"])
    check("l'identita' regge", reader.identity_stable, True)

    write_gauges(limit=None)
    check("memory.max = 'max' diventa None, non zero", cgroup_for().gauges["max"], None)

    root = write_gauges()
    for value, what in (("", "vuoto"), ("abc", "non numerico"), ("-5", "negativo")):
        with open(os.path.join(root, "memory.current"), "w") as handle:
            handle.write(value + "\n")
        check_raises(f"un current {what} non diventa zero", Unreadable,
                     lambda: cgroup_for().gauges)
    write_gauges()
    os.remove(os.path.join(root, "memory.current"))
    check_raises("un gauge assente non diventa zero", Unreadable,
                 lambda: cgroup_for().gauges)
    write_gauges()
    with open(os.path.join(root, "memory.events"), "w") as handle:
        handle.write("low 0\nhigh 0\n")
    check_raises("memory.events senza i contatori di pressione", Unreadable,
                 lambda: cgroup_for().gauges)

    print("\n== il pid: sparito, o diventato un altro ==")
    write_gauges()
    reader = cgroup_for()
    shutil.move(os.path.join(SANDBOX, "proc", "4242"),
                os.path.join(SANDBOX, "proc", "4242_morto"))
    check("un pid sparito non e' stabile", reader.identity_stable, False)
    shutil.move(os.path.join(SANDBOX, "proc", "4242_morto"),
                os.path.join(SANDBOX, "proc", "4242"))
    make_process(start="111111")
    check("lo stesso pid con altro start time non e' stabile", reader.identity_stable, False)

    print("\n== il guardiano: soglia, pressione, e non smette di guardare ==")
    make_process()
    write_gauges(current=100 * 1048576, limit=1000 * 1048576)
    flag = StopFlag()
    guard = MemoryGuard(cgroup_for(), flag, os.path.join(SANDBOX, "guard.json"),
                        threshold_percent=80.0, sample_seconds=0.05)
    guard.read_baseline()
    check("al 10% non chiede niente", guard.judge(guard.cgroup.gauges), None)
    write_gauges(current=850 * 1048576, limit=1000 * 1048576)
    verdict = guard.judge(guard.cgroup.gauges)
    check("all'85% chiede lo stop", verdict is not None, True)
    check("e lo dice in chiaro", "soglia" in (verdict or ""), True)

    write_gauges(current=100 * 1048576, limit=1000 * 1048576, events={"oom_kill": 1})
    verdict = guard.judge(guard.cgroup.gauges)
    check("un oom_kill cresciuto chiede lo stop anche al 10%", verdict is not None, True)
    check("e nomina il rifiuto del kernel", "rifiutato" in (verdict or ""), True)

    write_gauges(current=100 * 1048576, limit=1000 * 1048576)
    flag = StopFlag()
    guard = MemoryGuard(cgroup_for(), flag, os.path.join(SANDBOX, "guard2.json"),
                        threshold_percent=80.0, sample_seconds=0.05)
    guard.read_baseline()
    guard.start()
    wait_until(lambda: len(guard.samples) >= 1, "il guardiano ha preso un campione")
    check("il guardiano campiona appena parte", len(guard.samples) >= 1, True)
    write_gauges(current=900 * 1048576, limit=1000 * 1048576)
    wait_until(lambda: flag.stopped, "la bandiera si e' alzata")
    check("il ciclo chiede lo stop da se'", flag.stopped, True)
    check("e il thread e' vivo, non morto su una lettura", guard.error, None)
    asked_rows = len(guard.samples)
    wait_until(lambda: len(guard.samples) > asked_rows,
               f"i campioni sono cresciuti oltre {asked_rows}")
    check("dopo aver chiesto CONTINUA a campionare", len(guard.samples) > asked_rows, True)
    guard.driver_finished.set()
    guard.join(timeout=15)
    check("si ferma quando il driver dichiara di aver finito", guard.is_alive(), False)
    check("e non e' morto per un errore", guard.error, None)

    print("\n== il controllo finale, sempre, contro la baseline ==")
    write_gauges(current=100 * 1048576, limit=1000 * 1048576, events={"oom": 3})
    verdict = guard.final_check()
    check("il controllo finale vede la crescita di oom", verdict["pressure_delta"]["oom"], 3)
    check("e la dichiara un fallimento di sicurezza", verdict["safety_fail"], True)
    check("registra il memory stop avvenuto", verdict["memory_stop"], True)
    path = guard.write(verdict)
    check("scrive il suo verdetto su disco", os.path.getsize(path) > 0, True)

    write_gauges(current=100 * 1048576, limit=1000 * 1048576)
    flag = StopFlag()
    quiet = MemoryGuard(cgroup_for(), flag, os.path.join(SANDBOX, "guard3.json"))
    quiet.read_baseline()
    verdict = quiet.final_check()
    check("una corsa sana non e' un fallimento di sicurezza", verdict["safety_fail"], False)
    check("e non dichiara memory stop", verdict["memory_stop"], False)

    print("\n== una lettura impossibile alla fine non diventa un verdetto sano ==")
    write_gauges()
    guard = MemoryGuard(cgroup_for(), StopFlag(), os.path.join(SANDBOX, "guard4.json"))
    guard.read_baseline()
    shutil.rmtree(os.path.join(SANDBOX, "proc", "4242", "root"))
    verdict = guard.final_check()
    check("il verdetto registra l'errore di lettura", verdict["final_read_error"] is not None, True)
    check("e non finge un delta zero", verdict["final"], None)
finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)

print("\n" + "=" * 50)
if failures:
    print(f"FALLITI {len(failures)}:")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("tutti i controlli passati")
