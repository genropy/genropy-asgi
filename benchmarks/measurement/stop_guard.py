# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""The stop lifecycle every benchmark driver shares: one flag, three writers.

A measurement run must be interruptible at any instant and must always leave the
laboratory as it found it. Three things can ask for a stop, and all three raise
the SAME flag, so the driver has one condition to check and one cleanup path:

- the operator, with TERM or INT;
- the memory guard, when the container approaches its limit;
- the driver itself, when a structural criterion fails.

``StopFlag`` is that flag. ``MemoryGuard`` is the watcher that raises it on
memory. Both are stack-agnostic: they know nothing of bridge or legacy.

Why a flag and not an exception: the phases of a run are long loops — a
population of two thousand logins, a rest longer than the freeze window, a slow
rotation. An exception can only interrupt where it is raised; a flag can be read
at the top of every iteration, which is what "the driver controls the stop
during population, work, rotation, rest and logout" requires.

Three defects of the previous campaign are closed here by construction:

- the old MEMORY_STOP only blocked new arrivals: ``StopFlag`` is read by every
  phase, so work already under way stops too;
- the old driver ran as PID 1 and ignored TERM: ``install_signal_handlers``
  registers both TERM and INT onto the same flag, and the container declares
  ``init: true`` so a signal reaches the process at all;
- the old guard stopped observing the moment it wrote its stop file:
  ``MemoryGuard`` keeps sampling until the driver actually finishes, and its
  ``final_check`` compares ``memory.events`` against the baseline no matter how
  the run ended.

PROVISIONAL: the escalation after TERM is deliberately absent. The guard asks,
waits, and reports what it saw; it never kills. A kill would truncate the
writers and lose the samples that explain why the memory grew, which is the one
thing the run exists to record. If a driver ever hangs past the wait, that is a
defect to fix in the driver, not a signal to escalate.
"""

import json
import os
import signal
import subprocess
import threading
import time

# The counters whose growth means the kernel already refused memory.
PRESSURE_COUNTERS = ("max", "oom", "oom_kill", "oom_group_kill")


class StopRequested(RuntimeError):
    """Raised by a driver that decided to honour the stop flag and unwind."""


class StopFlag:
    """One stop condition, many writers, every phase a reader.

    The flag records WHO asked and WHY, because the report must distinguish a
    memory stop — which is a safety failure — from an operator interruption and
    from a structural failure of the run. ``reason`` is the whole record, never
    just a boolean.
    """

    def __init__(self):
        self.event = threading.Event()
        self.reasons = []
        self.lock = threading.Lock()

    @property
    def stopped(self):
        """True once anybody has asked for a stop."""
        return self.event.is_set()

    @property
    def first_reason(self):
        """The record that stopped the run, or None while it runs."""
        with self.lock:
            return dict(self.reasons[0]) if self.reasons else None

    @property
    def reason_list(self):
        """Every stop request, in order: the first one is the cause."""
        with self.lock:
            return [dict(record) for record in self.reasons]

    def ask_stop(self, source, detail, **numbers):
        """Raise the flag. Safe from a signal handler and from any thread."""
        with self.lock:
            self.reasons.append({"source": source, "detail": detail,
                                 "ts": time.time(), "numbers": numbers})
        self.event.set()

    def raise_if_stopped(self, where):
        """The guard clause a phase puts at the top of its loop."""
        if self.event.is_set():
            first = self.first_reason
            raise StopRequested(f"{where}: stop chiesto da {first['source']} — {first['detail']}")

    def wait(self, seconds, where):
        """Sleep in short steps so a stop is honoured within a tenth of a second.

        Returns True if the whole time elapsed, False if a stop cut it short.
        The long rests of the population scenario are the reason this exists:
        a plain ``time.sleep(600)`` would ignore a TERM for ten minutes.
        """
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.event.wait(min(0.1, max(0.0, deadline - time.time()))):
                return False
        self.raise_if_stopped(where)
        return True

    def install_signal_handlers(self):
        """TERM and INT set the same flag. Nothing else changes."""
        def handler(number, _frame):
            self.ask_stop("signal", signal.Signals(number).name, signal=number)
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)


class Unreadable(RuntimeError):
    """A gauge did not answer, or answered something that is not a byte count.

    This exists so that a bad read can never become a zero. A zero is a fact
    about memory; ``Unreadable`` is a fact about the instrument, and the two must
    never be confused — the previous campaign's guard was written around this
    same rule and it is the reason it caught a real OOM.
    """


class ContainerCgroup:
    """The cgroup gauges of one container, read FROM THE HOST.

    Nothing runs inside the container. ``docker inspect`` is called ONCE, to
    learn the pid of the container's init; from then on the gauges are read at
    ``/proc/<pid>/root/sys/fs/cgroup``, which is the container's own cgroup seen
    through its root. This is the technique the previous campaign proved, and the
    reason for it is measurable: a ``docker exec`` per sample adds a process to
    the very container whose memory is being watched, and four reads a second
    would be four processes a second inside the measure.

    The identity is the pid PLUS its start time — field 22 of ``/proc/<pid>/stat``,
    counted from the last ``)`` so a process name containing spaces and brackets
    cannot shift the count. A recycled pid is therefore not mistaken for the same
    process.

    ``proc_dir`` and ``cgroup_dir`` are parameters, not constants, so the whole
    reader can be exercised offline against a fabricated ``/proc``.
    """

    GAUGES = ("memory.current", "memory.max", "memory.peak")
    EVENT_KEYS = ("low", "high", "max", "oom", "oom_kill", "oom_group_kill")

    def __init__(self, container, proc_dir="/proc", cgroup_dir="/sys/fs/cgroup"):
        self.container = container
        self.proc_dir = proc_dir
        self.cgroup_dir = cgroup_dir
        self.pid = self.read_pid()
        self.start_time = self.read_start_time(self.pid)

    def read_pid(self):
        """The pid of the container's init, from one docker inspect."""
        done = subprocess.run(["docker", "inspect", "--format", "{{.State.Pid}}", self.container],
                              capture_output=True, text=True, timeout=20)
        if done.returncode != 0:
            raise Unreadable(f"container {self.container} non ispezionabile: "
                             f"{done.stderr.strip()}")
        text = done.stdout.strip()
        if not text.isdigit() or int(text) <= 0:
            raise Unreadable(f"pid non valido per {self.container}: {text!r}")
        return int(text)

    def read_start_time(self, pid):
        """Field 22 of /proc/<pid>/stat, counted from the last ')'."""
        try:
            with open(os.path.join(self.proc_dir, str(pid), "stat")) as handle:
                text = handle.read()
        except OSError as failure:
            raise Unreadable(f"stat del pid {pid} illeggibile: {failure}") from failure
        tail = text.rpartition(")")[2].split()
        if len(tail) < 20:
            raise Unreadable(f"stat del pid {pid} troppo corto: {len(tail)} campi dopo ')'")
        return tail[19]

    @property
    def cgroup_root(self):
        """The container's cgroup, seen from the host through its root."""
        return os.path.join(self.proc_dir, str(self.pid), "root",
                            self.cgroup_dir.lstrip("/"))

    @property
    def identity_stable(self):
        """False when the pid is gone, or is now a different process."""
        try:
            return self.read_start_time(self.pid) == self.start_time
        except Unreadable:
            return False

    def read_number(self, name):
        """One gauge as an integer, or Unreadable. 'max' means no limit."""
        path = os.path.join(self.cgroup_root, name)
        try:
            with open(path) as handle:
                text = handle.read().strip()
        except OSError as failure:
            raise Unreadable(f"{name} illeggibile: {failure}") from failure
        if text == "max":
            return None
        if not text or not text.lstrip("-").isdigit():
            raise Unreadable(f"{name} non e' un numero: {text!r}")
        value = int(text)
        if value < 0:
            raise Unreadable(f"{name} negativo: {value}")
        return value

    def read_events(self):
        """The six counters of memory.events, each an integer or Unreadable."""
        path = os.path.join(self.cgroup_root, "memory.events")
        try:
            with open(path) as handle:
                lines = handle.read().splitlines()
        except OSError as failure:
            raise Unreadable(f"memory.events illeggibile: {failure}") from failure
        events = {}
        for line in lines:
            name, _, value = line.partition(" ")
            value = value.strip()
            if name in self.EVENT_KEYS:
                if not value or not value.isdigit():
                    raise Unreadable(f"memory.events {name} non e' un numero: {value!r}")
                events[name] = int(value)
        missing = [name for name in PRESSURE_COUNTERS if name not in events]
        if missing:
            raise Unreadable(f"memory.events senza i contatori {missing}")
        return events

    @property
    def gauges(self):
        """current, max and peak in bytes, plus the events counters.

        ``max`` is None when the cgroup declares no limit, so a caller must
        decide what a percentage means before dividing.
        """
        return {"current": self.read_number("memory.current"),
                "max": self.read_number("memory.max"),
                "peak": self.read_number("memory.peak"),
                "events": self.read_events()}


class MemoryGuard(threading.Thread):
    """Watches one container's memory and asks for a stop before the kernel does.

    It samples the gauges, records every sample, and raises the stop flag on
    either of two conditions:

    - ``memory.current`` above ``threshold_percent`` of ``memory.max``;
    - any of ``max``, ``oom``, ``oom_kill``, ``oom_group_kill`` above its
      baseline, which means the kernel has already refused memory.

    After asking, it does NOT stop watching: it keeps sampling until the driver
    reports itself finished, because what the memory did during the shutdown is
    part of the evidence. ``final_check`` is always run by the runner, whatever
    the outcome, and compares the counters against the baseline one last time.
    """

    def __init__(self, cgroup, stop_flag, out_path, threshold_percent=80.0,
                 sample_seconds=2.0, wait_after_ask_seconds=120.0):
        super().__init__(daemon=True, name="memory-guard")
        self.cgroup = cgroup
        self.stop_flag = stop_flag
        self.out_path = out_path
        self.threshold_percent = threshold_percent
        self.sample_seconds = sample_seconds
        self.wait_after_ask_seconds = wait_after_ask_seconds
        self.driver_finished = threading.Event()
        self.samples = []
        self.baseline = None
        self.asked_at = None
        self.error = None

    def read_baseline(self):
        """The gauges before the run: every later judgement is against these."""
        self.baseline = self.cgroup.gauges
        return self.baseline

    @property
    def pressure_delta(self):
        """How much each pressure counter grew since the baseline."""
        if not self.samples or self.baseline is None:
            return {}
        last = self.samples[-1]["events"]
        return {name: last.get(name, 0) - self.baseline["events"].get(name, 0)
                for name in PRESSURE_COUNTERS}

    def occupancy_percent(self, sample):
        """Percent of the declared limit, or None when the cgroup has no limit."""
        limit = sample.get("max")
        if not limit:
            return None
        return sample["current"] / limit * 100.0

    def judge(self, sample):
        """The reason to stop on this sample, or None to keep going."""
        grown = {name: sample["events"].get(name, 0) - self.baseline["events"].get(name, 0)
                 for name in PRESSURE_COUNTERS}
        refused = {name: delta for name, delta in grown.items() if delta > 0}
        if refused:
            return f"il kernel ha rifiutato memoria: {refused}"
        occupancy = self.occupancy_percent(sample)
        if occupancy is not None and occupancy >= self.threshold_percent:
            return (f"memory.current {sample['current'] / 1048576:.0f} MB = "
                    f"{occupancy:.1f}% del limite, soglia {self.threshold_percent:.0f}%")
        if not self.cgroup.identity_stable:
            return "il container e' stato ricreato durante la corsa: identita' perduta"
        return None

    def run(self):
        try:
            self.watch()
        except BaseException as failure:                      # noqa: BLE001
            self.error = repr(failure)
            self.stop_flag.ask_stop("memory_guard", f"guardiano morto: {self.error}")

    def watch(self):
        """Sample until the driver is finished; ask for a stop when judged."""
        if self.baseline is None:
            self.read_baseline()
        while not self.driver_finished.is_set():
            started = time.time()
            sample = self.cgroup.gauges
            sample["ts"] = started
            sample["occupancy_percent"] = self.occupancy_percent(sample)
            self.samples.append(sample)
            if self.asked_at is None:
                verdict = self.judge(sample)
                if verdict:
                    self.asked_at = started
                    self.stop_flag.ask_stop("memory_guard", verdict,
                                            current=sample["current"], max=sample["max"],
                                            occupancy_percent=sample["occupancy_percent"])
                    print(f"!!! MEMORY STOP: {verdict}", flush=True)
            elif started - self.asked_at > self.wait_after_ask_seconds:
                print(f"!!! il driver non ha concluso entro "
                      f"{self.wait_after_ask_seconds:.0f}s dallo stop: continuo a osservare",
                      flush=True)
                self.asked_at = started
            self.driver_finished.wait(max(0.0, self.sample_seconds - (time.time() - started)))

    def final_check(self):
        """Always run, however the run ended. Returns the verdict record."""
        last, read_error = None, None
        try:
            last = self.cgroup.gauges
        except RuntimeError as failure:
            read_error = str(failure)
        record = {
            "baseline": self.baseline,
            "final": last,
            "final_read_error": read_error,
            "samples": len(self.samples),
            "threshold_percent": self.threshold_percent,
            "asked_at": self.asked_at,
            "memory_stop": self.asked_at is not None,
            "guard_error": self.error,
            "container": self.cgroup.container,
            "container_pid": self.cgroup.pid,
            "container_start_time": self.cgroup.start_time,
            "identity_stable": self.cgroup.identity_stable,
        }
        if self.baseline is not None and last is not None:
            record["pressure_delta"] = {
                name: last["events"].get(name, 0) - self.baseline["events"].get(name, 0)
                for name in PRESSURE_COUNTERS}
        else:
            record["pressure_delta"] = self.pressure_delta
        record["safety_fail"] = bool(record["memory_stop"]
                                     or any(v > 0 for v in record["pressure_delta"].values()))
        return record

    def write(self, verdict):
        """The guard's whole record on disk: samples plus verdict."""
        with open(self.out_path, "w") as handle:
            json.dump({"verdict": verdict, "samples": self.samples}, handle, indent=2)
        return self.out_path
