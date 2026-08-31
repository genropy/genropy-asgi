# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""The latency guard: it stops the GROWTH of the population, and nothing else.

It is deliberately NOT the memory guard, and the difference is the whole point:

- ``MEMORY_STOP``, an OOM, or a lost container identity are FULL stops. They
  raise the run's stop flag, every phase ends, and the measure is over.
- ``ADMISSION_STOP`` is a partial stop. No user is disturbed: those already
  active keep calling to the end of the run. Only the door closes — no further
  login, no further return — and it stays closed for the rest of the execution
  even if the latency recovers. The measure then continues at the population
  that was reached.

Reporting them as one number would make an overloaded stack indistinguishable
from a stack that ran out of memory, so the two never share a counter, a file or
a flag.

WHAT IS OBSERVED, and what is excluded on purpose. Only real application calls
enter the window: the guard is fed by the load engine's completed calls, and the
driver's own traffic — logins, logouts, the census, the page-class-cache
certification, the orchestration reads — never passes through there. A guard that
watched the census would be watching the instrument.

THE CONDITION, and why each part of it exists:

- the p95 of the calls COMPLETED in the last ten seconds, recomputed once a
  second. A rolling window, not a cumulative one: a cumulative p95 keeps a memory
  of the whole run and would take minutes to react.
- strictly above the limit. Equal is not a breach.
- for fifteen consecutive evaluations. One spike does not close the door, and
  neither does a shorter sequence: the counter resets on the first evaluation that
  comes back inside the limit.
- with at least a minimum number of samples in the window. Under the planned load
  ten seconds hold hundreds of calls, so the minimum only matters at the very
  beginning of a ramp, where a p95 over three requests would be noise. An
  evaluation without enough samples is neither a breach nor a reset: the counter
  is left where it was, because the window simply was not readable.

HOW MANY SECONDS OF SLOWNESS THAT REALLY IS, measured on the guard itself and not
inferred: the window is mobile, so one slow second keeps breaching for the ten
evaluations it stays inside it. Fifteen consecutive breaches therefore need about
FIVE consecutive slow seconds, not fifteen. Three slow seconds produce twelve
breaches and the door stays open; one slow second produces ten. Reading "fifteen
evaluations" as "fifteen seconds of bad latency" would overstate what the guard
tolerates, so the figure is written here rather than left to be derived.

The event is written the moment it fires, before anything else is done with it: a
run killed one second later must still leave the fact on disk.
"""

import json
import threading
import time


class AdmissionGuard(threading.Thread):
    """The rolling p95 of application calls, and the door it closes once."""

    def __init__(self, settings, event_path, context_source, clock=time.time):
        super().__init__(daemon=True, name="admission_guard")
        self.limit_ms = float(settings["p95_limit_ms"])
        self.needed = int(settings["consecutive_evaluations"])
        self.window_seconds = float(settings["window_seconds"])
        self.minimum_samples = int(settings["minimum_samples"])
        self.event_path = event_path
        self.context_source = context_source
        self.clock = clock
        self.lock = threading.Lock()
        self.samples = []
        self.consecutive = 0
        self.evaluations = 0
        self.breaches = 0
        self.unreadable = 0
        self.peak_consecutive = 0
        self.closed = threading.Event()
        self.finished = threading.Event()
        self.event = None
        self.history = []

    # ------------------------------------------------------------------ ingresso
    def record_latency(self, completed_at, latency_ms):
        """One completed application call. Called from every user thread."""
        with self.lock:
            self.samples.append((completed_at, latency_ms))

    @property
    def admission_open(self):
        """Whether a new login or a return may still happen."""
        return not self.closed.is_set()

    # ------------------------------------------------------------------ giudizio
    def window_latencies(self, now):
        """The latencies completed inside the window, sorted. Older ones dropped."""
        floor = now - self.window_seconds
        with self.lock:
            self.samples = [pair for pair in self.samples if pair[0] >= floor]
            return sorted(latency for _, latency in self.samples)

    def percentile(self, values, which):
        """The same index rule the load engine uses, so the two agree."""
        if not values:
            return None
        index = max(0, min(len(values) - 1, int(len(values) * which / 100.0) - 1))
        return round(values[index], 4)

    def evaluate(self, now=None):
        """One evaluation. Returns the record of what was decided.

        The door is closed here and nowhere else, so a test can drive the guard
        one evaluation at a time without a thread and without a clock.
        """
        now = self.clock() if now is None else now
        latencies = self.window_latencies(now)
        self.evaluations += 1
        record = {"at": round(now, 3), "samples": len(latencies),
                  "p95_ms": self.percentile(latencies, 95)}
        if len(latencies) < self.minimum_samples:
            # Finestra non leggibile: non e' una violazione e non azzera il
            # conteggio. Lasciarlo dov'e' e' l'unica lettura onesta.
            self.unreadable += 1
            record.update(verdict="illeggibile", consecutive=self.consecutive)
            self.history.append(record)
            return record
        if record["p95_ms"] > self.limit_ms:
            self.consecutive += 1
            self.breaches += 1
            self.peak_consecutive = max(self.peak_consecutive, self.consecutive)
            record.update(verdict="oltre", consecutive=self.consecutive)
            if self.consecutive >= self.needed and not self.closed.is_set():
                record["fired"] = True
                self.close_admission(record, latencies)
        else:
            self.consecutive = 0
            record.update(verdict="dentro", consecutive=0)
        self.history.append(record)
        return record

    def close_admission(self, record, latencies):
        """The door closes once, the event is written at once, and it never reopens."""
        context = self.context_source() or {}
        self.event = {
            "event": "ADMISSION_STOP",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "epoch": record["at"],
            "reason": (f"p95 mobile > {self.limit_ms:.0f} ms per "
                       f"{self.needed} valutazioni consecutive"),
            "window_seconds": self.window_seconds,
            "p95_limit_ms": self.limit_ms,
            "consecutive_evaluations": self.consecutive,
            "samples_in_window": len(latencies),
            "p50_ms": self.percentile(latencies, 50),
            "p95_ms": self.percentile(latencies, 95),
            "p99_ms": self.percentile(latencies, 99),
            **context,
        }
        self.write()
        self.closed.set()
        print(f"!!! ADMISSION_STOP: {self.event['reason']} | fase "
              f"{self.event.get('phase')} | autenticati "
              f"{self.event.get('population_authenticated')} attivi "
              f"{self.event.get('population_active')} | p95 "
              f"{self.event['p95_ms']} ms | completate "
              f"{self.event.get('completed')} pendenti {self.event.get('pending')}",
              flush=True)

    # ------------------------------------------------------------------ il ciclo
    def run(self):
        """One evaluation a second, until the driver says it is done."""
        while not self.finished.is_set():
            started = self.clock()
            self.evaluate(started)
            self.finished.wait(max(0.0, 1.0 - (self.clock() - started)))

    def write(self):
        """The verdict on disk. Written when it fires, and again at the end."""
        with open(self.event_path, "w") as handle:
            json.dump(self.verdict, handle, indent=2)

    @property
    def verdict(self):
        """Everything the guard has to say, event or no event."""
        return {
            "admission_stop": self.closed.is_set(),
            "event": self.event,
            "p95_limit_ms": self.limit_ms,
            "consecutive_needed": self.needed,
            "window_seconds": self.window_seconds,
            "minimum_samples": self.minimum_samples,
            "evaluations": self.evaluations,
            "evaluations_over_limit": self.breaches,
            "evaluations_unreadable": self.unreadable,
            "longest_consecutive_run": self.peak_consecutive,
            "history": self.history[-600:],
        }
