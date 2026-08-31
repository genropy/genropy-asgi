# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""The load generator, identical for both stacks: a global pace, one thread per user.

The trace declares WHEN each request is offered; the engine offers it at that
instant and never waits for the answer before offering the next. Each user has
its own session, its own connection and its own queue, so requests of different
users overlap while the same user never has two in flight — which is what a
browser does.

Nothing here knows whether it is talking to the bridge or to Gunicorn. The
stack-specific observations — census, journal, topology — live in the driver that
owns this engine, and the mandate is explicit that they must not change the load
sent. That is why they are absent from this file.

THREE COUNTS, THREE INSTANTS. The previous scenario reported ``started`` and
``completed`` from the same list, so the two numbers were equal by construction
and a request that went on the wire but never came back was invisible. Here:

- OFFERED is a row of the trace whose instant has arrived;
- STARTED is a request handed to the socket;
- COMPLETED is a response fully read.

THREE KINDS OF ERROR, KEPT APART. An HTTP status that is not 200; an application
error inside a 200 body; and a transport exception. The old driver merged the
three into one counter and a retry made them ambiguous.
"""

import http.client
import json
import queue
import statistics
import threading
import time

# Il marcatore che GenroPy mette in una risposta 200 quando la chiamata e' fallita
# dentro l'applicazione. E' la stessa stringa che churn_driver.send_call osserva.
APP_ERROR_MARKER = b"<error>"


class UserRunner(threading.Thread):
    """One user: its queue, its connection, its requests in order."""

    def __init__(self, engine, label, logged_user):
        super().__init__(daemon=True, name=label)
        self.engine = engine
        self.label = label
        self.user = logged_user
        self.inbox = queue.Queue()
        self.counter = 0

    @property
    def pending(self):
        return self.inbox.qsize()

    def run(self):
        while True:
            item = self.inbox.get()
            if item is None:
                return
            scheduled_at, phase = item
            self.execute(scheduled_at, phase)

    def execute(self, scheduled_at, phase):
        """One request, timed at three instants and classified once."""
        started_at = time.time()
        self.engine.count_started()
        lookup = self.engine.lookups[self.counter % len(self.engine.lookups)]
        body = self.engine.body_for(self.user, lookup, self.counter + 100)
        status, app_error, transport_error = self.send(body)
        completed_at = time.time()
        self.counter += 1
        self.engine.record_call({
            "phase": phase, "user": self.label,
            "scheduled_at": round(scheduled_at, 6),
            "started_at": round(started_at, 6),
            "completed_at": round(completed_at, 6),
            "lateness_s": round(started_at - scheduled_at, 6),
            "latency_ms": round((completed_at - started_at) * 1000, 3),
            "status": status if status is not None else "",
            "app_error": app_error or "",
            "transport_error": transport_error or "",
            "worker": self.engine.worker_of.get(self.label, ""),
        })

    def send(self, body):
        """Returns (status, app_error, transport_error).

        One retry, and ONLY on a transport exception: a keep-alive the server
        closed while the user was thinking is not a failure of the stack, and a
        first run of the churn driver against Gunicorn — 2 s idle window, users
        calling every 3 s — read half its calls as failures for want of this.
        A status is never retried: retrying it would hide it.
        """
        for attempt in (1, 2):
            try:
                self.user.connection.request("POST", "/", body=body, headers=self.user.headers)
                answer = self.user.connection.getresponse()
                payload = answer.read()
                if answer.status != 200:
                    return answer.status, None, None
                if APP_ERROR_MARKER in payload:
                    return answer.status, "application error", None
                return answer.status, None, None
            except Exception as failure:                         # noqa: BLE001
                self.reopen()
                if attempt == 2:
                    return None, None, repr(failure)[:120]
        return None, None, "unreachable"

    def reopen(self):
        """A fresh connection for this user, the way the churn driver does it."""
        try:
            self.user.connection.close()
        except Exception:                                        # noqa: BLE001, S110
            pass
        self.user.connection = http.client.HTTPConnection(self.user.netloc, timeout=30)


class LoadEngine:
    """The pace, the counters, the windows. One instance per run, per stack."""

    CALL_FIELDS = ["phase", "user", "scheduled_at", "started_at", "completed_at",
                   "lateness_s", "latency_ms", "status", "app_error", "transport_error",
                   "worker"]

    def __init__(self, trace, body_for, lookups, stop_flag, calls_writer=None,
                 calls_handle=None):
        self.trace = trace
        self.body_for = body_for
        self.lookups = lookups
        self.stop_flag = stop_flag
        self.calls_writer = calls_writer
        self.calls_handle = calls_handle
        self.lock = threading.Lock()
        self.runners = {}
        self.worker_of = {}
        self.windows = []
        self.window_calls = []
        self.state = {"phase": "start", "level_rate": 0.0, "offered": 0, "started": 0,
                      "completed": 0, "errors_http": 0, "errors_app": 0,
                      "errors_transport": 0, "lat": [], "late": []}

    # ------------------------------------------------------------------ conteggi
    def count_offered(self):
        with self.lock:
            self.state["offered"] += 1

    def count_started(self):
        with self.lock:
            self.state["started"] += 1

    def record_call(self, row):
        with self.lock:
            self.state["completed"] += 1
            self.state["lat"].append(row["latency_ms"])
            self.state["late"].append(row["lateness_s"])
            if row["transport_error"]:
                self.state["errors_transport"] += 1
            elif row["app_error"]:
                self.state["errors_app"] += 1
            elif row["status"] != 200:
                self.state["errors_http"] += 1
            self.window_calls.append({"phase": row["phase"], "lateness_s": row["lateness_s"],
                                      "latency_ms": row["latency_ms"], "status": row["status"],
                                      "app_error": row["app_error"],
                                      "transport_error": row["transport_error"]})
        if self.calls_writer is not None and self.calls_handle is not None:
            self.calls_writer.writerow(row)
            self.calls_handle.flush()

    def take_interval(self):
        """The latencies since the last call, and reset. For the sampler."""
        with self.lock:
            lat = sorted(self.state["lat"])
            late = sorted(self.state["late"])
            self.state["lat"] = []
            self.state["late"] = []
            snapshot = {key: self.state[key] for key in
                        ("phase", "level_rate", "offered", "started", "completed",
                         "errors_http", "errors_app", "errors_transport")}
        return lat, late, snapshot

    @property
    def pending(self):
        return sum(runner.pending for runner in self.runners.values())

    # ------------------------------------------------------------------ finestre
    def play_window(self, phase):
        """Offer the rows of this window at the pace they declare.

        A stop request ends the window immediately: the rows not yet offered are
        recorded as such, so a truncated window is visible as truncated.
        """
        rows = [row for row in self.trace if row["phase"] == phase]
        if not rows:
            print(f"--- {phase}: assente nella traccia, saltata ---", flush=True)
            return None
        rate = rows[0]["rate"]
        with self.lock:
            self.state["phase"] = phase
            self.state["level_rate"] = rate
        print(f"--- {phase}: {len(rows)} richieste a {rate:.0f}/s ---", flush=True)
        self.stop_flag.raise_if_stopped(f"inizio {phase}")
        started = time.time()
        offered = 0
        for row in rows:
            if self.stop_flag.stopped:
                print(f"--- {phase}: interrotta a {offered}/{len(rows)} richieste ---", flush=True)
                break
            target = started + row["t_rel"]
            now = time.time()
            if target > now:
                if self.stop_flag.event.wait(target - now):
                    break
            self.count_offered()
            offered += 1
            self.runners[row["user"]].inbox.put((target, phase))
        self.drain()
        return self.close_window(phase, rows, offered, started)

    def drain(self, timeout=120.0):
        """Wait for the queues to empty: nothing in flight when a window closes."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.pending == 0:
                time.sleep(1.0)
                if self.pending == 0:
                    return True
            time.sleep(0.2)
        return False

    def close_window(self, phase, rows, offered, started):
        """The window's numbers. Judgement about the generator is NOT made here.

        A growing start lateness means the stack is not absorbing the offered
        rate. Whether that is the generator's limit or the stack's cannot be
        decided from these numbers alone — it needs the generator's own CPU and
        the host's — so this method reports and does not rule. The old scenario
        printed "generatore NON VALIDO" on drift alone and was wrong about it.
        """
        with self.lock:
            calls = [c for c in self.window_calls if c["phase"] == phase]
            self.window_calls = [c for c in self.window_calls if c["phase"] != phase]
        seconds = max(rows[-1]["t_rel"] + 1.0 / rows[0]["rate"], 1e-9)
        wall = max(time.time() - started, 1e-9)
        late = sorted(c["lateness_s"] for c in calls)
        latency = sorted(c["latency_ms"] for c in calls)
        half = len(late) // 2
        first = statistics.median(late[:half]) if half else 0.0
        second = statistics.median(late[half:]) if half else 0.0
        record = {
            "phase": phase, "declared_seconds": round(seconds, 2), "wall_seconds": round(wall, 2),
            "offered": offered, "offered_declared": len(rows),
            "completed": len(calls),
            "offered_per_s": round(offered / seconds, 2),
            "completed_per_s": round(len(calls) / seconds, 2),
            "completed_ratio": round(len(calls) / len(rows), 4) if rows else None,
            "p50_ms": self.percentile(latency, 50), "p95_ms": self.percentile(latency, 95),
            "p99_ms": self.percentile(latency, 99),
            "late_p50_s": self.percentile(late, 50), "late_p95_s": self.percentile(late, 95),
            "late_max_s": round(late[-1], 4) if late else None,
            "late_first_half_s": round(first, 4), "late_second_half_s": round(second, 4),
            "late_drift_s": round(second - first, 4),
            "pending_at_end": self.pending,
            "errors_http": len([c for c in calls if not c["transport_error"]
                                and not c["app_error"] and c["status"] != 200]),
            "errors_app": len([c for c in calls if c["app_error"]]),
            "errors_transport": len([c for c in calls if c["transport_error"]]),
            "truncated_by_stop": offered < len(rows),
        }
        self.windows.append(record)
        print(f"  [{phase}] offerte {record['offered']}/{record['offered_declared']} "
              f"completate {record['completed']} ({(record['completed_ratio'] or 0) * 100:.1f}%) "
              f"p50 {record['p50_ms']} ms p95 {record['p95_ms']} ms | "
              f"lateness p50 {record['late_p50_s']}s deriva {record['late_drift_s']:+.3f}s | "
              f"errori http {record['errors_http']} app {record['errors_app']} "
              f"trasporto {record['errors_transport']} | pendenti {record['pending_at_end']}",
              flush=True)
        return record

    def percentile(self, values, which):
        if not values:
            return None
        index = max(0, min(len(values) - 1, int(len(values) * which / 100.0) - 1))
        return round(values[index], 4)

    # ------------------------------------------------------------------ chiusura
    def stop_runners(self):
        """Every user thread told to end, then waited for."""
        for runner in self.runners.values():
            runner.inbox.put(None)
        for runner in self.runners.values():
            runner.join(timeout=10)
        alive = [runner.name for runner in self.runners.values() if runner.is_alive()]
        return alive

    def write_windows(self, path):
        with open(path, "w") as handle:
            json.dump(self.windows, handle, indent=2)
