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
enter a bucket: the guard is fed by the load engine's completed calls, and the
driver's own traffic — logins, logouts, the census, the page-class-cache
certification, the orchestration reads — never passes through there. A guard that
watched the census would be watching the instrument.

ONE SECOND, ONE BUCKET, AND THE BUCKETS DO NOT OVERLAP. Each completed call is
filed under the whole second in which it completed. A bucket is judged once, when
it can no longer receive calls, and then thrown away. Fifteen consecutive bad
buckets are therefore fifteen consecutive BAD SECONDS — no more and no less.

This replaced a rolling ten-second window, which was wrong for this decision and
measurably so: one slow second stayed inside a rolling window for ten
evaluations, so about five seconds of real slowness already produced fifteen
consecutive breaches. "Fifteen evaluations" and "fifteen seconds" were not the
same thing. With non-overlapping buckets they are.

THE CONDITION, part by part:

- the p95 of the calls filed in ONE whole second;
- strictly above the limit. Equal is not a breach;
- a bucket that is not strictly above the limit RESETS the count to zero;
- a bucket holding fewer than the minimum number of samples also resets it: a p95
  over three requests is noise, and a second with no traffic at all — the
  observation phase, the gap between windows — must not carry a verdict forward;
- every whole second is judged, including the ones with no calls, so a gap breaks
  a sequence instead of being skipped over.

The event is written the moment it fires, before anything else is done with it: a
run killed one second later must still leave the fact on disk.
"""

import json
import threading
import time


class AdmissionGuard(threading.Thread):
    """One bucket per second, and the door it closes once."""

    def __init__(self, settings, event_path, context_source, clock=time.time):
        super().__init__(daemon=True, name="admission_guard")
        self.limit_ms = float(settings["p95_limit_ms"])
        self.needed = int(settings["consecutive_buckets"])
        self.minimum_samples = int(settings["minimum_samples"])
        self.event_path = event_path
        self.context_source = context_source
        self.clock = clock
        self.lock = threading.Lock()
        self.buckets = {}
        self.next_bucket = None
        self.consecutive = 0
        self.judged = 0
        self.bad = 0
        self.thin = 0
        self.peak_consecutive = 0
        self.closed = threading.Event()
        self.finished = threading.Event()
        self.event = None
        self.history = []

    # ------------------------------------------------------------------ ingresso
    def record_latency(self, completed_at, latency_ms):
        """One completed application call, filed under its whole second."""
        index = int(completed_at)
        with self.lock:
            self.buckets.setdefault(index, []).append(latency_ms)

    @property
    def admission_open(self):
        """Whether a new login or a return may still happen."""
        return not self.closed.is_set()

    # ------------------------------------------------------------------ giudizio
    def percentile(self, values, which):
        """The same index rule the load engine uses, so the two agree."""
        if not values:
            return None
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, int(len(ordered) * which / 100.0) - 1))
        return round(ordered[index], 4)

    def take_bucket(self, index):
        """The latencies of one whole second, removed from the store."""
        with self.lock:
            return self.buckets.pop(index, [])

    def judge_bucket(self, index):
        """One second judged. Returns its record; closes the door if it is time."""
        latencies = self.take_bucket(index)
        self.judged += 1
        record = {"bucket": index, "samples": len(latencies),
                  "p95_ms": self.percentile(latencies, 95)}
        if len(latencies) < self.minimum_samples:
            # Un secondo con pochi campioni non porta un verdetto: azzera.
            self.thin += 1
            self.consecutive = 0
            record.update(verdict="pochi campioni", consecutive=0)
        elif record["p95_ms"] > self.limit_ms:
            self.bad += 1
            self.consecutive += 1
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

    def judge_closed_buckets(self, now):
        """Judge every whole second that can no longer receive a call.

        A call completing at 12.9 is filed under 12, so at 13.0 the bucket 12 is
        closed: nothing can still land in it. Every index is judged in order,
        including the ones nobody wrote to, because a second without traffic has
        to break a sequence rather than be stepped over.
        """
        last_closed = int(now) - 1
        if self.next_bucket is None:
            self.next_bucket = last_closed + 1
            return []
        judged = []
        while self.next_bucket <= last_closed:
            judged.append(self.judge_bucket(self.next_bucket))
            self.next_bucket += 1
        return judged

    def close_admission(self, record, latencies):
        """The door closes once, the event is written at once, and it never reopens."""
        context = self.context_source() or {}
        self.event = {
            "event": "ADMISSION_STOP",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "bucket": record["bucket"],
            "reason": (f"p95 > {self.limit_ms:.0f} ms per {self.needed} secondi "
                       f"consecutivi, bucket non sovrapposti di un secondo"),
            "p95_limit_ms": self.limit_ms,
            "consecutive_buckets": self.consecutive,
            "samples_in_bucket": len(latencies),
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
        """One pass a second: judge whatever seconds have closed since the last."""
        while not self.finished.is_set():
            started = self.clock()
            self.judge_closed_buckets(started)
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
            "consecutive_buckets_needed": self.needed,
            "bucket_seconds": 1.0,
            "minimum_samples": self.minimum_samples,
            "buckets_judged": self.judged,
            "buckets_over_limit": self.bad,
            "buckets_too_thin": self.thin,
            "longest_consecutive_run": self.peak_consecutive,
            "history": self.history[-900:],
        }
