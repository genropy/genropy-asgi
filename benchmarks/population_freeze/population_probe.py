# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""One leg of the population run: a large authenticated population, a small working set.

Eight phases, in order: populate, rest, wake, work, rotate, rest2, logout,
observe. The plan file decides who enters when, how long each pause lasts and
exactly which users swap in and out of the working set, so the legacy leg and the
bridge leg replay the same run.

WHAT MAKES THIS MEASURABLE, and it is one property: THE SILENT USERS SEND
NOTHING. A user outside the working set has no thread, no timer and no ping. Two
facts make that necessary rather than tidy:

- the bridge lets a client push its own activity clock forward through
  ``/_ping`` — the core defends ``last_refresh_ts`` but not the two clocks the
  freeze judge reads — so a driver that pinged would be deciding its own freeze;
- a ``GET /`` without a cookie makes the site coin a guest, so anonymous polling
  would inflate the population at every round.

The only reads this driver makes are the census and the orchestration status,
both of which the pool's demux diverts before the hosted site: they stamp no
clock and coin no guest.

THREE MEASURES THE CORE DOES NOT OFFER, taken here:

- the FREEZE ITSELF is not journalled when it succeeds — only a refused one is —
  so the count of frozen users comes from the census, never from the journal;
- the THAW LATENCY has no timer anywhere. The thaw is synchronous inside the
  first request of a returning user, so what is timed here is that request, and
  separately the whole burst that follows it;
- the SIZE OF THE FROZEN DATA is measured with one ``du`` on the deposit.

FREEZE GRANULARITY: the vertex judges inactivity every twelve beats of five
seconds, on a photograph up to five seconds old. A freeze set at five minutes
happens between five minutes and about six minutes five seconds after the last
call. That band is a fact of the core's module constants and cannot be narrowed;
the plan's rest is long enough to clear it, and this driver refuses a plan whose
rest does not.

    python3 population_probe.py --stack bridge --run pop_pilot_bridge \\
        --base http://127.0.0.1:8098 --container genro-bench-lab-bridge-1 \\
        --plan traces/population_pilot.json --out /work/pop_pilot_bridge \\
        --journal /lab/runtime/pop_pilot_bridge_orders.decisions.jsonl \\
        --frozen-users-path /lab/projects/lab_bench/instances/bridge_lab/site/data/_frozen_users \\
        --expect-freeze-minutes 5
"""

import argparse
import csv
import hashlib
import http.client
import json
import os
import statistics
import sys
import threading
import time
import urllib.parse

BENCHMARKS_DIR = os.path.abspath(
    os.environ.get("BENCHMARKS_DIR")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
sys.path.insert(0, BENCHMARKS_DIR)

from bench_common.bridge_eyes import BridgeEyes                                  # noqa: E402
from bench_common.container_probe import (                                       # noqa: E402
    BridgeRoles, ContainerProbe, LegacyRoles, RoleSample)
from bench_common.stop_guard import (                                            # noqa: E402
    ContainerCgroup, MemoryGuard, StopFlag, StopRequested)
from churn_driver import LoggedUser, build_plan, load_capture                    # noqa: E402

SESSION_CAPTURE = os.path.join(BENCHMARKS_DIR, "session_capture.jsonl")
USERNAMES_ALL = os.path.join(BENCHMARKS_DIR, "usernames_all.txt")
SAMPLE_S = 5.0
# La banda di incertezza del freeze: 12 battiti da 5s piu' la freschezza della
# foto del worker. Sono costanti di modulo del core, non configurazione.
FREEZE_UNCERTAINTY_S = 65.0

PHASE_COLUMNS = ["ts", "epoch", "phase", "note", "elapsed_s",
                 "authenticated", "placed", "frozen", "unplaced", "guest",
                 "connections", "pages", "worker_count", "workers", "worker_pids",
                 "per_worker", "memory_occupied_percent",
                 "cg_current", "cg_peak", "pss_total_kb", "pss_by_role",
                 "frozen_bytes", "frozen_folders", "frozen_pickles",
                 "active_users", "calls_done", "calls_failed"]

SAMPLE_COLUMNS = ["ts", "epoch", "run", "stack", "phase", "active_users",
                  "calls_done", "calls_failed", "p50_ms", "p95_ms",
                  "process_count", "cpu_total_pct", "cpu_by_role", "cpu_workers_pct",
                  "pss_total_kb", "rss_total_kb", "pss_by_role", "pss_workers_kb",
                  "worker_count", "worker_roles",
                  "cg_current", "cg_peak", "cg_max", "cg_events",
                  "st_anon", "st_file", "st_kernel", "st_sock", "st_shmem",
                  "authenticated", "placed", "frozen", "unplaced", "guest",
                  "connections", "pages", "per_worker"]

CALL_COLUMNS = ["phase", "user", "started_at", "completed_at", "latency_ms",
                "status", "app_error", "transport_error", "kind"]


class InvalidRun(RuntimeError):
    """A structural criterion failed: the run's data is not comparable."""


class SamplerDown(RuntimeError):
    """The sampler produced nothing: there is no measure."""


class Resident:
    """One authenticated user. Works only while it belongs to the working set.

    A resident outside the working set holds its session and its connection and
    does nothing at all: no thread, no timer, no request. That is the whole point
    of the run — nineteen hundred and twenty of them must be genuinely silent.
    """

    def __init__(self, probe, label, logged_user, think_times):
        self.probe = probe
        self.label = label
        self.user = logged_user
        self.think_times = list(think_times) or [60.0]
        self.thread = None
        self.leave = threading.Event()
        self.bursts = 0
        self.first_call_ms = None
        self.reentry_ms = None
        self.failed = 0

    @property
    def active(self):
        return self.thread is not None and self.thread.is_alive()

    def activate(self, measure_reentry=False):
        """Give the resident a thread, and let it work until told to leave."""
        self.leave.clear()
        self.thread = threading.Thread(target=self.work_loop, args=(measure_reentry,),
                                       daemon=True, name=f"resident-{self.label}")
        self.thread.start()

    def deactivate(self, timeout=30.0):
        """Take the thread away. The session stays: nobody logs out here."""
        self.leave.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)
        alive = self.thread is not None and self.thread.is_alive()
        self.thread = None
        return not alive

    def work_loop(self, measure_reentry):
        """Burst, pause, burst — until the resident is told to leave."""
        first = True
        while not (self.leave.is_set() or self.probe.stop_flag.stopped):
            started = time.time()
            self.burst(measure_reentry and first)
            if measure_reentry and first:
                self.reentry_ms = round((time.time() - started) * 1000, 3)
                first = False
            pause = self.think_times[self.bursts % len(self.think_times)]
            self.leave.wait(pause)

    def burst(self, measure_first):
        """One recorded burst: for now one indexed call, as everywhere else."""
        lookup = self.probe.lookups[self.bursts % len(self.probe.lookups)]
        body = urllib.parse.urlencode(self.user.get_call_form(lookup, self.bursts + 100))
        started = time.time()
        status, app_error, transport_error = self.probe.send(self.user, body)
        completed = time.time()
        latency = round((completed - started) * 1000, 3)
        if measure_first:
            self.first_call_ms = latency
        self.bursts += 1
        if transport_error or app_error or status != 200:
            self.failed += 1
        self.probe.record_call({
            "phase": self.probe.phase, "user": self.label,
            "started_at": round(started, 6), "completed_at": round(completed, 6),
            "latency_ms": latency, "status": status if status is not None else "",
            "app_error": app_error or "", "transport_error": transport_error or "",
            "kind": "first_after_thaw" if measure_first else "burst",
        })


class PopulationProbe:
    """One leg: eight phases, one stack, the same plan as the other leg."""

    def __init__(self, arguments):
        self.arguments = arguments
        self.plan_sha256 = None
        self.plan = self.read_plan()
        self.protocol = self.plan["protocol"]
        self.check_plan()
        self.stop_flag = StopFlag()
        self.eyes = None
        if arguments.stack == "bridge":
            self.eyes = BridgeEyes(arguments.base, arguments.census, arguments.journal,
                                   container=arguments.container,
                                   frozen_users_path=arguments.frozen_users_path)
        self.roles = BridgeRoles() if arguments.stack == "bridge" else LegacyRoles()
        self.probe = ContainerProbe(arguments.container, self.roles)
        self.residents = {}
        self.phase = "start"
        self.phase_log = []
        self.logouts = []
        self.thaw = []
        self.rotation_log = []
        self.lock = threading.Lock()
        self.latencies = []
        self.calls_done = 0
        self.calls_failed = 0
        self.sampler_rows = 0
        self.sampler_error = None
        self.sampler_ready = threading.Event()
        self.sampler_failed = threading.Event()
        self.previous_ticks = {}
        self.previous_cpu_usec = None
        self.previous_stamp = None
        self.calls_writer = None
        self.calls_handle = None
        self.started_at = None
        self.lookups = [line.strip() for line in open(USERNAMES_ALL) if line.strip()][:32]

    def check_plan(self):
        """A plan whose rest cannot outlast the freeze band is not a plan."""
        rest = self.protocol["rest_seconds"]
        freeze = self.protocol["freeze_minutes"] * 60.0
        if rest <= freeze + FREEZE_UNCERTAINTY_S:
            raise SystemExit(
                f"il piano ha un riposo di {rest:.0f}s contro un freeze a {freeze:.0f}s: "
                f"servono almeno {freeze + FREEZE_UNCERTAINTY_S:.0f}s perche' il "
                f"congelamento sia distinguibile")

    def read_plan(self):
        """The plan's bytes, and the digest of exactly what was read.

        The digest is taken from the SAME read that produced the plan, not from a
        second open: it is the only way to certify that this leg replayed the very
        bytes the other leg replayed. The runner passes the digest it recorded once
        for the whole campaign, and a mismatch stops the leg.
        """
        raw = open(self.arguments.plan, "rb").read()
        digest = hashlib.sha256(raw).hexdigest()
        expected = self.arguments.plan_sha256
        if expected and digest != expected:
            raise SystemExit(
                f"il piano letto non e' quello dichiarato dalla campagna:\n"
                f"  atteso   {expected}\n  ottenuto {digest}\n"
                f"Le due gambe leggerebbero file diversi.")
        self.plan_sha256 = digest
        return json.loads(raw)

    # ------------------------------------------------------------------ traffico
    def send(self, user, body):
        """One request. Returns (status, app_error, transport_error)."""
        for attempt in (1, 2):
            try:
                user.connection.request("POST", "/", body=body, headers=user.headers)
                answer = user.connection.getresponse()
                payload = answer.read()
                if answer.status != 200:
                    return answer.status, None, None
                if b"<error>" in payload:
                    return answer.status, "application error", None
                return answer.status, None, None
            except Exception as failure:                         # noqa: BLE001
                try:
                    user.connection.close()
                except Exception:                                # noqa: BLE001, S110
                    pass
                user.connection = http.client.HTTPConnection(user.netloc, timeout=30)
                if attempt == 2:
                    return None, None, repr(failure)[:120]
        return None, None, "unreachable"

    def record_call(self, row):
        with self.lock:
            self.calls_done += 1
            self.latencies.append(row["latency_ms"])
            if row["transport_error"] or row["app_error"] or row["status"] != 200:
                self.calls_failed += 1
        if self.calls_writer is not None:
            self.calls_writer.writerow(row)
            self.calls_handle.flush()

    @property
    def active_users(self):
        return sorted(label for label, resident in self.residents.items() if resident.active)

    # ------------------------------------------------------------------ registro
    def record_phase(self, phase, note=""):
        """One line per phase boundary: the five populations and the memory."""
        self.phase = phase
        now = time.time()
        population = self.eyes.population() if self.eyes is not None else {}
        deposit = self.eyes.frozen_deposit() if self.eyes is not None else {}
        processes, cgroup, memory_stat, cpu_usec = self.probe.read()
        sample = RoleSample(processes, cgroup, memory_stat, cpu_usec, self.roles)
        with self.lock:
            done, failed = self.calls_done, self.calls_failed
        row = {
            "ts": time.strftime("%H:%M:%S"), "epoch": round(now, 3),
            "phase": phase, "note": note,
            "elapsed_s": round(now - (self.started_at or now), 1),
            "authenticated": population.get("authenticated", len(self.residents)),
            "placed": population.get("placed", ""),
            "frozen": population.get("frozen", ""),
            "unplaced": population.get("unplaced", ""),
            "guest": population.get("guest", ""),
            "connections": population.get("connections", ""),
            "pages": population.get("pages", ""),
            "worker_count": population.get("worker_count",
                                           len(sample.worker_roles) or ""),
            "workers": "|".join(population.get("workers", sample.worker_roles)),
            "worker_pids": json.dumps(self.eyes.worker_pid_map) if self.eyes else "",
            "per_worker": json.dumps(population.get("per_worker", {})),
            "memory_occupied_percent": population.get("memory_occupied_percent", ""),
            "cg_current": cgroup.get("current", ""), "cg_peak": cgroup.get("peak", ""),
            "pss_total_kb": sum(sample.pss_by_role().values()),
            "pss_by_role": json.dumps(sample.pss_by_role()),
            "frozen_bytes": deposit.get("bytes", ""),
            "frozen_folders": deposit.get("user_folders", ""),
            "frozen_pickles": deposit.get("pickles", ""),
            "active_users": len(self.active_users),
            "calls_done": done, "calls_failed": failed,
        }
        self.phase_log.append(row)
        self.phase_writer.writerow(row)
        self.phase_handle.flush()
        print(f"  [{phase}] autenticati {row['authenticated']} collocati {row['placed']} "
              f"congelati {row['frozen']} non collocati {row['unplaced']} | "
              f"worker {row['worker_count']} | attivi {row['active_users']} | "
              f"cg {row['cg_current']} | freezer {row['frozen_bytes']} byte", flush=True)
        return row

    # ------------------------------------------------------------------ le fasi
    def populate(self):
        """The users enter in batches, and the entry never runs blind."""
        capture = load_capture(SESSION_CAPTURE)
        login_calls, pages = build_plan(capture)
        entries = self.plan["entries"]
        batch = self.protocol["batch"]
        gap = self.protocol["arrival_gap_s"]
        settle = self.protocol["batch_settle_s"]
        timeout = self.protocol["populate_timeout_s"]
        print(f"--- populate: {len(entries)} utenti a gruppi di {batch}, "
              f"uno ogni {gap:.1f}s ---", flush=True)
        started = time.time()
        for start in range(0, len(entries), batch):
            self.stop_flag.raise_if_stopped("populate")
            for entry in entries[start:start + batch]:
                self.stop_flag.raise_if_stopped("populate")
                if time.time() - started > timeout:
                    raise InvalidRun(f"populate oltre {timeout:.0f}s con "
                                     f"{len(self.residents)} utenti")
                logged = LoggedUser(self.arguments.base, login_calls, pages,
                                    entry["username"], self.arguments.password,
                                    self.lookups, 0.0)
                resident = Resident(self, entry["label"], logged, entry["think_times"])
                self.residents[entry["label"]] = resident
                if not self.stop_flag.wait(gap, "populate"):
                    break
            done = len(self.residents)
            self.record_phase("populate", note=f"{done}/{len(entries)} entrati")
            if done < len(entries) and not self.stop_flag.wait(settle, "populate"):
                break
        if len(self.residents) != len(entries):
            raise InvalidRun(f"popolamento incompleto: {len(self.residents)} "
                             f"su {len(entries)}")

    def rest(self, seconds, phase, note=""):
        """Silence. Nobody sends anything: this is where the freeze happens."""
        print(f"--- {phase}: {seconds:.0f}s di silenzio ---", flush=True)
        self.record_phase(phase, note=note or "inizio")
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not self.stop_flag.wait(min(30.0, max(0.0, deadline - time.time())), phase):
                break
            self.record_phase(phase, note="in corso")
        self.record_phase(phase, note="fine")

    def wake(self):
        """The working set comes back, one user at a time.

        One at a time and spread out on purpose: a burst of returns would measure
        the freezer's queue instead of one thaw.
        """
        working_set = self.plan["working_set"]
        spread = self.protocol["wake_spread_s"]
        step = spread / max(len(working_set), 1)
        print(f"--- wake: {len(working_set)} utenti, uno ogni {step:.1f}s ---", flush=True)
        self.record_phase("wake", note="inizio")
        clocks_before = self.eyes.user_clocks() if self.eyes is not None else {}
        for label in working_set:
            self.stop_flag.raise_if_stopped("wake")
            resident = self.residents[label]
            was = clocks_before.get(resident.user.username, {})
            resident.activate(measure_reentry=True)
            if not self.stop_flag.wait(step, "wake"):
                break
            self.thaw.append({
                "user": label, "username": resident.user.username,
                "state_before": was.get("state"),
                "first_call_ms": resident.first_call_ms,
                "reentry_ms": resident.reentry_ms,
            })
        self.record_phase("wake", note="fine")

    def work(self):
        """The working set works, with the pauses the plan drew."""
        seconds = self.protocol["work_seconds"]
        print(f"--- work: {seconds:.0f}s con {len(self.active_users)} utenti attivi ---",
              flush=True)
        self.record_phase("work", note="inizio")
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not self.stop_flag.wait(min(30.0, max(0.0, deadline - time.time())), "work"):
                break
            self.record_phase("work", note="in corso")
        self.record_phase("work", note="fine")

    def rotate(self):
        """The swaps the plan wrote: as many enter as leave, at fixed instants."""
        swaps = self.plan["rotation"]
        print(f"--- rotate: {len(swaps)} scambi ---", flush=True)
        self.record_phase("rotate", note="inizio")
        started = time.time()
        for swap in swaps:
            self.stop_flag.raise_if_stopped("rotate")
            wait = swap["at_s"] - (time.time() - started)
            if wait > 0 and not self.stop_flag.wait(wait, "rotate"):
                break
            record = {"at_s": swap["at_s"], "out": [], "in": []}
            for label in swap["out"]:
                resident = self.residents[label]
                record["out"].append({"user": label,
                                      "stopped": resident.deactivate()})
            for label in swap["in"]:
                resident = self.residents[label]
                resident.activate(measure_reentry=True)
                record["in"].append({"user": label})
            self.rotation_log.append(record)
            self.record_phase("rotate", note=f"scambio a {swap['at_s']:.0f}s")
        for label in {entry["user"] for swap in self.rotation_log for entry in swap["in"]}:
            resident = self.residents[label]
            self.thaw.append({"user": label, "username": resident.user.username,
                              "state_before": None,
                              "first_call_ms": resident.first_call_ms,
                              "reentry_ms": resident.reentry_ms,
                              "during": "rotate"})
        self.record_phase("rotate", note="fine")

    def log_out_all(self):
        """Everybody leaves. Errors are recorded, never swallowed."""
        print(f"--- logout: {len(self.residents)} utenti ---", flush=True)
        self.record_phase("logout", note="inizio")
        for label, resident in self.residents.items():
            resident.deactivate(timeout=10.0)
            record = {"user": label, "ts": time.strftime("%H:%M:%S")}
            try:
                resident.user.log_out()
                record["outcome"] = "ok"
            except Exception as failure:                         # noqa: BLE001
                record["outcome"] = "error"
                record["error"] = repr(failure)[:200]
            self.logouts.append(record)
        failed = [record for record in self.logouts if record["outcome"] != "ok"]
        print(f"  logout: {len(self.logouts) - len(failed)} riusciti, "
              f"{len(failed)} falliti", flush=True)
        self.record_phase("logout", note="fine")

    # ------------------------------------------------------------------ freeze
    def certify_freeze(self):
        """The live setpoint, read from the server and not from the plan.

        ``null`` means the freeze is OFF: the core keeps ``math.inf`` and JSON
        cannot carry it. A run that expects a freeze and reads null has no freeze.
        """
        if self.eyes is None:
            return {"stack": "legacy", "freeze": "assente per costruzione"}
        status = self.eyes.live_settings()
        live = status["effective_settings"].get("user_idle_freeze_minutes")
        expected = self.arguments.expect_freeze_minutes
        record = {"live_minutes": live, "expected_minutes": expected,
                  "generation": status.get("generation"),
                  "active_profile": status.get("active_profile")}
        if expected is None:
            record["verdict"] = "nessun valore atteso dichiarato"
            return record
        if live is None:
            raise InvalidRun("il freeze e' spento sul server (null) ma la corsa "
                             f"ne attende {expected}: nessun utente verrebbe congelato")
        if abs(float(live) - float(expected)) > 1e-6:
            raise InvalidRun(f"il freeze vivo e' {live} minuti, atteso {expected}")
        record["verdict"] = "certificato"
        print(f"  freeze vivo certificato: {live} minuti "
              f"(generazione {record['generation']})", flush=True)
        return record

    # ------------------------------------------------------------------ campioni
    def sample_once(self, writer, handle):
        started = time.time()
        with self.lock:
            latency = sorted(self.latencies)
            self.latencies = []
            done, failed = self.calls_done, self.calls_failed
        if self.eyes is not None:
            self.roles.worker_pids = self.eyes.worker_pids
        processes, cgroup, memory_stat, cpu_usec = self.probe.read()
        elapsed = started - (self.previous_stamp or started)
        sample = RoleSample(processes, cgroup, memory_stat, cpu_usec, self.roles,
                            previous_ticks=self.previous_ticks,
                            previous_cpu_usec=self.previous_cpu_usec,
                            elapsed=elapsed if elapsed > 0 else None)
        population = self.eyes.population() if self.eyes is not None else {}
        columns = sample.columns()
        row = {
            "ts": time.strftime("%H:%M:%S"), "epoch": round(started, 3),
            "run": self.arguments.run, "stack": self.arguments.stack,
            "phase": self.phase, "active_users": len(self.active_users),
            "calls_done": done, "calls_failed": failed,
            "p50_ms": self.percentile(latency, 50), "p95_ms": self.percentile(latency, 95),
            "authenticated": population.get("authenticated", len(self.residents)),
            "placed": population.get("placed", ""), "frozen": population.get("frozen", ""),
            "unplaced": population.get("unplaced", ""), "guest": population.get("guest", ""),
            "connections": population.get("connections", ""),
            "pages": population.get("pages", ""),
            "per_worker": json.dumps(population.get("per_worker", {})),
        }
        row.update({key: value for key, value in columns.items() if key in SAMPLE_COLUMNS})
        self.previous_ticks = sample.ticks
        self.previous_cpu_usec = cpu_usec
        self.previous_stamp = started
        writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())
        self.sampler_rows += 1
        self.sampler_ready.set()
        return started

    def sample_loop(self, writer, handle, stop):
        try:
            while not stop.is_set():
                started = self.sample_once(writer, handle)
                stop.wait(max(0.0, SAMPLE_S - (time.time() - started)))
        except BaseException as failure:                          # noqa: BLE001
            self.sampler_error = repr(failure)
            self.sampler_failed.set()
            print(f"!!! CAMPIONATORE MORTO: {self.sampler_error}", flush=True)

    def percentile(self, values, which):
        if not values:
            return ""
        index = max(0, min(len(values) - 1, int(len(values) * which / 100.0) - 1))
        return round(values[index], 3)

    # ------------------------------------------------------------------ la corsa
    def run(self):
        self.started_at = time.time()
        self.phase_handle = open(f"{self.arguments.out}_phases.csv", "w", newline="")
        self.phase_writer = csv.DictWriter(self.phase_handle, fieldnames=PHASE_COLUMNS,
                                           extrasaction="ignore")
        self.phase_writer.writeheader()
        self.calls_handle = open(f"{self.arguments.out}_calls.csv", "w", newline="")
        self.calls_writer = csv.DictWriter(self.calls_handle, fieldnames=CALL_COLUMNS,
                                          extrasaction="ignore")
        self.calls_writer.writeheader()
        samples_handle = open(f"{self.arguments.out}_samples.csv", "w", newline="")
        samples_writer = csv.DictWriter(samples_handle, fieldnames=SAMPLE_COLUMNS,
                                        extrasaction="ignore")
        samples_writer.writeheader()
        cgroup = ContainerCgroup(self.arguments.container)
        guard = MemoryGuard(cgroup, self.stop_flag, f"{self.arguments.out}_memory_guard.json",
                            threshold_percent=self.arguments.memory_threshold)
        guard.read_baseline()
        stop_sampler = threading.Event()
        sampler = threading.Thread(target=self.sample_loop,
                                   args=(samples_writer, samples_handle, stop_sampler),
                                   daemon=True, name="sampler")
        sampler.start()
        guard.start()
        outcome = {"stack": self.arguments.stack, "run": self.arguments.run,
                   "plan": os.path.basename(self.arguments.plan),
                   "plan_sha256": self.plan_sha256}
        try:
            if not self.sampler_ready.wait(timeout=30.0):
                raise SamplerDown(f"nessuna riga campionata: {self.sampler_error}")
            outcome["freeze"] = self.certify_freeze()
            outcome["role_certification"] = self.probe.certify(
                self.probe.read()[0], self.arguments.expect_workers) \
                if self.arguments.expect_workers else "non dichiarata"
            self.record_phase("start", note="baseline")
            self.populate()
            self.rest(self.protocol["rest_seconds"], "rest",
                      note=f"freeze atteso a {self.protocol['freeze_minutes'] * 60:.0f}s")
            self.wake()
            self.work()
            self.rotate()
            self.rest(self.protocol["rest2_seconds"], "rest2")
            outcome["result"] = "completa"
        except StopRequested as stop_asked:
            outcome["result"] = "interrotta"
            outcome["stop"] = str(stop_asked)
            print(f"!!! CORSA INTERROTTA: {stop_asked}", flush=True)
        except InvalidRun as failure:
            outcome["result"] = "non valida"
            outcome["invalid"] = str(failure)
            print(f"!!! CORSA NON VALIDA: {failure}", flush=True)
        except SamplerDown as failure:
            outcome["result"] = "senza misura"
            outcome["sampler"] = str(failure)
            print(f"!!! CAMPIONATORE: {failure}", flush=True)
        finally:
            self.close(guard, sampler, stop_sampler, samples_handle, outcome)
        return outcome

    def close(self, guard, sampler, stop_sampler, samples_handle, outcome):
        """Logout, observation, writers, guard. Always, and in this order."""
        try:
            self.log_out_all()
        except Exception as failure:                              # noqa: BLE001
            outcome["logout_error"] = repr(failure)[:200]
        try:
            self.rest(self.protocol["observe_seconds"], "observe",
                      note="memoria restituita")
        except StopRequested:
            self.record_phase("observe", note="interrotta")
        alive = [label for label, resident in self.residents.items() if resident.active]
        if alive:
            outcome["threads_alive"] = alive[:20]
        stop_sampler.set()
        sampler.join(timeout=20)
        guard.driver_finished.set()
        guard.join(timeout=20)
        verdict = guard.final_check()
        guard.write(verdict)
        outcome["memory"] = verdict
        outcome["stop_reasons"] = self.stop_flag.reason_list
        outcome["sampler_rows"] = self.sampler_rows
        outcome["thaw_summary"] = self.thaw_summary()
        samples_handle.close()
        self.calls_handle.close()
        self.phase_handle.close()
        for name, payload in (("thaw", self.thaw), ("rotation", self.rotation_log),
                              ("logouts", self.logouts), ("outcome", outcome)):
            with open(f"{self.arguments.out}_{name}.json", "w") as handle:
                json.dump(payload, handle, indent=2)
        if self.eyes is not None:
            with open(f"{self.arguments.out}_journal_events.json", "w") as handle:
                json.dump(self.eyes.read_journal_events(), handle, indent=2)
        print(f"FINE {self.arguments.run}: esito {outcome['result']} | "
              f"chiamate {self.calls_done} fallite {self.calls_failed} | "
              f"memory stop {verdict['memory_stop']}", flush=True)

    def thaw_summary(self):
        """The two thaw numbers, summarised. Empty when nothing was measured."""
        first = [record["first_call_ms"] for record in self.thaw
                 if record.get("first_call_ms") is not None]
        whole = [record["reentry_ms"] for record in self.thaw
                 if record.get("reentry_ms") is not None]
        if not first:
            return {"measured": 0}
        return {
            "measured": len(first),
            "first_call_ms": {"p50": round(statistics.median(first), 1),
                              "min": round(min(first), 1), "max": round(max(first), 1)},
            "reentry_ms": ({"p50": round(statistics.median(whole), 1),
                            "min": round(min(whole), 1), "max": round(max(whole), 1)}
                           if whole else {}),
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", required=True, choices=("bridge", "legacy"))
    parser.add_argument("--run", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--password", default="a")
    parser.add_argument("--memory-threshold", type=float, default=80.0)
    parser.add_argument("--plan-sha256", default=None,
                        help="il digest che la campagna ha registrato una volta: "
                             "questa gamba si ferma se legge altri byte")
    parser.add_argument("--expect-workers", type=int, default=0,
                        help="0 = non dichiarata: la topologia qui non e' la variabile")
    parser.add_argument("--expect-freeze-minutes", type=float, default=None,
                        help="il valore che il server deve avere vivo; solo bridge")
    parser.add_argument("--census", default=None)
    parser.add_argument("--journal", default=None)
    parser.add_argument("--frozen-users-path", default=None)
    arguments = parser.parse_args(argv)
    probe = PopulationProbe(arguments)
    probe.stop_flag.install_signal_handlers()
    outcome = probe.run()
    if outcome["memory"]["safety_fail"]:
        return 7
    if outcome["result"] == "completa":
        return 0
    if outcome["result"] == "interrotta":
        return 5
    if outcome["result"] == "non valida":
        return 3
    return 2


if __name__ == "__main__":
    sys.exit(main())
