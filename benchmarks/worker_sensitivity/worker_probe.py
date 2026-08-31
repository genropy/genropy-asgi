"""Sensitivity sul numero di worker del bridge: W1, W2, W4, W8 a 48 utenti.

Il generatore NON e' un loop sincrono. Uno scheduler a ritmo globale accoda le
richieste della traccia; ogni utente ha la propria sessione, la propria
connessione e un thread che le consuma in ordine, cosi' le richieste di utenti
diversi si sovrappongono mentre lo stesso utente non ne ha mai due in volo.

La traccia e' materializzata una sola volta per tutte le corse. L'unica leva
che cambia fra W1/W2/W4/W8 e' ``worker_max_users`` nella recipe.
"""

import argparse
import csv
import http.client
import json
import os
import queue
import statistics
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request

# Le dipendenze del driver stanno nella directory benchmarks, una sopra questo
# scenario. Si risolvono dalla posizione di QUESTO file, mai dalla directory
# corrente: il runner puo' essere invocato da ovunque. BENCHMARKS_DIR le sposta
# altrove quando lo scenario e' installato fuori dal repository.
BENCHMARKS_DIR = os.path.abspath(
    os.environ.get("BENCHMARKS_DIR")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
)
CHURN_DRIVER = os.path.join(BENCHMARKS_DIR, "churn_driver.py")
SESSION_CAPTURE = os.path.join(BENCHMARKS_DIR, "session_capture.jsonl")
USERNAMES_ALL = os.path.join(BENCHMARKS_DIR, "usernames_all.txt")

_missing = [p for p in (CHURN_DRIVER, SESSION_CAPTURE, USERNAMES_ALL) if not os.path.isfile(p)]
if _missing:
    sys.exit(
        "dipendenze del driver assenti in BENCHMARKS_DIR=%s:\n  %s\n"
        "Impostare BENCHMARKS_DIR sulla directory benchmarks del repository."
        % (BENCHMARKS_DIR, "\n  ".join(_missing))
    )

sys.path.insert(0, BENCHMARKS_DIR)
from churn_driver import LoggedUser, load_capture, build_plan  # noqa: E402

USERS = 48
BASELINE_S, SETTLE_S, OBSERVE_S = 60.0, 30.0, 120.0
LOGIN_PERIOD_S = 3.0         # cadenza prudente dei 48 login
APPLY_WAIT_S = 80.0          # due beat pieni piu' margine, senza richieste
POPULATION_TIMEOUT_S = 900.0 # oltre, la preparazione fallisce
# La policy della fase MISURATA, applicata a caldo dopo il popolamento.
# worker_min_life_seconds=3600 e' un CONTROLLO SPERIMENTALE per isolare
# l'effetto del numero di worker: non e' una configurazione di produzione.
MEASURED_POLICY = {
    "cpu_grow_percent": 50.0,
    "cpu_grow_rearm_percent": 30.0,
    "occupancy_max_percent": 80.0,
    "reception_reserved_percent": 0.0,
    "cpu_retirement_quiet_seconds": 60.0,
    "restart_occupancy_max_percent": 95.0,
    "worker_min_life_seconds": 3600.0,
}
SAMPLE_S = 2.0
SATURATION_P95_S = 1.0          # oltre questo, per due mezzi minuti di fila, il livello e' saturo
SATURATION_WINDOW_S = 30.0


class SamplerDown(RuntimeError):
    """Il campionatore non produce dati: la misura non e' valida."""


class InvalidRun(RuntimeError):
    """La corsa non soddisfa un criterio strutturale: i suoi dati vanno in invalid/."""


class Saturated(RuntimeError):
    """Il livello ha superato p95=1 s per due finestre consecutive."""


class UserRunner(threading.Thread):
    """Un utente: la sua sessione, la sua connessione, le sue richieste in ordine."""

    def __init__(self, probe, label, logged_user):
        super().__init__(daemon=True, name=label)
        self.probe = probe
        self.label = label
        self.user = logged_user
        self.inbox = queue.Queue()
        self.counter = 0

    def run(self):
        while True:
            item = self.inbox.get()
            if item is None:
                return
            scheduled_at, phase = item
            started_at = time.time()
            lookup = self.probe.lookups[self.counter % len(self.probe.lookups)]
            body = urllib.parse.urlencode(self.user.get_call_form(lookup, self.counter + 100))
            status, error = self.send(body)
            completed_at = time.time()
            self.counter += 1
            self.probe.record_call({
                "phase": phase, "user": self.label,
                "scheduled_at": round(scheduled_at, 6),
                "started_at": round(started_at, 6),
                "completed_at": round(completed_at, 6),
                "lateness_s": round(started_at - scheduled_at, 6),
                "latency_ms": round((completed_at - started_at) * 1000, 3),
                "status": status, "error": error,
                "worker": self.probe.worker_of.get(self.label, ""),
            })

    def send(self, body):
        """Una richiesta; un solo retry sulle eccezioni di TRASPORTO, mai sugli status."""
        for attempt in (1, 2):
            try:
                self.user.connection.request("POST", "/", body=body, headers=self.user.headers)
                answer = self.user.connection.getresponse()
                payload = answer.read()
                if answer.status != 200:
                    return answer.status, f"http {answer.status}"
                if b"<error>" in payload:
                    return answer.status, "application error"
                return answer.status, ""
            except (http.client.HTTPException, OSError) as failure:
                try:
                    self.user.connection.close()
                except Exception:
                    pass
                if attempt == 2:
                    return None, f"{type(failure).__name__}: {failure}"
                self.user.connection = http.client.HTTPConnection(self.probe.netloc, timeout=30)
        return None, "unreachable"


class Probe:
    def __init__(self, arguments):
        self.arguments = arguments
        self.netloc = urllib.parse.urlparse(arguments.base).netloc
        self.trace = [json.loads(line) for line in open(arguments.trace) if line.strip()]
        self.lock = threading.Lock()
        self.state = {"phase": "baseline", "level_rate": 0.0, "scheduled": 0,
                      "done": 0, "errors": 0, "transport_errors": 0,
                      "lat": [], "late": [], "logins": 0, "logouts": 0}
        self.runners = {}
        self.worker_of = {}
        self.logouts = []
        self.checkpoints = []
        self.population_log = []
        self.frozen_topology = None
        self.topology_broken = None
        self.users = getattr(arguments, "users", None) or USERS
        self.previous_cpu = {}
        self.previous_stamp = None
        self.sampler_ready = threading.Event()
        self.sampler_failed = threading.Event()
        self.sampler_error = None
        self.sampler_rows = 0
        self.sampler_seen = 0
        self.sampler_checked_at = 0.0
        self.saturation_marks = []
        self.windows = []
        self.window_calls = []
        self.journal_offset = 0

    # ------------------------------------------------------------------ carico
    def record_call(self, row):
        with self.lock:
            self.state["done"] += 1
            self.state["lat"].append(row["latency_ms"])
            self.state["late"].append(row["lateness_s"])
            if row["error"]:
                self.state["errors"] += 1
                if row["status"] is None:
                    self.state["transport_errors"] += 1
        self.calls_writer.writerow(row)
        self.calls_handle.flush()
        with self.lock:
            self.window_calls.append({"phase": row["phase"], "lateness_s": row["lateness_s"],
                                      "error": row["error"]})

    def play_window(self, phase):
        """Accoda le righe della finestra al ritmo che dichiarano. Non attende le risposte."""
        rows = [row for row in self.trace if row["phase"] == phase]
        if not rows:
            # La traccia non porta questa finestra: e' il caso dello smoke test,
            # che ne materializza solo alcune. Saltarla non e' un errore.
            print(f"--- {phase}: assente nella traccia, saltata ---", flush=True)
            return
        rate = rows[0]["rate"]
        with self.lock:
            self.state["phase"] = phase
            self.state["level_rate"] = rate
        print(f"--- {phase}: {len(rows)} richieste a {rate:.0f}/s ---", flush=True)
        self.check_sampler(f"inizio {phase}")
        self.check_watch(f"inizio {phase}")
        self.saturation_marks = []
        started = time.time()
        last_check = started
        for row in rows:
            target = started + row["t_rel"]
            now = time.time()
            if target > now:
                time.sleep(target - now)
            with self.lock:
                self.state["scheduled"] += 1
            self.runners[row["user"]].inbox.put((target, phase))
            if time.time() - last_check >= SATURATION_WINDOW_S:
                last_check = time.time()
                if self.saturated():
                    raise Saturated(f"{phase}: p95 oltre {SATURATION_P95_S:.0f}s "
                                    f"per due finestre da {SATURATION_WINDOW_S:.0f}s")
        self.drain()
        self.check_sampler(f"fine {phase}")
        self.check_watch(phase)
        self.close_window(phase, rows, started)

    def close_window(self, phase, rows, started):
        """Le metriche del generatore per questa finestra, e il suo giudizio."""
        calls = [c for c in self.window_calls if c["phase"] == phase]
        self.window_calls = [c for c in self.window_calls if c["phase"] != phase]
        seconds = max(rows[-1]["t_rel"] + 1.0 / rows[0]["rate"], 1e-9)
        late = sorted(c["lateness_s"] for c in calls)
        meta = len(late) // 2
        prima = statistics.median(late[:meta]) if meta else 0.0
        seconda = statistics.median(late[meta:]) if meta else 0.0
        pending = sum(runner.inbox.qsize() for runner in self.runners.values())
        record = {
            "phase": phase, "seconds": round(seconds, 2),
            "offered": len(rows), "started": len(calls),
            "offered_per_s": round(len(rows) / seconds, 2),
            "started_per_s": round(len(calls) / seconds, 2),
            "completed_per_s": round(len(calls) / seconds, 2),
            "started_ratio": round(len(calls) / len(rows), 4),
            "late_p50_s": round(statistics.median(late), 4) if late else None,
            "late_p95_s": round(late[max(0, int(len(late) * 0.95) - 1)], 4) if late else None,
            "late_max_s": round(late[-1], 4) if late else None,
            "late_first_half_s": round(prima, 4), "late_second_half_s": round(seconda, 4),
            "late_drift_s": round(seconda - prima, 4),
            "pending_at_end": pending,
            "errors": len([c for c in calls if c["error"]]),
        }
        problemi = []
        if record["started_ratio"] < 0.99:
            problemi.append(f"avviate {record['started_ratio']*100:.1f}% delle previste")
        if record["late_drift_s"] > 0.100:
            problemi.append(f"deriva della lateness +{record['late_drift_s']*1000:.0f} ms")
        if pending:
            problemi.append(f"{pending} richieste non completate a fine fase")
        record["generator_valid"] = not problemi
        record["problemi"] = problemi
        self.windows.append(record)
        print(f"  [{phase}] offerte {len(rows)} avviate {len(calls)} "
              f"({record['started_ratio']*100:.1f}%) lateness p50 {record['late_p50_s']}s "
              f"max {record['late_max_s']}s deriva {record['late_drift_s']:+.3f}s "
              f"-> generatore {'valido' if record['generator_valid'] else 'NON VALIDO: ' + '; '.join(problemi)}",
              flush=True)

    def saturated(self):
        """Due mezzi minuti di fila con p95 oltre la soglia: il livello e' saturo."""
        with self.lock:
            recent = sorted(self.state["lat"][-int(self.state["level_rate"] * SATURATION_WINDOW_S) or -1:])
        if not recent:
            return False
        p95 = recent[max(0, int(len(recent) * 0.95) - 1)] / 1000.0
        self.saturation_marks.append(p95 > SATURATION_P95_S)
        return len(self.saturation_marks) >= 2 and all(self.saturation_marks[-2:])

    def drain(self):
        """Attende che le code degli utenti si svuotino: nessuna richiesta in volo."""
        for runner in self.runners.values():
            runner.inbox.join() if False else None
        deadline = time.time() + 60
        while time.time() < deadline:
            if all(runner.inbox.empty() for runner in self.runners.values()):
                time.sleep(1.0)
                if all(runner.inbox.empty() for runner in self.runners.values()):
                    return
            time.sleep(0.2)

    # ------------------------------------------------------------------ sessioni
    def log_in_all(self, accounts):
        """I 48 login a cadenza prudente, con un controllo dopo ciascuno."""
        print(f"--- popolamento: {self.users} utenti, uno ogni {LOGIN_PERIOD_S:.0f}s ---", flush=True)
        with self.lock:
            self.state["phase"] = "login"
        atteso = self.arguments.expect_workers
        started = time.time()
        for index in range(self.users):
            if time.time() - started > POPULATION_TIMEOUT_S:
                raise InvalidRun(f"popolamento oltre {POPULATION_TIMEOUT_S:.0f}s "
                                 f"con {index} utenti collocati")
            label = f"user_{index + 1}"
            logged = LoggedUser(self.arguments.base, self.login_calls, self.pages,
                                accounts[index], "a", self.lookups, 0.0)
            runner = UserRunner(self, label, logged)
            runner.start()
            self.runners[label] = runner
            with self.lock:
                self.state["logins"] += 1
            census = self.read_census()
            if census is None:
                raise InvalidRun(f"census non leggibile dopo il login di {label}")
            group = census["groups"]["pool"]
            vivi = group["living_workers"]
            reali = [u for u in census["user_map"] if not u.startswith("guest_")]
            guest = [u for u in census["user_map"] if u.startswith("guest_")]
            conteggi = {}
            for utente, worker in group["user_worker_map"].items():
                if not utente.startswith("guest_"):
                    conteggi[worker] = conteggi.get(worker, 0) + 1
            riga = {"login": index + 1, "user": label, "username": accounts[index],
                    "ts": time.strftime("%H:%M:%S"), "workers": list(vivi),
                    "counts": dict(conteggi), "users_real": len(reali), "guests": len(guest)}
            self.population_log.append(riga)
            if len(vivi) > atteso:
                raise InvalidRun(f"al login {index+1} i worker sono {len(vivi)} "
                                 f"(attesi al massimo {atteso}): {vivi}")
            if guest:
                raise InvalidRun(f"al login {index+1} compaiono {len(guest)} guest: {guest}")
            eventi = self.read_journal_events()
            for parola in ("placement_fallback", "cpu_closed_hard_cap_fallback",
                           "restart_worker", "process_quitted"):
                if eventi.get(parola):
                    raise InvalidRun(f"{parola} x{eventi[parola]} al login {index+1}")
            if (index + 1) % 6 == 0:
                print(f"    {index+1}/{self.users}: worker={vivi} conteggi={conteggi}", flush=True)
            time.sleep(LOGIN_PERIOD_S)

    def log_out_all(self):
        print("--- logout di tutti gli utenti ---", flush=True)
        with self.lock:
            self.state["phase"] = "logout"
        for label in sorted(self.runners, key=lambda n: int(n.split("_")[1]), reverse=True):
            self.log_out(label)

    def log_out(self, label):
        runner = self.runners[label]
        user = runner.user
        entry = {"ts": time.strftime("%H:%M:%S"), "user": label, "username": user.username,
                 "status": None, "body": None, "exception": None, "outcome": None, "attempts": []}
        body = urllib.parse.urlencode({"method": "connection.logout",
                                       "page_id": user.page_id, "callcounter": "9999"})
        for attempt in (1, 2):
            record = {"attempt": attempt, "ts": time.strftime("%H:%M:%S"), "status": None,
                      "body": None, "exception": None, "error_kind": None, "connection_closed": False}
            try:
                user.connection.request("POST", "/", body=body, headers=user.headers)
                answer = user.connection.getresponse()
                payload = answer.read()
                record["status"] = answer.status
                record["body"] = payload[:200].decode("utf-8", "replace")
                entry["attempts"].append(record)
                entry["status"] = answer.status
                entry["body"] = record["body"]
                entry["outcome"] = "ok" if answer.status == 200 else "http_error"
                break
            except Exception as failure:
                transport = isinstance(failure, (http.client.HTTPException, OSError))
                record["exception"] = f"{type(failure).__name__}: {failure}"
                record["error_kind"] = "transport" if transport else "other"
                try:
                    user.connection.close()
                    record["connection_closed"] = True
                except Exception:
                    pass
                entry["attempts"].append(record)
                entry["exception"] = record["exception"]
                entry["outcome"] = "exception"
                if attempt == 2 or not transport:
                    break
                user.connection = http.client.HTTPConnection(self.netloc, timeout=30)
        self.logouts.append(entry)
        return entry

    # --------------------------------------------------------------- misurazione
    def read_census(self):
        try:
            with urllib.request.urlopen(self.arguments.census, timeout=4) as answer:
                return json.load(answer)["site"]
        except Exception:
            return None

    def read_container(self, worker_pids):
        """Ogni processo con cmdline intera e le sei grandezze di smaps_rollup."""
        script = (
            r'for d in /proc/[0-9]*; do p=${d#/proc/}; [ -r $d/stat ] || continue; '
            r'ppid=$(awk "{print \$4}" $d/stat 2>/dev/null); '
            r'ut=$(awk "{print \$14+\$15}" $d/stat 2>/dev/null); '
            r'cmd=$(tr "\0" " " < $d/cmdline 2>/dev/null); '
            r'rss=$(awk "/^VmRSS/{print \$2}" $d/status 2>/dev/null); '
            r'sm=$(awk "/^Pss:/{a+=\$2} /^Private_Clean:/{b+=\$2} /^Private_Dirty:/{c+=\$2} '
            r'/^Shared_Clean:/{e+=\$2} /^Shared_Dirty:/{f+=\$2} END{printf \"%d,%d,%d,%d,%d\",a,b,c,e,f}" '
            r'$d/smaps_rollup 2>/dev/null); '
            r'echo "P|$p|$ppid|${rss:-0}|${sm:-0,0,0,0,0}|${ut:-0}|$cmd"; done; '
            r'echo "C|$(cat /sys/fs/cgroup/memory.current)|$(cat /sys/fs/cgroup/memory.peak)|'
            r'$(cat /sys/fs/cgroup/memory.max)"; '
            r'echo "E|$(tr "\n" ";" < /sys/fs/cgroup/memory.events)"; '
            r'awk "/^(anon|file|kernel|sock|shmem) /{printf \"S|%s|%s\n\",\$1,\$2}" /sys/fs/cgroup/memory.stat; '
            r'awk "/usage_usec/{printf \"U|%s\n\",\$2}" /sys/fs/cgroup/cpu.stat')
        try:
            out = subprocess.run(["docker", "exec", self.arguments.container, "sh", "-c", script],
                                 capture_output=True, text=True, timeout=20).stdout
        except Exception:
            return {}, {}, {}, None
        procs, cgroup, stat, cpu_usec = {}, {}, {}, None
        for line in out.splitlines():
            parts = line.split("|")
            if parts[0] == "P" and len(parts) >= 7:
                cmd = "|".join(parts[6:]).strip()
                if not cmd or cmd.startswith("sh -c for d in"):
                    continue
                pss, pclean, pdirty, sclean, sdirty = (int(v) for v in parts[4].split(","))
                pid, ppid = parts[1], parts[2]
                procs[pid] = {"pid": pid, "ppid": ppid, "rss_kb": int(parts[3] or 0),
                              "pss_kb": pss, "private_clean_kb": pclean, "private_dirty_kb": pdirty,
                              "shared_clean_kb": sclean, "shared_dirty_kb": sdirty,
                              "ticks": int(parts[5] or 0), "cmd": cmd,
                              "role": worker_pids.get(pid) or self.get_role(pid, ppid, cmd)}
            elif parts[0] == "C":
                cgroup.update(current=parts[1], peak=parts[2], max=parts[3])
            elif parts[0] == "E":
                cgroup["events"] = parts[1].strip(";")
            elif parts[0] == "S":
                stat[parts[1]] = parts[2]
            elif parts[0] == "U":
                cpu_usec = int(parts[1])
        return procs, cgroup, stat, cpu_usec

    def get_role(self, pid, ppid, cmd):
        if "gnrasgiserve" in cmd:
            return "commander"
        if "template_entry" in cmd:
            return "template" if ppid == "1" else "worker_unknown"
        return "other"

    def sample(self, writer, handle, stop):
        try:
            self.sample_loop(writer, handle, stop)
        except BaseException:
            self.sampler_error = traceback.format_exc()
            self.sampler_failed.set()
            print("!!! CAMPIONATORE MORTO:\n" + self.sampler_error, flush=True)

    def sample_loop(self, writer, handle, stop):
        while not stop.is_set():
            started = time.time()
            with self.lock:
                phase = self.state["phase"]
                rate = self.state["level_rate"]
                scheduled, done = self.state["scheduled"], self.state["done"]
                errors, transport = self.state["errors"], self.state["transport_errors"]
                lat = sorted(self.state["lat"])
                late = sorted(self.state["late"])
                self.state["lat"] = []
                self.state["late"] = []
            census = self.read_census()
            group = ((census or {}).get("groups") or {}).get("pool", {})
            worker_pids, users_per_worker = {}, {}
            for name, entry in (census or {}).get("workers", {}).items():
                if entry.get("pid") is not None:
                    worker_pids[str(entry["pid"])] = name
            for user, worker in group.get("user_worker_map", {}).items():
                users_per_worker[worker] = users_per_worker.get(worker, 0) + 1
            self.worker_of = dict(group.get("user_worker_map", {}))
            procs, cgroup, stat, cpu_usec = self.read_container(worker_pids)
            elapsed = started - (self.previous_stamp or started)
            cpu_total = ""
            if cpu_usec is not None and self.previous_cpu.get("_cgroup") is not None and elapsed > 0:
                cpu_total = round((cpu_usec - self.previous_cpu["_cgroup"]) / 10000.0 / elapsed, 2)
            per_process = {}
            for pid, info in procs.items():
                before = self.previous_cpu.get(pid)
                if before is not None and elapsed > 0:
                    per_process[f'{info["role"]}:{pid}'] = round(
                        (info["ticks"] - before) / 100.0 / elapsed * 100.0, 1)
            self.previous_cpu = {pid: info["ticks"] for pid, info in procs.items()}
            self.previous_cpu["_cgroup"] = cpu_usec
            self.previous_stamp = started
            workers = [i for i in procs.values() if i["role"].startswith("pool_")]
            commander = [i for i in procs.values() if i["role"] == "commander"]
            template = [i for i in procs.values() if i["role"] == "template"]
            row = {
                "ts": time.strftime("%H:%M:%S"), "epoch": round(started, 3),
                "config": self.arguments.config, "phase": phase, "rate_offered": rate,
                "scheduled": scheduled, "done": done, "errors": errors,
                "transport_errors": transport,
                "reqs_per_s": round(len(lat) / SAMPLE_S, 1),
                "p50_ms": self.percentile(lat, 50), "p95_ms": self.percentile(lat, 95),
                "p99_ms": self.percentile(lat, 99),
                "late_p50_s": self.percentile(late, 50, scale=1), "late_max_s": round(late[-1], 4) if late else "",
                "cpu_total_pct": cpu_total, "cpu_per_process": json.dumps(per_process),
                "processes": json.dumps(list(procs.values())),
                "process_count": len(procs),
                "pss_total_kb": sum(i["pss_kb"] for i in procs.values()),
                "rss_total_kb": sum(i["rss_kb"] for i in procs.values()),
                "commander_pss_kb": sum(i["pss_kb"] for i in commander),
                "template_pss_kb": sum(i["pss_kb"] for i in template),
                "workers_pss_kb": sum(i["pss_kb"] for i in workers),
                "worker_pss_each": json.dumps({i["role"]: i["pss_kb"] for i in workers}),
                "worker_rss_each": json.dumps({i["role"]: i["rss_kb"] for i in workers}),
                "cg_current": cgroup.get("current", ""), "cg_peak": cgroup.get("peak", ""),
                "cg_max": cgroup.get("max", ""), "cg_events": cgroup.get("events", ""),
                "st_anon": stat.get("anon", ""), "st_file": stat.get("file", ""),
                "st_kernel": stat.get("kernel", ""), "st_sock": stat.get("sock", ""),
                "st_shmem": stat.get("shmem", ""),
                "users_real": len([u for u in (census or {}).get("user_map", {}) if not u.startswith("guest_")]),
                "users_guest": len([u for u in (census or {}).get("user_map", {}) if u.startswith("guest_")]),
                "connections": len((census or {}).get("connection_user_map", {})),
                "pages": len((census or {}).get("page_connection_map", {})),
                "workers_alive": len(group.get("living_workers", [])),
                "worker_names": "|".join(group.get("living_workers", [])),
                "users_per_worker": json.dumps(users_per_worker),
            }
            if cgroup.get("current") and row["pss_total_kb"]:
                row["cg_minus_pss_kb"] = int(cgroup["current"]) // 1024 - row["pss_total_kb"]
            writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
            self.sampler_rows += 1
            self.sampler_ready.set()
            stop.wait(max(0.0, SAMPLE_S - (time.time() - started)))

    def percentile(self, values, which, scale=1000):
        if not values:
            return ""
        index = max(0, min(len(values) - 1, int(len(values) * which / 100.0) - 1))
        return round(values[index] * (1 if scale == 1000 else 1), 4) if scale == 1 else round(values[index], 2)

    FIELDS = ["ts", "epoch", "config", "phase", "rate_offered", "scheduled", "done",
              "errors", "transport_errors", "reqs_per_s", "p50_ms", "p95_ms", "p99_ms",
              "late_p50_s", "late_max_s", "cpu_total_pct", "cpu_per_process",
              "process_count", "processes", "pss_total_kb", "rss_total_kb",
              "commander_pss_kb", "template_pss_kb", "workers_pss_kb",
              "worker_pss_each", "worker_rss_each",
              "cg_current", "cg_peak", "cg_max", "cg_events", "cg_minus_pss_kb",
              "st_anon", "st_file", "st_kernel", "st_sock", "st_shmem",
              "users_real", "users_guest", "connections", "pages",
              "workers_alive", "worker_names", "users_per_worker"]

    CALL_FIELDS = ["phase", "user", "scheduled_at", "started_at", "completed_at",
                   "lateness_s", "latency_ms", "status", "error", "worker"]

    def check_sampler(self, where):
        if self.sampler_failed.is_set() or not self.sampler_thread.is_alive():
            raise SamplerDown(f"campionatore fermo durante {where}: {self.sampler_error}")
        now = time.time()
        if now - self.sampler_checked_at >= 3 * SAMPLE_S:
            if self.sampler_rows <= self.sampler_seen:
                raise SamplerDown(f"le righe del campionatore non crescono durante {where}")
            self.sampler_seen = self.sampler_rows
            self.sampler_checked_at = now

    def wait_quiet(self, label, seconds):
        with self.lock:
            self.state["phase"] = label
            self.state["level_rate"] = 0.0
        print(f"--- {label} ({seconds:.0f}s, nessuna richiesta) ---", flush=True)
        time.sleep(seconds)
        self.check_sampler(label)

    def check_distribution(self, expected_workers, expected_per_worker):
        """Bloccante: la distribuzione deve coincidere esattamente con l'attesa."""
        census = self.read_census()
        if census is None:
            raise InvalidRun("census non leggibile dopo i login")
        group = census["groups"]["pool"]
        real = [u for u in census["user_map"] if not u.startswith("guest_")]
        counts = {}
        for user, worker in group["user_worker_map"].items():
            if not user.startswith("guest_"):
                counts[worker] = counts.get(worker, 0) + 1
        record = {"expected_workers": expected_workers,
                  "expected_per_worker": expected_per_worker,
                  "workers_alive": group["living_workers"],
                  "users_real": len(real), "counts": counts,
                  "connections": len(census["connection_user_map"]),
                  "pages": len(census["page_connection_map"])}
        self.checkpoints.append(record)
        atteso = sorted([expected_per_worker] * expected_workers, reverse=True)
        ottenuto = sorted(counts.values(), reverse=True)
        record["expected_list"] = atteso
        record["obtained_list"] = ottenuto
        print(f"  distribuzione: worker={group['living_workers']} utenti={len(real)} "
              f"per_worker={counts}", flush=True)
        print(f"  attesa {atteso} | ottenuta {ottenuto}", flush=True)
        problemi = []
        if len(group["living_workers"]) != expected_workers:
            problemi.append(f"worker vivi {len(group['living_workers'])} invece di {expected_workers}")
        if ottenuto != atteso:
            problemi.append(f"distribuzione {ottenuto} invece di {atteso}")
        if len(real) != self.users:
            problemi.append(f"utenti reali {len(real)} invece di {self.users}")
        if sum(counts.values()) != self.users:
            problemi.append(f"utenti collocati {sum(counts.values())} invece di {self.users}")
        non_collocati = [u for u in real if u not in group["user_worker_map"]]
        if non_collocati:
            problemi.append(f"utenti non collocati: {non_collocati}")
        eventi = self.read_journal_events()
        for parola in ("placement_fallback", "cpu_closed_hard_cap_fallback",
                       "restart_worker", "process_quitted"):
            if eventi.get(parola):
                problemi.append(f"{parola} x{eventi[parola]} prima della misura")
        record["problemi"] = problemi
        record["journal_events"] = eventi
        if problemi:
            raise InvalidRun("distribuzione non conforme: " + "; ".join(problemi))
        return record

    def read_journal_events(self):
        """Conteggio dei reason/decision del journal, dall'offset di questa corsa."""
        counts = {}
        try:
            with open(self.arguments.journal) as handle:
                lines = [line for line in handle if line.strip()]
        except Exception:
            return counts
        for line in lines[self.journal_offset:]:
            try:
                row = json.loads(line)
            except Exception:
                continue
            for key in (row.get("reason"), row.get("decision")):
                if key:
                    counts[key] = counts.get(key, 0) + 1
        return counts

    def apply_measured_policy(self):
        """La policy della misura entra in vigore a caldo, senza riavviare nulla."""
        with self.lock:
            self.state["phase"] = "apply"
        prima = self.topology()
        base = self.arguments.base
        richiesta = urllib.request.Request(
            base + "/_orchestration/apply", data=json.dumps(MEASURED_POLICY).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(richiesta, timeout=30) as risposta:
            esito = json.load(risposta)
        print(f"--- apply: {esito['outcome']} generation {esito['generation']} "
              f"changed {json.dumps(esito['changed_settings'])} ---", flush=True)
        offset_eventi = self.read_journal_events()
        with self.lock:
            self.state["phase"] = "apply_wait"
        print(f"--- attesa di {APPLY_WAIT_S:.0f}s senza richieste ---", flush=True)
        time.sleep(APPLY_WAIT_S)
        dopo = self.topology()
        with urllib.request.urlopen(base + "/_orchestration/status", timeout=15) as risposta:
            stato = json.load(risposta)
        vivi = stato["effective_settings"]
        problemi = []
        for chiave, valore in MEASURED_POLICY.items():
            if vivi.get(chiave) != valore:
                problemi.append(f"{chiave} vale {vivi.get(chiave)} invece di {valore}")
        if prima["workers"] != dopo["workers"]:
            problemi.append(f"worker cambiati: {prima['workers']} -> {dopo['workers']}")
        if prima["map"] != dopo["map"]:
            problemi.append("la mappa utenti->worker e' cambiata durante l'apply")
        dopo_eventi = self.read_journal_events()
        for parola in ("close_worker", "restart_worker", "process_quitted",
                       "placement_fallback", "cpu_closed_hard_cap_fallback"):
            delta = dopo_eventi.get(parola, 0) - offset_eventi.get(parola, 0)
            if delta:
                problemi.append(f"{parola} x{delta} durante l'apply")
        aperti = self.admission_state()
        chiusi = [n for n, aperto in aperti.items() if aperto is False]
        if chiusi:
            problemi.append(f"ammissione chiusa su {chiusi}")
        record = {"apply": esito, "policy_viva": vivi, "prima": prima, "dopo": dopo,
                  "admission": aperti, "problemi": problemi}
        self.checkpoints.append({"stage": "apply", **record})
        print(f"  policy viva certificata: {not problemi}; ammissione: {aperti}", flush=True)
        if problemi:
            raise InvalidRun("apply non conforme: " + "; ".join(problemi))

    def topology(self):
        census = self.read_census()
        if census is None:
            raise InvalidRun("census non leggibile")
        group = census["groups"]["pool"]
        return {"workers": sorted(group["living_workers"]),
                "map": dict(group["user_worker_map"])}

    def admission_state(self):
        """cpu_admission_open per worker, dall'ultima scansione del journal."""
        try:
            righe = [line for line in open(self.arguments.journal) if line.strip()]
        except Exception:
            return {}
        for linea in reversed(righe[-60:]):
            riga = json.loads(linea)
            candidati = riga.get("candidates") or []
            if candidati:
                return {c["name"]: c.get("cpu_admission_open") for c in candidati}
        return {}

    def check_watch(self, where):
        if self.topology_broken:
            raise InvalidRun(f"topologia cambiata durante {where}: {self.topology_broken}")

    def watch_topology(self, where):
        """Al primo scostamento la corsa e' strutturalmente non valida."""
        adesso = self.topology()
        if adesso != self.frozen_topology:
            righe = [json.loads(line) for line in open(self.arguments.journal) if line.strip()]
            colpevoli = [r for r in righe[-12:]
                         if r["decision"] in ("start_worker", "placement", "retirement",
                                              "restart_worker", "close_worker")]
            dettaglio = "; ".join(f"{r['decision_id']} {r['decision']} {r['reason']} "
                                  f"-> {r['outcome']} {json.dumps(r.get('numbers', {}))}"
                                  for r in colpevoli)
            raise InvalidRun(f"topologia cambiata durante {where}: "
                             f"{self.frozen_topology['workers']} -> {adesso['workers']}; "
                             f"journal: {dettaglio}")

    def run(self):
        capture = load_capture(SESSION_CAPTURE)
        self.login_calls, self.pages = build_plan(capture)
        accounts = [line.strip() for line in open(USERNAMES_ALL) if line.strip()]
        self.lookups = accounts[:32]
        handle = open(f"{self.arguments.out}_samples.csv", "w", newline="")
        writer = csv.DictWriter(handle, fieldnames=self.FIELDS, extrasaction="ignore")
        writer.writeheader()
        calls_handle = open(f"{self.arguments.out}_calls.csv", "w", newline="")
        self.calls_handle = calls_handle
        self.calls_writer = csv.DictWriter(calls_handle, fieldnames=self.CALL_FIELDS, extrasaction="ignore")
        self.calls_writer.writeheader()
        stop = threading.Event()
        self.sampler_thread = threading.Thread(target=self.sample, args=(writer, handle, stop), daemon=True)
        self.sampler_thread.start()
        if not self.sampler_ready.wait(timeout=15.0):
            stop.set()
            handle.close()
            raise SamplerDown(f"nessuna riga campionata: {self.sampler_error}")
        self.sampler_checked_at = time.time()
        try:
            self.journal_offset = sum(1 for line in open(self.arguments.journal) if line.strip())
        except Exception:
            self.journal_offset = 0
        print(f"  campionatore attivo; offset journal {self.journal_offset} righe", flush=True)
        saturated_at = None
        try:
            self.wait_quiet("baseline", BASELINE_S)
            self.log_in_all(accounts)
            self.check_distribution(self.arguments.expect_workers, self.arguments.expect_per_worker)
            self.apply_measured_policy()
            self.frozen_topology = self.topology()
            print(f"  topologia congelata: {self.frozen_topology['workers']}", flush=True)
            try:
                self.journal_offset = sum(1 for line in open(self.arguments.journal) if line.strip())
            except Exception:
                self.journal_offset = 0
            print(f"  offset journal della misura: {self.journal_offset} righe", flush=True)
            self.play_window("warmup")
            for level in ("L40", "L80", "L120"):
                self.play_window(f"stabilize_{level}")
                self.play_window(f"measure_{level}")
            self.check_distribution(self.arguments.expect_workers, self.arguments.expect_per_worker)
        except Saturated as failure:
            saturated_at = str(failure)
            print(f"!!! SATURAZIONE: {failure} — chiusura sicura", flush=True)
        self.log_out_all()
        self.wait_quiet("observe", OBSERVE_S)
        for runner in self.runners.values():
            runner.inbox.put(None)
        stop.set()
        self.sampler_thread.join(timeout=10)
        handle.close()
        calls_handle.close()
        for name, payload in (("logouts", self.logouts), ("checkpoints", self.checkpoints),
                              ("windows", self.windows),
                              ("population_log", self.population_log),
                              ("journal_events", self.read_journal_events()),
                              ("saturation", {"saturated_at": saturated_at})):
            with open(f"{self.arguments.out}_{name}.json", "w") as out:
                json.dump(payload, out, indent=2)
        print(f"FINE: schedulate={self.state['scheduled']} eseguite={self.state['done']} "
              f"errori={self.state['errors']} trasporto={self.state['transport_errors']}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--census", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--expect-workers", type=int, required=True)
    parser.add_argument("--expect-per-worker", type=int, required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--users", type=int, default=USERS,
                        help="quanti utenti popolare; il default e' la prova vera")
    probe = Probe(parser.parse_args())
    try:
        probe.run()
    except SamplerDown as failure:
        print(f"PROVA NON VALIDA (campionatore): {failure}", flush=True)
        sys.exit(2)
    except InvalidRun as failure:
        print(f"PROVA NON VALIDA (struttura): {failure}", flush=True)
        try:
            probe.log_out_all()
        except Exception:
            pass
        sys.exit(3)


main()
