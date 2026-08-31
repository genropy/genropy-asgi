# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""One leg of the eight-core cycle: the same run, against one stack.

The driver is invoked once per stack, on the same boot of the same machine, and
the load it sends is byte-identical in both invocations: the plan file decides
the accounts, the instants and the lookups, and the applicative part of this file
knows nothing about which stack answers.

WHAT DIFFERS BETWEEN THE TWO LEGS, and nothing else:

- the ROLE MAP of the sampler — commander/template/pool workers on the bridge,
  daemon/master/Gunicorn workers on the legacy;
- the OPTIONAL bridge observations — census, decision journal, topology and the
  live setpoints. All reads. There is no hot apply in this run: the policy the
  measure needs is in the recipe from the first instant, so the two legs share
  one timeline with no wait to equalise.

THE PACE IS THE PLAN'S, PER USER. The engine offers a row when its instant
arrives and its user is admitted; a user that is paused simply has no rows. So
when fifty users stop, the offered rate drops by fifty requests a second, and it
drops because fifty users' worth of work is absent — never because a global rate
was rewritten and redistributed over the others.

TWO GUARDS THAT MUST NEVER BE CONFUSED:

- the MEMORY guard raises the run's stop flag. Everything ends, the logout runs,
  the measure is over. So do an OOM and a lost container identity.
- the ADMISSION guard closes the door and nothing else. Users already active keep
  working to the end; no new login and no return is allowed, for the rest of the
  execution, even if the latency recovers. The run then measures the population it
  reached. It is a fact about capacity, not a failure of the leg.

EVERY PHASE IS MANDATORY. The driver plays all six, and ``require_every_phase``
fails the leg if one is missing or if the population never filled: a run that
reports a subset looks like a result and is not one, because the two stacks would
then be compared on different work. An incomplete population fails at the login
instead, so the phases are never reached in a shape nobody asked for.

``full_warmup`` sits between the ramp and the first measure, at full population
and outside every measured window: the first construction of the site costs a
service process seconds, and that cost belongs nowhere near a measure.

THE LOGIN IS RETRIED, up to the plan's limit, with a fresh connection each time.
A service process asked to build the site while answering a login answers 500;
those 500s are classified ``cold_start``, counted on their own, and never folded
into the errors of a measured window. A user that never gets in fails the run.

    python3 cycle_probe.py --stack bridge --run e8c_bridge \\
        --base http://127.0.0.1:8098 --container genro-bench-lab-bridge-1 \\
        --plan traces/cycle_plan.json --out /work/e8c_bridge \\
        --census http://127.0.0.1:8098/_server/inspector/census \\
        --journal /lab/runtime/e8c_bridge_orders.decisions.jsonl \\
        --expect-workers 8 --expect-per-worker 15
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse

BENCHMARKS_DIR = os.path.abspath(
    os.environ.get("BENCHMARKS_DIR")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
sys.path.insert(0, BENCHMARKS_DIR)
sys.path.insert(0, os.path.join(BENCHMARKS_DIR, "bench_common"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from admission_guard import AdmissionGuard                                        # noqa: E402
from bench_common.bridge_eyes import BridgeEyes                                   # noqa: E402
from bench_common.container_probe import (                                        # noqa: E402
    COLUMNS, BridgeRoles, ContainerProbe, LegacyRoles, RoleSample)
from bench_common.load_engine import LoadEngine, UserRunner                       # noqa: E402
from bench_common.page_class_cache import PageClassCacheCertificate               # noqa: E402
from bench_common.stop_guard import (                                             # noqa: E402
    ContainerCgroup, MemoryGuard, StopFlag, StopRequested)
from churn_driver import LoggedUser, build_plan, load_capture                     # noqa: E402

SESSION_CAPTURE = os.path.join(BENCHMARKS_DIR, "session_capture.jsonl")
USERNAMES_ALL = os.path.join(BENCHMARKS_DIR, "usernames_all.txt")
SAMPLE_S = 2.0

# I setpoint che la recipe dichiara e che il driver ricontrolla vivi prima di
# misurare. user_idle_freeze_minutes DEVE tornare null: e' il freeze spento, e la
# pausa di sessanta secondi deve lasciare gli utenti residenti sul loro worker.
REQUIRED_SETTINGS = {
    "cpu_grow_percent": None,
    "user_idle_freeze_minutes": None,
    "worker_min_life_seconds": 3600.0,
    "occupancy_max_percent": 80.0,
    "reception_reserved_percent": 0.0,
}

# Le colonne dello scenario: quelle condivise, piu' le tre che questo ciclo
# aggiunge. Il formato dei CSV degli scenari precedenti non e' toccato.
CYCLE_COLUMNS = COLUMNS[:COLUMNS.index("process_count")] + [
    "users_active", "users_paused", "admission_stop"] + COLUMNS[COLUMNS.index("process_count"):]


class InvalidRun(RuntimeError):
    """A structural criterion failed: the run's data is not comparable."""


class SamplerDown(RuntimeError):
    """The sampler produced nothing: there is no measure."""


class CycleEngine(LoadEngine):
    """The load engine of this scenario: a per-user pace and an admitted set.

    Three additions to the shared engine, and nothing removed:

    - a row is offered only if its user is ADMITTED. A row whose user never
      logged in, or never came back because the door closed, is counted as
      withheld and reported as such: the plan is fixed, so what the run did not
      send has to be a number.
    - every completed call is handed to the admission guard.
    - the FIRST call of a returning user is timed on its own, because the cost of
      coming back is not visible in an average.
    """

    def __init__(self, *args, admission=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.admission = admission
        self.calling = set()
        self.username_of = {}
        self.awaiting_return = {}
        self.return_calls = []
        self.withheld = {}

    def admit(self, label):
        with self.lock:
            self.calling.add(label)

    def expect_return(self, label, position):
        with self.lock:
            self.awaiting_return[label] = position

    @property
    def active_count(self):
        with self.lock:
            return len(self.calling)

    def record_call(self, row):
        """The shared accounting, then this scenario's two extra readings."""
        super().record_call(row)
        if self.admission is not None:
            self.admission.record_latency(row["completed_at"], row["latency_ms"])
        with self.lock:
            position = self.awaiting_return.pop(row["user"], None)
        if position is not None:
            self.return_calls.append({
                "user": row["user"], "return_position": position,
                "phase": row["phase"], "latency_ms": row["latency_ms"],
                "lateness_s": row["lateness_s"], "status": row["status"],
                "app_error": row["app_error"], "transport_error": row["transport_error"],
                "worker": row["worker"],
            })

    def play_cycle_window(self, phase):
        """Offer this phase's rows at their instants, to admitted users only."""
        rows = [row for row in self.trace if row["phase"] == phase]
        if not rows:
            print(f"--- {phase}: assente nella traccia, saltata ---", flush=True)
            return None
        rate = rows[0]["rate"]
        with self.lock:
            self.state["phase"] = phase
            self.state["level_rate"] = rate
        print(f"--- {phase}: {len(rows)} richieste, picco {rate:.0f}/s ---", flush=True)
        self.stop_flag.raise_if_stopped(f"inizio {phase}")
        started = time.time()
        offered = withheld = 0
        for row in rows:
            if self.stop_flag.stopped:
                print(f"--- {phase}: interrotta a {offered}/{len(rows)} richieste ---",
                      flush=True)
                break
            target = started + row["t_rel"]
            now = time.time()
            if target > now and self.stop_flag.event.wait(target - now):
                break
            with self.lock:
                admitted = row["user"] in self.calling
            if not admitted:
                withheld += 1
                continue
            self.count_offered()
            offered += 1
            self.runners[row["user"]].inbox.put((target, phase))
        self.drain()
        record = self.close_window(phase, rows, offered, started)
        record["withheld"] = withheld
        record["declared_rows"] = len(rows)
        record["active_users_at_end"] = self.active_count
        self.withheld[phase] = withheld
        if withheld:
            print(f"    trattenute {withheld} richieste di utenti non ammessi", flush=True)
        return record


class CycleProbe:
    """One leg: baseline, login ramp, measures, pause, return, logout, observation."""

    def __init__(self, arguments):
        self.arguments = arguments
        self.plan_sha256 = None
        self.plan = self.read_plan()
        self.protocol = self.plan["protocol"]
        self.stop_flag = StopFlag()
        self.eyes = None
        if arguments.stack == "bridge":
            if not (arguments.census and arguments.journal):
                raise SystemExit("--census e --journal sono obbligatori con --stack bridge")
            self.eyes = BridgeEyes(arguments.base, arguments.census, arguments.journal)
        self.roles = BridgeRoles() if arguments.stack == "bridge" else LegacyRoles()
        self.probe = ContainerProbe(arguments.container, self.roles)
        self.admission = AdmissionGuard(self.protocol["admission"],
                                        f"{arguments.out}_admission.json",
                                        self.admission_context)
        self.engine = None
        self.checkpoints = []
        self.population_log = []
        self.logouts = []
        self.login_attempts = []
        self.cold_start_errors = 0
        self.login_failure = None
        self.phases_played = []
        self.reached_full = False
        self.sampler_rows = 0
        self.sampler_error = None
        self.sampler_ready = threading.Event()
        self.sampler_failed = threading.Event()
        self.previous_ticks = {}
        self.previous_cpu_usec = None
        self.previous_stamp = None
        self.accounts = [line.strip() for line in open(USERNAMES_ALL) if line.strip()]
        self.lookups = self.accounts[:self.plan["lookups"]]

    def read_plan(self):
        """The plan's bytes, and the digest of exactly what was read.

        The digest is taken from the SAME read that produced the plan, not from a
        second open: it is the only way to certify that this leg replayed the very
        bytes the other leg replayed.
        """
        raw = open(self.arguments.plan, "rb").read()
        digest = hashlib.sha256(raw).hexdigest()
        expected = self.arguments.plan_sha256
        if expected and digest != expected:
            raise SystemExit(
                f"il piano letto non e' quello dichiarato dalla campagna:\n"
                f"  atteso   {expected}\n  ottenuto {digest}\n"
                f"I due stack leggerebbero file diversi.")
        self.plan_sha256 = digest
        return json.loads(raw)

    @property
    def expected_workers_now(self):
        """How many worker processes the stack should be showing right now.

        The two stacks do not answer this the same way, and using one condition
        for both was wrong:

        - the LEGACY has no pool. Gunicorn forks its workers at boot and keeps
          them whatever the population does, so the expected count is always the
          declared one.
        - the BRIDGE grows its pool from placement demand only. The expected count
          is therefore derived from the users actually PLACED, at
          ``worker_max_users`` each, never fewer than the one worker the group
          starts with, and never more than the configured maximum.
        """
        if self.eyes is None:
            return self.arguments.expect_workers
        placed = (self.eyes.population() or {}).get("placed") or 0
        per_worker = max(1, self.arguments.expect_per_worker)
        needed = -(-placed // per_worker)
        return max(1, min(self.arguments.expect_workers, needed))

    # ------------------------------------------------------------------ contesto
    def admission_context(self):
        """What the ADMISSION_STOP event records about the run, at the instant."""
        with self.engine.lock:
            phase = self.engine.state["phase"]
            completed = self.engine.state["completed"]
            active = len(self.engine.calling)
            authenticated = len(self.engine.runners)
        context = {"phase": phase, "completed": completed,
                   "population_active": active,
                   "population_authenticated": authenticated,
                   "pending": self.engine.pending}
        if self.eyes is not None:
            population = self.eyes.population()
            context["census_authenticated"] = population.get("authenticated")
            context["census_placed"] = population.get("placed")
            context["census_per_worker"] = population.get("per_worker")
        return context

    # ------------------------------------------------------------------ carico
    def body_for(self, user, lookup, counter):
        """The load unit: the same indexed getSelection on both stacks."""
        return urllib.parse.urlencode(user.get_call_form(lookup, counter))

    def build_user(self, index, login_calls, pages):
        """One account logged in, with its runner started and admitted.

        Up to ``login_attempts_max`` attempts, a FRESH connection every time —
        ``LoggedUser`` builds its own opener and its own ``HTTPConnection``, so
        re-constructing it is a new TCP connection by construction. Every attempt
        is recorded with its status, its body and its exception.

        The first construction of the site costs a service process seconds, and a
        process asked to do it while answering a login answers 500. Those 500s are
        classified as ``cold_start`` — kept, counted, reported, and NEVER folded
        into the errors of a measured window, which they precede. A user that does
        not get in after every attempt fails the whole run: this measures the
        operating regime, so an incomplete population is not a result.
        """
        label = f"user_{index + 1}"
        username = self.accounts[index]
        attempts_max = int(self.protocol["login_attempts_max"])
        wait = float(self.protocol["login_retry_seconds"])
        attempts, logged = [], None
        for attempt in range(1, attempts_max + 1):
            record = {"ts": time.strftime("%H:%M:%S"), "user": label, "username": username,
                      "attempt": attempt, "status": "", "response": "", "exception": ""}
            try:
                logged = LoggedUser(self.arguments.base, login_calls, pages,
                                    username, self.arguments.password, self.lookups, 0.0)
                record.update(status=200, outcome="ok")
                attempts.append(record)
                break
            except urllib.error.HTTPError as failure:
                body = ""
                try:
                    body = failure.read().decode("utf-8", "replace")[:300]
                except Exception:                                 # noqa: BLE001, S110
                    pass
                record.update(status=failure.code, response=body,
                              exception=repr(failure)[:200], outcome="failed")
            except Exception as failure:                          # noqa: BLE001
                record.update(exception=repr(failure)[:200], outcome="failed")
            attempts.append(record)
            # ``StopFlag.wait`` torna True quando l'attesa si e' COMPLETATA, e
            # False quando uno stop l'ha interrotta. Si esce dal ciclo solo nel
            # secondo caso: un'attesa andata a buon fine e' il permesso di fare
            # il tentativo successivo, non la ragione per rinunciare.
            if attempt < attempts_max and not self.stop_flag.wait(
                    wait, "attesa fra i tentativi"):
                break
        if logged is None:
            self.login_attempts.extend(attempts)
            raise InvalidRun(
                f"{label} ({username}) non e' entrato dopo {len(attempts)} "
                f"tentativi: {[a.get('status') or a.get('exception') for a in attempts]}")
        # Chi ha fallito PRIMA di un successo e' costo a freddo, non un errore
        # della prova: il nome lo dice, e il conteggio resta separato.
        for record in attempts:
            if record["outcome"] == "failed":
                record["outcome"] = "cold_start"
                self.cold_start_errors += 1
        self.login_attempts.extend(attempts)
        runner = UserRunner(self.engine, label, logged)
        runner.start()
        self.engine.runners[label] = runner
        self.engine.username_of[label] = username
        self.engine.admit(label)
        return label

    def login_pacer(self, done, login_calls, pages):
        """One login per period, for the whole login ramp, unless the door closes.

        Runs beside the offering loop: the ramp's own rows are already in the
        plan, and a user's rows start one second after its login instant. A login
        that takes longer than the period makes the pacer late, and the lateness
        is visible in ``population_log`` — it is never hidden by moving the plan.
        """
        period = self.protocol["login_period_seconds"]
        total = self.plan["users"]
        started = time.time()
        for index in range(total):
            if self.stop_flag.stopped or done.is_set():
                return
            if not self.admission.admission_open:
                print(f"--- login fermati a {index} utenti: ADMISSION_STOP ---", flush=True)
                return
            target = started + index * period
            now = time.time()
            if target > now and self.stop_flag.event.wait(target - now):
                return
            record = {"login": index + 1, "user": f"user_{index + 1}",
                      "username": self.accounts[index], "ts": time.strftime("%H:%M:%S"),
                      "planned_at": round(index * period, 3),
                      "actual_at": round(time.time() - started, 3)}
            before = len(self.login_attempts)
            try:
                self.build_user(index, login_calls, pages)
                record["outcome"] = "ok"
            except InvalidRun as failure:
                record["outcome"] = "error"
                record["error"] = str(failure)[:300]
                self.login_failure = str(failure)
            except Exception as failure:                          # noqa: BLE001
                record["outcome"] = "error"
                record["error"] = repr(failure)[:200]
                self.login_failure = repr(failure)[:200]
            record["attempts"] = len(self.login_attempts) - before
            record["active"] = self.engine.active_count
            if self.eyes is not None:
                record.update(self.eyes.population())
            self.population_log.append(record)

    def return_pacer(self, done):
        """The paused users come back one per period, unless the door closed."""
        period = self.protocol["return_period_seconds"]
        started = time.time()
        for position, label in enumerate(self.plan["paused_order"]):
            if self.stop_flag.stopped or done.is_set():
                return
            if not self.admission.admission_open:
                print(f"--- rientri fermati a {position} utenti: ADMISSION_STOP ---",
                      flush=True)
                return
            target = started + position * period
            now = time.time()
            if target > now and self.stop_flag.event.wait(target - now):
                return
            self.engine.expect_return(label, position)
            self.engine.admit(label)

    def pause_users(self):
        """The paused set stops calling. No logout, no freeze, no eviction.

        Removing a user from the admitted set is the whole mechanism: its rows are
        absent from the plan for the pause anyway, and it stays authenticated,
        placed and resident on its worker.
        """
        with self.engine.lock:
            for label in self.plan["paused_order"]:
                self.engine.calling.discard(label)
        print(f"--- pausa: {len(self.plan['paused_order'])} utenti smettono di "
              f"chiamare, {self.engine.active_count} restano attivi ---", flush=True)

    def log_out_all(self):
        """Every user logged out, one per period. Errors are recorded."""
        period = self.protocol["logout_period_seconds"]
        print(f"--- logout di {len(self.engine.runners)} utenti, uno ogni {period:.0f}s ---",
              flush=True)
        with self.engine.lock:
            self.engine.state["phase"] = "logout"
        for label, runner in list(self.engine.runners.items()):
            record = {"user": label, "ts": time.strftime("%H:%M:%S")}
            try:
                runner.user.log_out()
                record["outcome"] = "ok"
            except Exception as failure:                          # noqa: BLE001
                record["outcome"] = "error"
                record["error"] = repr(failure)[:200]
            self.logouts.append(record)
            time.sleep(period)
        failed = [record for record in self.logouts if record["outcome"] != "ok"]
        print(f"  logout: {len(self.logouts) - len(failed)} riusciti, {len(failed)} falliti",
              flush=True)

    # ------------------------------------------------------------------ certificati
    def certify_settings(self):
        """The live setpoints, or the leg is not measuring what it declared.

        The freeze is the one that matters most here: ``user_idle_freeze_minutes``
        must read back as null, because the core stores its infinity and JSON
        cannot carry it. A null is "never freeze". Anything else would move the
        paused users to disk and the pause would measure the freezer.
        """
        if self.eyes is None:
            return None
        live = self.eyes.live_settings()["effective_settings"]
        problems = [f"{key} vale {live.get(key)!r} invece di {value!r}"
                    for key, value in REQUIRED_SETTINGS.items() if live.get(key) != value]
        if live.get("worker_max_users") != self.arguments.expect_per_worker:
            problems.append(f"worker_max_users vale {live.get('worker_max_users')!r} "
                            f"invece di {self.arguments.expect_per_worker}")
        if live.get("worker_max_number") != self.arguments.expect_workers:
            problems.append(f"worker_max_number vale {live.get('worker_max_number')!r} "
                            f"invece di {self.arguments.expect_workers}")
        record = {"stage": "settings", "live_settings": live, "problemi": problems}
        self.checkpoints.append(record)
        print(f"  setpoint vivi: freeze={live.get('user_idle_freeze_minutes')} "
              f"cpu_grow={live.get('cpu_grow_percent')} "
              f"max_users={live.get('worker_max_users')} "
              f"max_number={live.get('worker_max_number')} "
              f"min_life={live.get('worker_min_life_seconds')}", flush=True)
        if problems:
            raise InvalidRun("setpoint non conformi: " + "; ".join(problems))
        return record

    def check_distribution(self, stage):
        """The bridge's declared shape. Blocking only at the full population."""
        if self.eyes is None:
            return None
        population = self.eyes.population()
        expected = [self.arguments.expect_per_worker] * self.arguments.expect_workers
        obtained = sorted(population["per_worker"].values(), reverse=True)
        problems = []
        if population["guest"]:
            problems.append(f"{population['guest']} guest presenti")
        if self.reached_full:
            if population["authenticated"] != self.plan["users"]:
                problems.append(f"utenti reali {population['authenticated']} "
                                f"invece di {self.plan['users']}")
            if obtained != sorted(expected, reverse=True):
                problems.append(f"distribuzione {obtained} invece di {expected}")
        if population["frozen"]:
            problems.append(f"{population['frozen']} utenti congelati: il freeze "
                            f"doveva restare spento")
        record = {"stage": stage, "expected": expected, "obtained": obtained,
                  "reached_full": self.reached_full, **population, "problemi": problems}
        self.checkpoints.append(record)
        print(f"  distribuzione [{stage}]: attesa {expected} ottenuta {obtained}", flush=True)
        if problems:
            raise InvalidRun(f"distribuzione non conforme a {stage}: " + "; ".join(problems))
        return record

    def certify_page_class_cache(self):
        """The page-class cache certificate, taken outside any measured window.

        The blocking part is what makes the two stacks comparable: the DB
        preference really True, the same GenroPy revision, a load that carries a
        ``page_id``, and no ``_avoid_module_cache``. A gap there stops the leg.

        The entries are only diagnostic. On the legacy they need an account that
        already carries ``superadmin`` or ``_DEV_``, and if it does not the
        certificate records ``entries_status: unavailable`` and the run goes on.
        """
        form = None
        runners = list(self.engine.runners.values())
        if runners:
            form = runners[0].user.get_call_form(self.lookups[0], 0)
        certificate = PageClassCacheCertificate(
            self.arguments.stack, self.arguments.base,
            container=self.arguments.container,
            instance=self.arguments.instance,
            genropy_tree=self.arguments.genropy_tree).certify(form=form)
        print(f"--- page-class cache [{self.arguments.stack}]: "
              f"preferenza={certificate['configuration_enabled']} "
              f"revisione={(certificate['genropy_revision'] or '?')[:12]} "
              f"page_id={certificate['requests_carry_page_id']} "
              f"bypass={certificate['avoid_module_cache']} | "
              f"entry {certificate['entries_status']}: {certificate['entries']} ---",
              flush=True)
        if certificate["entries_note"]:
            print(f"    entry non osservabili: {certificate['entries_note']}", flush=True)
        with open(f"{self.arguments.out}_page_class_cache.json", "w") as handle:
            json.dump(certificate, handle, indent=2)
        if certificate["blocking"]:
            raise InvalidRun("page-class cache: " + "; ".join(certificate["blocking"]))
        return certificate

    def get_worker_of_labels(self, placement):
        """The census placement, re-keyed from GenroPy usernames to ``user_N``.

        The census indexes ``user_worker_map`` by the SITE's own user id — the
        GenroPy username — while the load engine knows each user by the label it
        gave it. Without this translation the lookup never matched and the
        ``worker`` column of every call stayed empty, in this scenario and in the
        ones before it.

        A user the census does not place is simply absent from the result: an
        empty cell is a missing observation, never an invented worker.
        """
        return {label: placement[username]
                for label, username in self.engine.username_of.items()
                if placement.get(username)}

    # ------------------------------------------------------------------ campioni
    def sample_once(self, writer, handle):
        """One row: the engine's counters plus the container's own numbers."""
        started = time.time()
        latency, lateness, snapshot = self.engine.take_interval()
        if self.arguments.stack == "bridge" and self.eyes is not None:
            self.roles.worker_pids = self.eyes.worker_pids
        processes, cgroup, memory_stat, cpu_usec = self.probe.read()
        elapsed = started - (self.previous_stamp or started)
        sample = RoleSample(processes, cgroup, memory_stat, cpu_usec, self.roles,
                            previous_ticks=self.previous_ticks,
                            previous_cpu_usec=self.previous_cpu_usec,
                            elapsed=elapsed if elapsed > 0 else None)
        row = {
            "ts": time.strftime("%H:%M:%S"), "epoch": round(started, 3),
            "run": self.arguments.run, "phase": snapshot["phase"],
            "rate_offered": snapshot["level_rate"],
            "scheduled": snapshot["offered"], "done": snapshot["completed"],
            "errors_http": snapshot["errors_http"], "errors_app": snapshot["errors_app"],
            "errors_transport": snapshot["errors_transport"],
            "reqs_per_s": round(len(latency) / SAMPLE_S, 1),
            "p50_ms": self.engine.percentile(latency, 50),
            "p95_ms": self.engine.percentile(latency, 95),
            "p99_ms": self.engine.percentile(latency, 99),
            "late_p50_s": self.engine.percentile(lateness, 50),
            "late_max_s": round(lateness[-1], 4) if lateness else "",
            "pending": self.engine.pending,
            "users_active": self.engine.active_count,
            "users_paused": len(self.engine.runners) - self.engine.active_count,
            "admission_stop": int(not self.admission.admission_open),
            **sample.columns(),
        }
        if self.eyes is not None:
            population = self.eyes.population()
            placement = ((self.eyes.read_census() or {}).get("groups", {})
                         .get("pool", {}).get("user_worker_map", {}))
            self.engine.worker_of = self.get_worker_of_labels(placement)
            row.update({
                "users_authenticated": population.get("authenticated", ""),
                "users_placed": population.get("placed", ""),
                "users_unplaced": population.get("unplaced", ""),
                "users_frozen": population.get("frozen", ""),
                "users_guest": population.get("guest", ""),
                "connections": population.get("connections", ""),
                "pages": population.get("pages", ""),
                "users_per_worker": json.dumps(population.get("per_worker", {})),
            })
        else:
            row.update({"users_authenticated": len(self.engine.runners),
                        "users_placed": "", "users_unplaced": "", "users_frozen": "",
                        "users_guest": "", "connections": "", "pages": "",
                        "users_per_worker": ""})
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

    def check_sampler(self, where):
        if self.sampler_failed.is_set():
            raise SamplerDown(f"campionatore fermo durante {where}: {self.sampler_error}")

    # ------------------------------------------------------------------ le fasi
    def play_login_ramp(self):
        """The ramp: the pacer logs users in while the plan's rows are offered.

        The capture is read HERE, before the pacer starts: reading it inside the
        pacer would delay the first login by the read, and the first login is the
        instant the whole ramp is measured from.
        """
        capture = load_capture(SESSION_CAPTURE)
        login_calls, pages = build_plan(capture)
        done = threading.Event()
        pacer = threading.Thread(target=self.login_pacer, args=(done, login_calls, pages),
                                 daemon=True, name="login_pacer")
        pacer.start()
        try:
            self.engine.play_cycle_window("login_ramp")
        finally:
            done.set()
            pacer.join(timeout=30)
        self.phases_played.append("login_ramp")
        self.reached_full = len(self.engine.runners) == self.plan["users"]
        print(f"  login ramp finita: {len(self.engine.runners)} autenticati, "
              f"{self.engine.active_count} attivi, tentativi "
              f"{len(self.login_attempts)}, errori a freddo "
              f"{self.cold_start_errors}, popolazione piena: {self.reached_full}",
              flush=True)
        if self.login_failure:
            raise InvalidRun(f"un login non e' riuscito: {self.login_failure}")

    def play_return_ramp(self):
        """The return: the pacer readmits the paused users one per period."""
        done = threading.Event()
        pacer = threading.Thread(target=self.return_pacer, args=(done,),
                                 daemon=True, name="return_pacer")
        pacer.start()
        try:
            self.engine.play_cycle_window("return_ramp")
        finally:
            done.set()
            pacer.join(timeout=30)
        self.phases_played.append("return_ramp")

    def play_steady(self, phase):
        self.engine.play_cycle_window(phase)
        self.check_sampler(phase)
        self.phases_played.append(phase)

    def require_every_phase(self):
        """Every phase the plan declares was played, or this is not a comparison.

        A run that reports a subset of the phases looks like a result and is not
        one: the two stacks would be compared on different work. So the leg fails
        here, with the missing phases named, and the sequence stops before the
        other stack is even started.
        """
        declared = [window["phase"] for window in self.protocol["phases"]]
        missing = [phase for phase in declared if phase not in self.phases_played]
        problems = []
        if missing:
            problems.append(f"fasi non eseguite: {missing}")
        if not self.reached_full:
            problems.append(f"popolazione incompleta: {len(self.engine.runners)} utenti "
                            f"invece di {self.plan['users']}")
        withheld = sum(self.engine.withheld.values())
        if withheld and self.admission.admission_open:
            problems.append(f"{withheld} richieste trattenute senza ADMISSION_STOP")
        record = {"stage": "fasi", "declared": declared, "played": self.phases_played,
                  "problemi": problems}
        self.checkpoints.append(record)
        if problems:
            raise InvalidRun("verdetto delle fasi: " + "; ".join(problems))
        return record

    # ------------------------------------------------------------------ la corsa
    def run(self):
        calls_path = f"{self.arguments.out}_calls.csv"
        samples_path = f"{self.arguments.out}_samples.csv"
        calls_handle = open(calls_path, "w", newline="")
        calls_writer = csv.DictWriter(calls_handle, fieldnames=LoadEngine.CALL_FIELDS,
                                      extrasaction="ignore")
        calls_writer.writeheader()
        samples_handle = open(samples_path, "w", newline="")
        samples_writer = csv.DictWriter(samples_handle, fieldnames=CYCLE_COLUMNS,
                                        extrasaction="ignore")
        samples_writer.writeheader()
        self.engine = CycleEngine(self.plan["calls"], self.body_for, self.lookups,
                                  self.stop_flag, calls_writer, calls_handle,
                                  admission=self.admission)
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
        self.admission.start()
        outcome = {"stack": self.arguments.stack, "run": self.arguments.run,
                   "plan": os.path.basename(self.arguments.plan),
                   "plan_sha256": self.plan_sha256,
                   "users_planned": self.plan["users"],
                   "paused_planned": self.plan["paused"]}
        try:
            if not self.sampler_ready.wait(timeout=20.0):
                raise SamplerDown(f"nessuna riga campionata: {self.sampler_error}")
            # La certificazione dei ruoli a vuoto: il pool non e' ancora nato,
            # perche' un worker nasce solo da un placement. Serve come baseline,
            # non come forma attesa.
            outcome["role_certification_baseline"] = self.probe.certify(
                self.probe.read()[0], self.expected_workers_now)
            self.stop_flag.wait(self.protocol["baseline_seconds"], "baseline")
            self.play_login_ramp()
            self.stop_flag.wait(self.protocol["settle_seconds"], "quiete dopo i login")
            # ORA la forma e' quella della corsa, e la certificazione ha senso:
            # la precedente e' presa a pool vuoto.
            outcome["role_certification"] = self.probe.certify(
                self.probe.read()[0], self.expected_workers_now)
            self.certify_settings()
            self.check_distribution("dopo i login")
            outcome["page_class_cache"] = self.certify_page_class_cache()
            self.check_sampler("prima delle finestre")
            self.play_steady("full_warmup")
            self.play_steady("full_measure_1")
            self.pause_users()
            self.play_steady("pause_50")
            self.play_return_ramp()
            self.check_sampler("return_ramp")
            self.play_steady("full_measure_2")
            self.check_distribution("alla fine delle finestre")
            self.require_every_phase()
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
            self.close(guard, sampler, stop_sampler, samples_handle, calls_handle, outcome)
        return outcome

    def close(self, guard, sampler, stop_sampler, samples_handle, calls_handle, outcome):
        """The logout, the observation, the writers, the guards. Always, in order."""
        try:
            self.log_out_all()
        except Exception as failure:                              # noqa: BLE001
            outcome["logout_error"] = repr(failure)[:200]
        with self.engine.lock:
            self.engine.state["phase"] = "observe"
        observe = self.protocol["observe_seconds"]
        print(f"--- observe ({observe:.0f}s, nessuna richiesta) ---", flush=True)
        deadline = time.time() + observe
        while time.time() < deadline:
            time.sleep(0.5)
        alive = self.engine.stop_runners()
        if alive:
            outcome["threads_alive"] = alive
        stop_sampler.set()
        sampler.join(timeout=15)
        self.admission.finished.set()
        self.admission.join(timeout=15)
        self.admission.write()
        guard.driver_finished.set()
        guard.join(timeout=15)
        verdict = guard.final_check()
        guard.write(verdict)
        outcome["memory"] = verdict
        outcome["admission"] = self.admission.verdict
        outcome["reached_full_population"] = self.reached_full
        outcome["login_attempts_total"] = len(self.login_attempts)
        outcome["cold_start_errors"] = self.cold_start_errors
        outcome["login_failure"] = self.login_failure
        outcome["phases_played"] = self.phases_played
        outcome["withheld_by_phase"] = self.engine.withheld
        outcome["stop_reasons"] = self.stop_flag.reason_list
        outcome["sampler_rows"] = self.sampler_rows
        samples_handle.close()
        calls_handle.close()
        self.engine.write_windows(f"{self.arguments.out}_windows.json")
        for name, payload in (("checkpoints", self.checkpoints),
                              ("login_attempts", self.login_attempts),
                              ("population_log", self.population_log),
                              ("logouts", self.logouts),
                              ("return_calls", self.engine.return_calls),
                              ("outcome", outcome)):
            with open(f"{self.arguments.out}_{name}.json", "w") as handle:
                json.dump(payload, handle, indent=2)
        if self.eyes is not None:
            with open(f"{self.arguments.out}_journal_events.json", "w") as handle:
                json.dump(self.eyes.read_journal_events(), handle, indent=2)
        print(f"FINE {self.arguments.run}: esito {outcome['result']}, "
              f"popolazione piena {self.reached_full}, "
              f"offerte {self.engine.state['offered']} "
              f"avviate {self.engine.state['started']} "
              f"completate {self.engine.state['completed']} | "
              f"errori http {self.engine.state['errors_http']} "
              f"app {self.engine.state['errors_app']} "
              f"trasporto {self.engine.state['errors_transport']} | "
              f"memory stop {verdict['memory_stop']} "
              f"admission stop {self.admission.verdict['admission_stop']} | "
              f"tentativi di login {len(self.login_attempts)} "
              f"a freddo {self.cold_start_errors}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", required=True, choices=("bridge", "legacy"))
    parser.add_argument("--run", required=True, help="il nome della corsa: battezza gli output")
    parser.add_argument("--base", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--password", default="a")
    parser.add_argument("--expect-workers", type=int, required=True)
    parser.add_argument("--expect-per-worker", type=int, required=True)
    parser.add_argument("--memory-threshold", type=float, default=80.0)
    parser.add_argument("--plan-sha256", default=None,
                        help="il digest che la campagna ha registrato una volta: "
                             "questa esecuzione si ferma se legge altri byte")
    parser.add_argument("--instance", default=None,
                        help="il nome dell'instance: la certificazione della "
                             "page-class cache legge la preferenza da un processo "
                             "di servizio nel container")
    parser.add_argument("--genropy-tree", default=None,
                        help="il path host del tree GenroPy montato: la sua "
                             "revisione deve essere la stessa sui due stack")
    parser.add_argument("--census", default=None, help="solo bridge")
    parser.add_argument("--journal", default=None, help="solo bridge")
    arguments = parser.parse_args(argv)
    probe = CycleProbe(arguments)
    probe.stop_flag.install_signal_handlers()
    outcome = probe.run()
    if outcome["result"] == "completa" and not outcome["memory"]["safety_fail"]:
        return 0
    if outcome["memory"]["safety_fail"]:
        return 7
    if outcome["result"] == "interrotta":
        return 5
    if outcome["result"] == "non valida":
        return 3
    return 2


if __name__ == "__main__":
    sys.exit(main())
