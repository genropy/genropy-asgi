# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""One leg of the L120 comparison: the same run, against one stack.

The driver is invoked once per stack, on the same boot of the same machine, and
the load it sends is byte-identical in both invocations: the plan file decides
the accounts, the instants and the lookups, and the applicative part of this
file knows nothing about which stack answers.

WHAT DIFFERS BETWEEN THE TWO LEGS, and nothing else:

- the ROLE MAP of the sampler — commander/template/pool workers on the bridge,
  daemon/master/Gunicorn workers on the legacy;
- the OPTIONAL bridge observations — census, decision journal, topology, and the
  hot application of the measured policy. They are reads, plus one POST to
  ``/_orchestration/apply`` which the legacy has no equivalent of.

The timeline is the SAME on both legs, including the wait that follows the
policy application: on the legacy that wait is simply quiet time. If one leg
waited and the other did not, the two measures would begin from different cache
and connection states and the comparison would be worth nothing.

Every phase reads the stop flag, so TERM, INT and the memory guard end the run
wherever it is, and the logout always runs.

    python3 compare_probe.py --stack bridge --run p1_bridge \\
        --base http://127.0.0.1:8098 --container genro-bench-lab-bridge-1 \\
        --plan traces/l120_plan.json --out /work/p1_bridge \\
        --census http://127.0.0.1:8098/_server/inspector/census \\
        --journal /lab/runtime/p1_bridge_orders.decisions.jsonl \\
        --expect-workers 4 --expect-per-worker 12
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import threading
import time
import urllib.parse

BENCHMARKS_DIR = os.path.abspath(
    os.environ.get("BENCHMARKS_DIR")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
sys.path.insert(0, BENCHMARKS_DIR)
sys.path.insert(0, os.path.join(BENCHMARKS_DIR, "bench_common"))

from bench_common.bridge_eyes import BridgeEyes                                  # noqa: E402
from bench_common.container_probe import (                                       # noqa: E402
    COLUMNS, BridgeRoles, ContainerProbe, LegacyRoles, RoleSample)
from bench_common.load_engine import LoadEngine, UserRunner                      # noqa: E402
from bench_common.page_class_cache import PageClassCacheCertificate              # noqa: E402
from bench_common.stop_guard import (                                            # noqa: E402
    ContainerCgroup, MemoryGuard, StopFlag, StopRequested)
from churn_driver import LoggedUser, build_plan, load_capture                    # noqa: E402

SESSION_CAPTURE = os.path.join(BENCHMARKS_DIR, "session_capture.jsonl")
USERNAMES_ALL = os.path.join(BENCHMARKS_DIR, "usernames_all.txt")
SAMPLE_S = 2.0

# La policy della finestra misurata, applicata a caldo dopo il popolamento.
# worker_min_life_seconds e' un CONTROLLO SPERIMENTALE: impedisce che il
# retirement chiuda un worker durante la corsa e cambi la topologia da sola.
MEASURED_POLICY = {
    "cpu_grow_percent": 50.0,
    "cpu_grow_rearm_percent": 30.0,
    "occupancy_max_percent": 80.0,
    "reception_reserved_percent": 0.0,
    "cpu_retirement_quiet_seconds": 60.0,
    "restart_occupancy_max_percent": 95.0,
    "worker_min_life_seconds": 3600.0,
}


class InvalidRun(RuntimeError):
    """A structural criterion failed: the run's data is not comparable."""


class SamplerDown(RuntimeError):
    """The sampler produced nothing: there is no measure."""


class CompareProbe:
    """One leg: population, policy, three windows, logout, observation."""

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
        self.engine = None
        self.checkpoints = []
        self.population_log = []
        self.logouts = []
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

    # ------------------------------------------------------------------ carico
    def body_for(self, user, lookup, counter):
        """The load unit: the same indexed getSelection on both stacks."""
        return urllib.parse.urlencode(user.get_call_form(lookup, counter))

    def log_in_all(self):
        """The logins at the plan's cadence, one check after each."""
        period = self.protocol["login_period_seconds"]
        total = self.plan["users"]
        print(f"--- popolamento: {total} utenti, uno ogni {period:.0f}s ---", flush=True)
        with self.engine.lock:
            self.engine.state["phase"] = "login"
        started = time.time()
        capture = load_capture(SESSION_CAPTURE)
        login_calls, pages = build_plan(capture)
        for index in range(total):
            self.stop_flag.raise_if_stopped("popolamento")
            if time.time() - started > self.protocol["population_timeout_seconds"]:
                raise InvalidRun(f"popolamento oltre "
                                 f"{self.protocol['population_timeout_seconds']:.0f}s "
                                 f"con {index} utenti")
            label = f"user_{index + 1}"
            logged = LoggedUser(self.arguments.base, login_calls, pages,
                               self.accounts[index], self.arguments.password,
                               self.lookups, 0.0)
            runner = UserRunner(self.engine, label, logged)
            runner.start()
            self.engine.runners[label] = runner
            record = {"login": index + 1, "user": label, "username": self.accounts[index],
                      "ts": time.strftime("%H:%M:%S")}
            if self.eyes is not None:
                population = self.eyes.population()
                record.update(population)
                if population.get("guest"):
                    raise InvalidRun(f"al login {index + 1} compaiono "
                                     f"{population['guest']} guest")
                if len(population.get("workers", [])) > self.arguments.expect_workers:
                    raise InvalidRun(f"al login {index + 1} i worker sono "
                                     f"{len(population['workers'])}, attesi al massimo "
                                     f"{self.arguments.expect_workers}")
            self.population_log.append(record)
            if not self.stop_flag.wait(period, "popolamento"):
                break

    def log_out_all(self):
        """Every user logged out, whatever happened before. Errors are recorded."""
        print(f"--- logout di {len(self.engine.runners)} utenti ---", flush=True)
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
        failed = [record for record in self.logouts if record["outcome"] != "ok"]
        print(f"  logout: {len(self.logouts) - len(failed)} riusciti, {len(failed)} falliti",
              flush=True)

    # ------------------------------------------------------------------ campioni
    def check_distribution(self):
        """The bridge's declared shape, or the run is not comparable."""
        if self.eyes is None:
            return None
        population = self.eyes.population()
        expected = [self.arguments.expect_per_worker] * self.arguments.expect_workers
        obtained = sorted(population["per_worker"].values(), reverse=True)
        problems = []
        if population["authenticated"] != self.plan["users"]:
            problems.append(f"utenti reali {population['authenticated']} "
                            f"invece di {self.plan['users']}")
        if population["guest"]:
            problems.append(f"{population['guest']} guest presenti")
        if obtained != sorted(expected, reverse=True):
            problems.append(f"distribuzione {obtained} invece di {expected}")
        record = {"stage": "distribution", "expected": expected, "obtained": obtained,
                  **population, "problemi": problems}
        self.checkpoints.append(record)
        print(f"  distribuzione: attesa {expected} ottenuta {obtained}", flush=True)
        if problems:
            raise InvalidRun("distribuzione non conforme: " + "; ".join(problems))
        return record

    def apply_policy(self):
        """Hot policy on the bridge; on the legacy, only the same quiet wait."""
        wait = self.protocol["apply_wait_seconds"]
        if self.eyes is None:
            print(f"--- legacy: nessuna policy da applicare, attesa di {wait:.0f}s "
                  f"per pareggiare la linea del tempo ---", flush=True)
            with self.engine.lock:
                self.engine.state["phase"] = "apply_wait"
            self.stop_flag.wait(wait, "attesa di pareggio")
            return None
        before = self.eyes.topology
        outcome = self.eyes.apply_settings(MEASURED_POLICY)
        print(f"--- apply: {outcome['outcome']} generation {outcome['generation']} "
              f"changed {json.dumps(outcome['changed_settings'])} ---", flush=True)
        with self.engine.lock:
            self.engine.state["phase"] = "apply_wait"
        print(f"--- attesa di {wait:.0f}s senza richieste ---", flush=True)
        self.stop_flag.wait(wait, "attesa dopo l'apply")
        after = self.eyes.topology
        live = self.eyes.live_settings()["effective_settings"]
        problems = [f"{key} vale {live.get(key)} invece di {value}"
                    for key, value in MEASURED_POLICY.items() if live.get(key) != value]
        if before["workers"] != after["workers"]:
            problems.append(f"worker cambiati: {before['workers']} -> {after['workers']}")
        if before["map"] != after["map"]:
            problems.append("la mappa utenti->worker e' cambiata durante l'apply")
        record = {"stage": "apply", "apply": outcome, "live_settings": live,
                  "before": before, "after": after, "problemi": problems}
        self.checkpoints.append(record)
        print(f"  policy viva certificata: {not problems}", flush=True)
        if problems:
            raise InvalidRun("apply non conforme: " + "; ".join(problems))
        return record

    def certify_page_class_cache(self):
        """The page-class cache certificate, taken between warmup and measure.

        The blocking part is what makes the two stacks comparable: the DB
        preference really True, the same GenroPy revision, a load that carries a
        ``page_id``, and no ``_avoid_module_cache``. A gap there stops the leg.

        The entries are only diagnostic. On the legacy they need an account that
        already carries ``superadmin`` or ``_DEV_``, and if it does not the
        certificate records ``entries_status: unavailable`` and the run goes on: no
        tag is granted, no account created, nothing added to the measured path.
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
            **sample.columns(),
        }
        if self.eyes is not None:
            population = self.eyes.population()
            self.engine.worker_of = dict(
                (self.eyes.read_census() or {}).get("groups", {}).get("pool", {})
                .get("user_worker_map", {}))
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

    # ------------------------------------------------------------------ la corsa
    def run(self):
        calls_path = f"{self.arguments.out}_calls.csv"
        samples_path = f"{self.arguments.out}_samples.csv"
        calls_handle = open(calls_path, "w", newline="")
        calls_writer = csv.DictWriter(calls_handle, fieldnames=LoadEngine.CALL_FIELDS,
                                      extrasaction="ignore")
        calls_writer.writeheader()
        samples_handle = open(samples_path, "w", newline="")
        samples_writer = csv.DictWriter(samples_handle, fieldnames=COLUMNS, extrasaction="ignore")
        samples_writer.writeheader()
        self.engine = LoadEngine(self.plan["calls"], self.body_for, self.lookups,
                                 self.stop_flag, calls_writer, calls_handle)
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
            if not self.sampler_ready.wait(timeout=20.0):
                raise SamplerDown(f"nessuna riga campionata: {self.sampler_error}")
            certification = self.probe.certify(self.probe.read()[0], self.arguments.expect_workers)
            outcome["role_certification"] = certification
            self.stop_flag.wait(self.protocol["baseline_seconds"], "baseline")
            self.log_in_all()
            self.check_distribution()
            self.apply_policy()
            self.check_sampler("prima delle finestre")
            for window in self.protocol["windows"]:
                self.engine.play_window(window["phase"])
                self.check_sampler(window["phase"])
                if window["phase"] == "warmup":
                    # FUORI dalla finestra misurata, e col carico fermo: il
                    # warmup ha appena aperto le pagine, quindi ci sono entry
                    # eleggibili, e la misura non e' ancora iniziata.
                    outcome["page_class_cache"] = self.certify_page_class_cache()
            self.check_distribution()
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
        """The logout, the observation, the writers, the guard. Always, in order."""
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
        guard.driver_finished.set()
        guard.join(timeout=15)
        verdict = guard.final_check()
        guard.write(verdict)
        outcome["memory"] = verdict
        outcome["stop_reasons"] = self.stop_flag.reason_list
        outcome["sampler_rows"] = self.sampler_rows
        samples_handle.close()
        calls_handle.close()
        self.engine.write_windows(f"{self.arguments.out}_windows.json")
        for name, payload in (("checkpoints", self.checkpoints),
                              ("population_log", self.population_log),
                              ("logouts", self.logouts),
                              ("outcome", outcome)):
            with open(f"{self.arguments.out}_{name}.json", "w") as handle:
                json.dump(payload, handle, indent=2)
        if self.eyes is not None:
            with open(f"{self.arguments.out}_journal_events.json", "w") as handle:
                json.dump(self.eyes.read_journal_events(), handle, indent=2)
        print(f"FINE {self.arguments.run}: esito {outcome['result']}, "
              f"offerte {self.engine.state['offered']} "
              f"avviate {self.engine.state['started']} "
              f"completate {self.engine.state['completed']} | "
              f"errori http {self.engine.state['errors_http']} "
              f"app {self.engine.state['errors_app']} "
              f"trasporto {self.engine.state['errors_transport']} | "
              f"memory stop {verdict['memory_stop']}", flush=True)


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
                             "questa gamba si ferma se legge altri byte")
    parser.add_argument("--instance", default=None,
                        help="il nome dell'instance: la certificazione della "
                             "page-class cache legge la preferenza da un processo "
                             "di servizio nel container")
    parser.add_argument("--genropy-tree", default=None,
                        help="il path host del tree GenroPy montato: la sua "
                             "revisione deve essere la stessa sulle due gambe")
    parser.add_argument("--census", default=None, help="solo bridge")
    parser.add_argument("--journal", default=None, help="solo bridge")
    arguments = parser.parse_args(argv)
    probe = CompareProbe(arguments)
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
