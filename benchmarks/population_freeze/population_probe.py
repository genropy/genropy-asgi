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
import urllib.error
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

# La guardia di latenza a bucket vive nello scenario del ciclo a otto core, dove
# e' stata scritta e provata con i suoi sei casi. Si importa invece di copiarla:
# duplicare duecento righe di giudizio sarebbe il modo piu' sicuro di far divergere
# due verdetti che devono restare lo stesso verdetto. Lo scenario di provenienza
# non viene toccato.
sys.path.insert(0, os.path.join(BENCHMARKS_DIR, "eight_core_cycle"))
from admission_guard import AdmissionGuard                                        # noqa: E402

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

CALL_COLUMNS = ["phase", "user", "started_at", "completed_at", "latency_ms", "lateness_s",
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

    An ACTIVE resident calls once a second, and keeps its own clock: it remembers
    the instant each call was DUE and records how late it actually started. That
    lateness is the only thing that can tell a slow stack from a driver that has
    run out of threads — see ``StepReading.generator_verdict``. A resident that
    cannot keep its second does not speed up to catch up: it moves its own due
    time forward, so the lateness is a debt that shows, not one that hides.
    """

    def __init__(self, probe, label, logged_user):
        self.probe = probe
        self.label = label
        self.user = logged_user
        self.thread = None
        self.leave = threading.Event()
        self.bursts = 0
        self.first_call_ms = None
        self.reentry_ms = None
        self.failed = 0
        self.due = None

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
        """One call a second, until the resident is told to leave.

        The period is the protocol's, not a draw. The due time advances by exactly
        one period per call, so a resident that arrives late stays late instead of
        firing a burst to catch up — a catch-up burst would offer more than one
        request per second and the measured rate would stop meaning anything.
        """
        period = self.probe.request_period
        self.due = time.time()
        first = True
        while not (self.leave.is_set() or self.probe.stop_flag.stopped):
            now = time.time()
            if self.due > now and self.leave.wait(self.due - now):
                return
            started = time.time()
            self.burst(measure_reentry and first, lateness=started - self.due)
            if measure_reentry and first:
                self.reentry_ms = round((time.time() - started) * 1000, 3)
                first = False
            self.due += period

    def burst(self, measure_first, lateness=0.0):
        """One recorded call: the indexed getSelection, as everywhere else."""
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
            "latency_ms": latency, "lateness_s": round(lateness, 6),
            "status": status if status is not None else "",
            "app_error": app_error or "", "transport_error": transport_error or "",
            "kind": "first_after_thaw" if measure_first else "call",
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
        self.lock = threading.Lock()
        self.request_period = self.protocol.get("request_period_s", 1.0)
        self.calls_seen = []
        self.feed_guard = False
        self.steps = []
        self.stop_reason = None
        self.structural_failure = None
        self.memory_verdict = None
        self.last_stable = None
        self.first_unsustainable = None
        self.login_attempts = []
        self.cold_start_errors = 0
        self.initial_working_set = set(self.plan["working_set"])
        self.guard = AdmissionGuard(
            {"p95_limit_ms": self.protocol["admission"]["p95_limit_ms"],
             "consecutive_buckets": self.protocol["admission"]["consecutive_buckets"],
             "minimum_samples": 2},
            f"{arguments.out}_admission.json", self.admission_context)
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

    def admission_context(self):
        """What the ADMISSION_STOP event records about the run, at the instant."""
        context = {"phase": self.phase, "completed": self.calls_done,
                   "population_active": len(self.active_users),
                   "population_authenticated": len(self.residents),
                   "pending": 0,
                   "last_stable_step": (self.last_stable or {}).get("phase")}
        if self.eyes is not None:
            population = self.eyes.population()
            context["census_authenticated"] = population.get("authenticated")
            context["census_placed"] = population.get("placed")
            context["census_frozen"] = population.get("frozen")
            context["census_workers"] = population.get("worker_count")
        return context

    def record_call(self, row):
        with self.lock:
            self.calls_done += 1
            self.latencies.append(row["latency_ms"])
            if row["transport_error"] or row["app_error"] or row["status"] != 200:
                self.calls_failed += 1
            self.calls_seen.append(row)
        # La guardia vede SOLO le finestre misurate: il thaw e l'assestamento non
        # sono capacita', e giudicarli chiamerebbe limite un transitorio.
        if self.feed_guard:
            self.guard.record_latency(row["completed_at"], row["latency_ms"])
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
                logged = self.log_in_with_retries(entry, login_calls, pages)
                resident = Resident(self, entry["label"], logged)
                self.residents[entry["label"]] = resident
                # Gli utenti del working set iniziale lavorano APPENA sono entrati:
                # e' cio' che rende il freeze misurabile sotto carico, ed e' il caso
                # reale — un sito non e' mai silenzioso mentre la popolazione entra.
                # Non si misura un reentry: questi non sono mai stati congelati.
                if entry["label"] in self.initial_working_set:
                    resident.activate(measure_reentry=False)
                if not self.stop_flag.wait(gap, "populate"):
                    break
            done = len(self.residents)
            self.record_phase("populate", note=f"{done}/{len(entries)} entrati")
            if done < len(entries) and not self.stop_flag.wait(settle, "populate"):
                break
        if len(self.residents) != len(entries):
            raise InvalidRun(f"popolamento incompleto: {len(self.residents)} "
                             f"su {len(entries)}")

    def log_in_with_retries(self, entry, login_calls, pages):
        """The login policy validated on the eight-core cycle: five tries, five apart.

        A service process asked to build the site while answering a login answers
        500, and the cold window of the legacy stack was measured at about fourteen
        seconds. Five attempts five seconds apart cover twenty. Every attempt is
        recorded; the ones that precede a success are ``cold_start`` and are never
        folded into the errors of a measured window.
        """
        attempts_max = int(self.protocol["login_attempts_max"])
        wait = float(self.protocol["login_retry_seconds"])
        attempts, logged = [], None
        for attempt in range(1, attempts_max + 1):
            record = {"ts": time.strftime("%H:%M:%S"), "user": entry["label"],
                      "username": entry["username"], "attempt": attempt,
                      "status": "", "exception": ""}
            try:
                logged = LoggedUser(self.arguments.base, login_calls, pages,
                                    entry["username"], self.arguments.password,
                                    self.lookups, 0.0)
                record.update(status=200, outcome="ok")
                attempts.append(record)
                break
            except urllib.error.HTTPError as failure:
                record.update(status=failure.code, exception=repr(failure)[:200],
                              outcome="failed")
            except Exception as failure:                          # noqa: BLE001
                record.update(exception=repr(failure)[:200], outcome="failed")
            attempts.append(record)
            if attempt < attempts_max and not self.stop_flag.wait(wait, "attesa login"):
                break
        if logged is None:
            self.login_attempts.extend(attempts)
            raise InvalidRun(f"{entry['label']} ({entry['username']}) non e' entrato "
                             f"dopo {len(attempts)} tentativi: "
                             f"{[a.get('status') or a.get('exception') for a in attempts]}")
        for record in attempts:
            if record["outcome"] == "failed":
                record["outcome"] = "cold_start"
                self.cold_start_errors += 1
        self.login_attempts.extend(attempts)
        return logged

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

    def measure_window(self, phase, seconds, active_target):
        """One measured window: the calls inside it are the step's reading.

        The latency guard is fed ONLY from here. During a thaw the returning users
        pay the cost of coming back, and during the settle the stack is still
        absorbing them: judging capacity on those seconds would call a transient a
        limit. The guard's minimum samples per bucket is set from the load level,
        because a bucket at eighty active users and one at five hundred cannot
        share a threshold.
        """
        self.guard.minimum_samples = max(
            2, int(active_target * self.protocol["admission"]["minimum_samples_ratio"]))
        self.guard.next_bucket = None
        self.feed_guard = True
        opened = time.time()
        first_call = len(self.calls_seen)
        self.record_phase(phase, note=f"inizio, {active_target} attivi attesi")
        deadline = opened + seconds
        while time.time() < deadline:
            if not self.stop_flag.wait(min(5.0, max(0.0, deadline - time.time())), phase):
                break
            self.guard.judge_closed_buckets(time.time())
            self.record_phase(phase, note="in corso")
        self.guard.judge_closed_buckets(time.time() + 1.0)
        self.feed_guard = False
        closed = time.time()
        self.record_phase(phase, note="fine")
        return self.read_step(phase, active_target, opened, closed, first_call)

    def read_step(self, phase, active_target, opened, closed, first_call):
        """The numbers of one measured window, and the two verdicts on them."""
        with self.lock:
            rows = self.calls_seen[first_call:]
        latencies = sorted(r["latency_ms"] for r in rows)
        late = sorted(r["lateness_s"] for r in rows)
        wall = max(closed - opened, 1e-9)
        half = len(late) // 2
        drift = ((statistics.median(late[half:]) - statistics.median(late[:half]))
                 if half else 0.0)
        planned = int(round(active_target * wall / self.request_period))
        errors = sum(1 for r in rows if r["transport_error"] or r["app_error"]
                     or r["status"] != 200)
        reading = {
            "phase": phase, "active_target": active_target,
            "active_observed": len(self.active_users),
            "seconds": round(wall, 2),
            "planned": planned, "started": len(rows), "completed": len(rows),
            "started_ratio": round(len(rows) / planned, 4) if planned else None,
            "per_second": round(len(rows) / wall, 2),
            "p50_ms": self.percentile(latencies, 50),
            "p95_ms": self.percentile(latencies, 95),
            "p99_ms": self.percentile(latencies, 99),
            "late_p50_s": self.percentile(late, 50),
            "late_max_s": round(late[-1], 4) if late else None,
            "late_drift_s": round(drift, 4),
            "errors": errors,
            "guard_buckets": self.guard.judged,
            "guard_over_limit": self.guard.bad,
            "guard_thin": self.guard.thin,
            "guard_longest_run": self.guard.peak_consecutive,
            "admission_stop": not self.guard.admission_open,
        }
        reading["generator_verdict"] = self.generator_verdict(reading)
        self.steps.append(reading)
        print(f"  [{phase}] attivi {reading['active_observed']} | "
              f"{reading['completed']}/{planned} completate "
              f"({(reading['started_ratio'] or 0) * 100:.1f}%) {reading['per_second']:.1f}/s | "
              f"p50 {reading['p50_ms']} p95 {reading['p95_ms']} p99 {reading['p99_ms']} | "
              f"lateness p50 {reading['late_p50_s']} deriva {reading['late_drift_s']:+.3f} | "
              f"errori {errors}", flush=True)
        return reading

    def generator_verdict(self, reading):
        """Is the limit the stack's or the driver's own? Never guessed.

        The driver is the suspect when it fails to START the work it planned, or
        when its own start lateness grows WHILE the server keeps answering fast.
        A slow server produces slow answers; a driver out of threads produces late
        starts with fast answers. The two look nothing alike in these numbers.
        """
        rules = self.protocol["generator"]
        started_short = ((reading["started_ratio"] or 1.0) < rules["started_ratio_min"])
        drifting = reading["late_drift_s"] > rules["lateness_drift_limit_s"]
        server_fast = ((reading["p95_ms"] or 0) < rules["server_fast_p95_ms"])
        if started_short:
            return {"limit": True, "reason": (
                f"avviato {(reading['started_ratio'] or 0) * 100:.1f}% delle richieste "
                f"pianificate, sotto il {rules['started_ratio_min'] * 100:.0f}%")}
        if drifting and server_fast:
            return {"limit": True, "reason": (
                f"la lateness del generatore deriva di {reading['late_drift_s']:+.1f}s "
                f"mentre il server risponde in p95 {reading['p95_ms']} ms, sotto "
                f"{rules['server_fast_p95_ms']:.0f}: e' il driver, non lo stack")}
        return {"limit": False, "reason": None}

    def baseline(self):
        """The initial working set, measured on its own.

        Nobody is woken here: these eighty have been working since they entered,
        all through the populate and all through the rest. They are the reference
        the ramp climbs from, and they were never frozen — so there is no thaw cost
        in this reading, which is exactly what makes it a baseline.
        """
        active = len(self.active_users)
        print(f"--- baseline: {active} utenti attivi, "
              f"{self.protocol['baseline_seconds']:.0f}s misurati ---", flush=True)
        return self.measure_window(f"baseline_{active}",
                                   self.protocol["baseline_seconds"], active)

    def wake_group(self, labels, gap, phase):
        """A group comes back, one user a second. The first call is timed apart."""
        clocks = self.eyes.user_clocks() if self.eyes is not None else {}
        self.record_phase(phase, note=f"risveglio di {len(labels)}")
        for label in labels:
            self.stop_flag.raise_if_stopped(phase)
            resident = self.residents[label]
            was = clocks.get(resident.user.username, {})
            resident.activate(measure_reentry=True)
            self.thaw.append({
                "user": label, "username": resident.user.username,
                "state_before": was.get("state"), "phase": phase,
                "at": round(time.time(), 3),
            })
            if not self.stop_flag.wait(gap, phase):
                break

    def collect_thaw_latencies(self):
        """Fill in the first-call cost of every user woken so far."""
        for record in self.thaw:
            if record.get("first_call_ms") is None:
                resident = self.residents[record["user"]]
                record["first_call_ms"] = resident.first_call_ms
                record["reentry_ms"] = resident.reentry_ms

    def run_step(self, step):
        """One rung of the ramp: ten users back, twenty quiet seconds, sixty measured."""
        target = step["target_active"]
        print(f"--- gradino {step['step']}: verso {target} utenti attivi ---", flush=True)
        self.wake_group(step["wake"], step["wake_gap_s"], f"thaw_{target}")
        self.collect_thaw_latencies()
        self.record_phase(f"settle_{target}", note="assestamento")
        self.stop_flag.wait(step["settle_seconds"], f"settle_{target}")
        reading = self.measure_window(f"measure_{target}", step["measure_seconds"], target)
        reading["step"] = step["step"]
        reading["woken"] = step["wake"]
        return reading

    def ramp(self):
        """Climb until a guard says stop, or until the plan's ceiling is reached."""
        for step in self.plan["steps"]:
            self.stop_flag.raise_if_stopped("rampa")
            reading = self.run_step(step)
            if reading["admission_stop"]:
                self.stop_reason = "CAPACITY_LIMIT"
                self.first_unsustainable = reading
                print(f"!!! CAPACITY_LIMIT al gradino {step['step']}, "
                      f"{reading['active_observed']} utenti attivi", flush=True)
                self.hold_after_stop()
                return
            if reading["generator_verdict"]["limit"]:
                self.stop_reason = "GENERATOR_LIMIT"
                self.first_unsustainable = reading
                print(f"!!! GENERATOR_LIMIT al gradino {step['step']}: "
                      f"{reading['generator_verdict']['reason']}", flush=True)
                self.hold_after_stop()
                return
            self.last_stable = reading
        self.stop_reason = "MAX_500_REACHED"
        print(f"--- tetto del piano raggiunto: {len(self.active_users)} utenti attivi ---",
              flush=True)

    def hold_after_stop(self):
        """The population is held, not grown, and not rescued with more resources."""
        seconds = self.protocol["hold_after_stop_seconds"]
        print(f"--- tenuta di {seconds:.0f}s alla popolazione raggiunta ---", flush=True)
        self.record_phase("hold_after_stop", note="inizio")
        self.stop_flag.wait(seconds, "hold_after_stop")
        self.record_phase("hold_after_stop", note="fine")

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
    def certify_population_after_rest(self):
        """The counts the freeze must have produced, or the run is not a measure.

        On the BRIDGE: the eighty that never stopped are active and placed, and
        everybody else is frozen. The two sets must not overlap and must not leave
        anyone out — a user counted twice would hide a user missing. The frozen
        store must be readable, because a freeze nobody can read is not a freeze.

        On the LEGACY there is no freezer: two thousand sessions stay resident,
        eighty of them working, and the count of frozen users must be zero.
        """
        expected_active = len(self.initial_working_set)
        active = len(self.active_users)
        record = {"stage": "popolazione dopo il riposo", "stack": self.arguments.stack,
                  "active_threads": active, "expected_active": expected_active,
                  "residents": len(self.residents)}
        problems = []
        if active != expected_active:
            problems.append(f"{active} utenti attivi invece di {expected_active}")
        if len(self.residents) != self.plan["users"]:
            problems.append(f"{len(self.residents)} residenti invece di {self.plan['users']}")
        if self.eyes is None:
            record["verdict"] = "legacy: nessun freezer per costruzione"
            record["frozen"] = 0
        else:
            population = self.eyes.population()
            record.update(population)
            deposit = self.eyes.frozen_deposit()
            record["frozen_store"] = deposit
            expected_frozen = self.plan["users"] - expected_active
            record["expected_frozen"] = expected_frozen
            counted = (population.get("placed", 0) + population.get("frozen", 0)
                       + population.get("unplaced", 0))
            if population.get("authenticated") != self.plan["users"]:
                problems.append(f"census: {population.get('authenticated')} autenticati "
                                f"invece di {self.plan['users']}")
            if counted != population.get("authenticated"):
                problems.append(f"conteggi incoerenti: collocati+congelati+non collocati "
                                f"= {counted}, autenticati {population.get('authenticated')}")
            if population.get("guest"):
                problems.append(f"{population['guest']} guest presenti")
            # La banda di incertezza del freeze e' dichiarata nel piano: si accetta
            # uno scostamento, non un ordine di grandezza.
            tolleranza = max(10, int(expected_frozen * 0.05))
            if abs(population.get("frozen", 0) - expected_frozen) > tolleranza:
                problems.append(f"{population.get('frozen')} congelati invece di circa "
                                f"{expected_frozen} (tolleranza {tolleranza})")
            if population.get("placed", 0) < expected_active:
                problems.append(f"{population.get('placed')} collocati, ne servono almeno "
                                f"{expected_active} per gli attivi")
            if not deposit.get("available"):
                problems.append(f"deposito congelato non leggibile: {deposit.get('reason')}")
            record["verdict"] = "certificato" if not problems else "non conforme"
        record["problemi"] = problems
        self.phase_log.append({"ts": time.strftime("%H:%M:%S"), "phase": "certify_counts",
                               "note": record["verdict"]})
        print(f"  conteggi dopo il riposo [{self.arguments.stack}]: attivi {active}, "
              f"residenti {len(self.residents)}, congelati "
              f"{record.get('frozen', 0)} (attesi ~{record.get('expected_frozen', 0)}), "
              f"worker {record.get('worker_count', '-')}", flush=True)
        if problems:
            raise InvalidRun("conteggi dopo il riposo: " + "; ".join(problems))
        return record

    @property
    def classification(self):
        """One word for how the run ended. The order below is the priority.

        A memory stop outranks everything: it is a fact about the machine, not a
        capacity reading, and a run that ran out of memory has not measured a
        limit. A structural failure outranks the capacity verdicts for the same
        reason — an incomplete population or a broken count is not a result.
        """
        memory = getattr(self, "memory_verdict", None) or {}
        if memory.get("memory_stop") or any(v > 0 for v in
                                           (memory.get("pressure_delta") or {}).values()):
            return "MEMORY_STOP"
        if self.structural_failure:
            return "STRUCTURAL_FAIL"
        return self.stop_reason or "STRUCTURAL_FAIL"

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
            outcome["freeze_after_rest"] = self.certify_freeze()
            outcome["counts_after_rest"] = self.certify_population_after_rest()
            self.baseline()
            self.ramp()
            self.collect_thaw_latencies()
            outcome["result"] = "completa"
        except StopRequested as stop_asked:
            outcome["result"] = "interrotta"
            outcome["stop"] = str(stop_asked)
            print(f"!!! CORSA INTERROTTA: {stop_asked}", flush=True)
        except InvalidRun as failure:
            outcome["result"] = "non valida"
            outcome["invalid"] = str(failure)
            self.structural_failure = str(failure)
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
        self.memory_verdict = verdict
        outcome["memory"] = verdict
        outcome["classification"] = self.classification
        outcome["stop_reason"] = self.stop_reason
        outcome["steps"] = self.steps
        outcome["last_stable"] = self.last_stable
        outcome["first_unsustainable"] = self.first_unsustainable
        outcome["admission"] = self.guard.verdict
        outcome["login_attempts_total"] = len(self.login_attempts)
        outcome["cold_start_errors"] = self.cold_start_errors
        outcome["request_period_s"] = self.request_period
        outcome["stop_reasons"] = self.stop_flag.reason_list
        outcome["sampler_rows"] = self.sampler_rows
        outcome["thaw_summary"] = self.thaw_summary()
        samples_handle.close()
        self.calls_handle.close()
        self.phase_handle.close()
        self.guard.write()
        for name, payload in (("thaw", self.thaw), ("steps", self.steps),
                              ("login_attempts", self.login_attempts),
                              ("phases", self.phase_log),
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
