"""Prova 3: a large authenticated population, a small working set.

The question: can the bridge hold two thousand authenticated users while
keeping in its workers only the recent working set, freezing the rest, and
giving back processes and memory? And what does the same population cost the
legacy stack, which has no freeze at all?

Why this is one process and not six. The phases share the users: the same
cookie jar that logged somebody in must still be there when he is woken hours
later, because the whole point is that NO NEW LOGIN is needed after a thaw.
Split across processes, the state would have to be serialised and the run
would prove nothing about the sessions it did not keep.

The phases, in order, each measured before the next begins:

- **populate** — users arrive in batches; each one logs in and does a real
  piece of work, so a store, a connection and a page exist for him. Memory,
  processes and the census are read after every batch, so the cost per user is
  measured as it accrues instead of being divided at the end.
- **rest** — everybody stops. Nobody logs out. The run waits for the freeze
  valve to park the idle users and for the pool to retire the workers it no
  longer needs, sampling until the population stops moving.
- **wake** — a DETERMINISTIC subset comes back, slowly, spread over minutes so
  the thaws never arrive as a burst. The latency of each user's first call
  after the thaw is recorded separately: that is what a returning operator
  actually feels.
- **work** — the woken users work with pauses between operations.
- **rotate** — some stop and an equal number of frozen users take their place,
  holding the active set roughly constant while the identities behind it
  change. This is the phase where freeze and thaw happen continuously.
- **rest2** — everybody stops again. The run checks whether memory and workers
  come back near the first minimum, which is what says there is no leak.

The silence is real: a user who stops does not ping either. The timestamps the
freeze reads are only stamped when a client reports them, so a ping that
carries none does not keep anybody awake — but a user that keeps pinging is
still traffic, and this run is about a population that has genuinely gone
quiet.

Only the bridge has a census and a freezer. Against the legacy stack the same
phases run and the same client-side numbers are recorded; the pool columns are
simply empty, and the comparison is made on memory, processes and latency.
"""

import argparse
import json
import os
import random
import statistics
import sys
import threading
import time
import urllib.parse
import urllib.request

BENCH = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BENCH)

from session_bench import (  # noqa: E402
    EmulatedUser,
    SecondSampler,
    SessionBench,
    StickyClient,
)

ACCOUNTS = "load_users.txt"


class Census:
    """The pool's own account of the population, or nothing for the legacy."""

    def __init__(self, url):
        self.url = url

    @property
    def snapshot(self):
        """Authenticated, placed and frozen users as the commander sees them."""
        if not self.url:
            return {}
        try:
            with urllib.request.urlopen(self.url, timeout=5) as answer:
                census = json.load(answer)
        except Exception:
            return {}
        front = next(iter(census.values()))
        user_map = front.get("user_map", {})
        groups = front.get("groups", {})
        placed, unplaced = 0, 0
        for group in groups.values():
            for user, worker in group.get("user_worker_map", {}).items():
                if user.startswith("guest_"):
                    continue
                if worker:
                    placed += 1
                else:
                    unplaced += 1
        named = [user for user in user_map if not user.startswith("guest_")]
        frozen = [user for user in named if user_map[user].get("frozen")]
        workers = front.get("workers", {})
        return {
            "authenticated": len(named),
            "placed": placed,
            "unplaced": unplaced,
            "frozen": len(frozen),
            "workers": len(workers),
            "worker_pids": sorted(
                worker.get("pid") for worker in workers.values() if isinstance(worker, dict)),
        }


class ResidentUser(EmulatedUser):
    """A logged-in user who works only when told, and is otherwise silent.

    The parent plays a tour and leaves. This one stays: it logs in, then waits
    to be asked for an operation — one pass of the body — and goes quiet again.

    **The ping keeps running, deliberately.** This user represents a browser
    left open: the site's own client keeps pinging, and its pings report the
    two activity clocks WITHOUT moving them. So the pings continue and the
    user still becomes freezable, because what ages is the clock and not the
    connection. (An earlier version of this class claimed not to start the
    pinger, which was false — ``play(head_rows)`` starts it through
    ``learn_frame`` — and would have measured a browser that had been closed.)

    **Operations are counted, not merely flagged.** ``requested`` rises when
    somebody asks for an operation, ``started`` when one actually begins, and
    ``completed`` when it ends. A request arriving while an operation is still
    in flight is counted as COALESCED rather than silently lost, which is what
    a bare Event would have done.

    **The first call back is timed on the call, not on the operation.** What a
    returning operator feels is the reply to his first request — the one that
    causes or crosses the thaw — not the whole burst that follows it.
    """

    def __init__(self, bench, number, username):
        super().__init__(bench, number, username)
        self.work_event = threading.Event()
        self.in_flight = False
        self.requested = 0
        self.started = 0
        self.completed = 0
        self.coalesced = 0
        self.operations = 0
        self.first_call_after_wake_ms = None
        self.operation_after_wake_ms = None
        self.awaiting_first_call = False
        self.ready = threading.Event()
        self.failed = False

    def run(self):
        """Log in once, then work only when asked, until told to leave."""
        self.client = StickyClient(self.bench.host, self.bench.port)
        self.cursor = time.time()
        try:
            self.play(self.bench.head_rows, 0)
            self.ready.set()
            while not self.exit_event.is_set():
                if not self.work_event.wait(0.5):
                    continue
                self.work_event.clear()
                if self.exit_event.is_set():
                    break
                self.play_one_operation()
            self.cursor = time.time()
            self.play(self.bench.tail_rows, 0)
        except Exception:
            self.failed = True
            self.ready.set()
        finally:
            self.ping_stop.set()
            self.bench.leave(self)

    def play_one_operation(self):
        """One pass of the recorded body, counted from start to finish."""
        self.in_flight = True
        self.started += 1
        began = time.time()
        try:
            self.play(self.bench.body_rows, self.operations + 1)
        finally:
            elapsed_ms = round((time.time() - began) * 1000, 1)
            if self.awaiting_first_call:
                # the burst finished without any call being timed (an empty
                # body would do it): record the operation, leave the call empty
                self.awaiting_first_call = False
            if self.operation_after_wake_ms == "pending":
                self.operation_after_wake_ms = elapsed_ms
            self.in_flight = False
            self.operations += 1
            self.completed += 1

    def replay_call(self, record, round_number, sequence, late_ms):
        """Every call of the tour, timing the FIRST one after a thaw."""
        if not self.awaiting_first_call:
            return super().replay_call(record, round_number, sequence, late_ms)
        began = time.time()
        try:
            return super().replay_call(record, round_number, sequence, late_ms)
        finally:
            self.first_call_after_wake_ms = round((time.time() - began) * 1000, 1)
            self.awaiting_first_call = False

    def work_once(self, after_wake=False):
        """Ask for one operation. ``after_wake`` times the first call back.

        Returns:
            True when the request was taken, False when it was coalesced into
            an operation already in flight.
        """
        self.requested += 1
        if self.in_flight:
            self.coalesced += 1
            return False
        if after_wake:
            self.awaiting_first_call = True
            self.operation_after_wake_ms = "pending"
        self.work_event.set()
        return True


class PopulationRun(SessionBench):
    """The whole Prova 3: the phases, in order, each one measured."""

    #: The five populations are never one number. Kept apart on every row:
    #: how many the driver holds logged in, how many the pool still places,
    #: how many it has parked, how many the working set NOMINALLY contains,
    #: and how many operations are genuinely in flight at this instant.
    PHASE_COLUMNS = ("wall,elapsed_s,phase,note,"
                     "resident_users,working_set_nominal,in_flight,"
                     "authenticated,placed,unplaced,frozen,workers,worker_pids,"
                     "ops_requested,ops_started,ops_completed,ops_coalesced")

    def __init__(self, arguments):
        super().__init__(arguments)
        self.census = Census(arguments.census)
        self.residents = []
        self.rng = random.Random(arguments.seed)
        self.phase_log = open(f"{arguments.out}_phases.csv", "w")
        self.phase_log.write(self.PHASE_COLUMNS + "\n")
        self._working_set_size = 0
        self.wake_latencies = []
        self.wake_operation_ms = []

    def write_summary(self):
        """One file with how the leg ended and the populations it reached."""
        stop = self.memory_stop
        snapshot = self.census.snapshot
        requested, started, completed, coalesced = self.operation_counts
        with open(f"{self.arguments.out}_summary.txt", "w") as handle:
            if stop:
                handle.write(f"ARRESTO DI SICUREZZA (memoria)\n{stop}\n"
                             f"{len(self.residents)} residenti su "
                             f"{self.arguments.count}: massimo osservato, non un limite\n")
            else:
                handle.write(f"COMPLETATA: {len(self.residents)} residenti su "
                             f"{self.arguments.count}\n")
            handle.write(f"autenticati {snapshot.get('authenticated', 'n/d')}, "
                         f"collocati {snapshot.get('placed', 'n/d')}, "
                         f"congelati {snapshot.get('frozen', 'n/d')}, "
                         f"working set {self.working_set_nominal}\n")
            handle.write(f"operazioni richieste/iniziate/completate/coalescenti: "
                         f"{requested}/{started}/{completed}/{coalesced}\n")
            for label, values in (("prima chiamata dopo il thaw", self.wake_latencies),
                                  ("raffica intera dopo il thaw", self.wake_operation_ms)):
                if values:
                    ordered = sorted(values)
                    handle.write(f"{label}: n={len(ordered)} "
                                 f"p50={self.percentile_of(ordered, 50)} ms "
                                 f"p95={self.percentile_of(ordered, 95)} ms\n")
        print(open(f"{self.arguments.out}_summary.txt").read(), flush=True)

    @property
    def in_flight_users(self):
        """Residents with an operation genuinely running right now.

        Read from the users' own ``in_flight`` state, never from the request
        flag: that flag is cleared the instant the operation begins, so
        counting it would report almost nobody working at any time.
        """
        return sum(1 for user in self.residents if user.in_flight)

    @property
    def working_set_nominal(self):
        """How many users the plan currently intends to keep working."""
        return self._working_set_size

    @property
    def operation_counts(self):
        """Requested, started, completed and coalesced, over all residents."""
        return (
            sum(user.requested for user in self.residents),
            sum(user.started for user in self.residents),
            sum(user.completed for user in self.residents),
            sum(user.coalesced for user in self.residents),
        )

    def record_phase(self, phase, note=""):
        """One line of the account: what we did, and what the pool then held."""
        snapshot = self.census.snapshot
        self.phase = phase
        requested, started, completed, coalesced = self.operation_counts
        row = (f"{time.time():.1f},{time.time() - self.started_at:.0f},{phase},{note},"
               f"{len(self.residents)},{self.working_set_nominal},{self.in_flight_users},"
               f"{snapshot.get('authenticated', '')},{snapshot.get('placed', '')},"
               f"{snapshot.get('unplaced', '')},{snapshot.get('frozen', '')},"
               f"{snapshot.get('workers', '')},"
               f"\"{snapshot.get('worker_pids', '')}\","
               f"{requested},{started},{completed},{coalesced}\n")
        self.phase_log.write(row)
        self.phase_log.flush()
        print(f"[{phase}] {note} — residenti {len(self.residents)}, "
              f"working set {self.working_set_nominal}, in volo {self.in_flight_users}, "
              f"congelati {snapshot.get('frozen', '?')}, "
              f"collocati {snapshot.get('placed', '?')}, "
              f"worker {snapshot.get('workers', '?')}", flush=True)

    def populate(self, count, batch):
        """Bring *count* users in, in batches, measuring after each one.

        The watchdog's sentinel is checked between batches AND between
        arrivals: on the legacy leg the memory can run out inside a single
        batch, and stopping a batch late is stopping it too late.
        """
        accounts = self.accounts[:count]
        for start in range(0, count, batch):
            if self.memory_stop:
                self.record_phase("populate", f"memory_stop_at_{len(self.residents)}")
                break
            for number, username in enumerate(accounts[start:start + batch], start + 1):
                if self.memory_stop:
                    break
                user = ResidentUser(self, number, username)
                self.enter(user)
                user.start()
                self.residents.append(user)
                time.sleep(self.arguments.arrival_gap)
            for user in self.residents[start:start + batch]:
                user.ready.wait(timeout=60)
                user.work_once()
            time.sleep(self.arguments.batch_settle)
            self.record_phase("populate", f"batch_to_{len(self.residents)}")
        failures = sum(1 for user in self.residents if user.failed)
        print(f"popolamento: {len(self.residents)} residenti su {count} chiesti, "
              f"{failures} falliti"
              + (" — FERMATO PER MEMORIA" if self.memory_stop else ""),
              flush=True)
        return failures

    def rest(self, seconds, note):
        """Everybody quiet. Watch the freeze and the retirement happen."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            time.sleep(self.arguments.sample_every)
            self.record_phase("rest", note)

    def wake(self, count, spread):
        """Bring *count* frozen users back, spread over *spread* seconds.

        Slowly and one at a time: a burst of thaws would measure the freezer's
        queue instead of the thaw itself.
        """
        chosen = self.rng.sample(self.residents, min(count, len(self.residents)))
        self._working_set_size = len(chosen)
        gap = spread / max(1, len(chosen))
        for index, user in enumerate(chosen, 1):
            user.work_once(after_wake=True)
            time.sleep(gap)
            self.record_phase("wake", f"woken_{index}")
        self.collect_wake_latencies(chosen)
        return chosen

    def collect_wake_latencies(self, users):
        """Take both measures apart: the first call back, and the whole burst."""
        for user in users:
            if user.first_call_after_wake_ms is not None:
                self.wake_latencies.append(user.first_call_after_wake_ms)
                user.first_call_after_wake_ms = None
            if isinstance(user.operation_after_wake_ms, float):
                self.wake_operation_ms.append(user.operation_after_wake_ms)
                user.operation_after_wake_ms = None

    def work(self, users, seconds, think_range):
        """The working set works, each with his own pauses, for *seconds*."""
        self._working_set_size = len(users)
        deadline = time.time() + seconds
        next_due = {user: time.time() + self.rng.uniform(*think_range) for user in users}
        while time.time() < deadline:
            now = time.time()
            for user, due in list(next_due.items()):
                if now >= due:
                    user.work_once()
                    next_due[user] = now + self.rng.uniform(*think_range)
            time.sleep(1.0)
            if int(now) % self.arguments.sample_every == 0:
                self.record_phase("work", f"working_set_{len(users)}")

    def rotate(self, working_set, seconds, think_range, every):
        """Hold the active set's SIZE while the identities behind it change."""
        deadline = time.time() + seconds
        pool = [user for user in self.residents if user not in working_set]
        current = list(working_set)
        self._working_set_size = len(current)
        next_due = {user: time.time() + self.rng.uniform(*think_range) for user in current}
        last_swap = time.time()
        while time.time() < deadline:
            now = time.time()
            for user, due in list(next_due.items()):
                if now >= due:
                    user.work_once()
                    next_due[user] = now + self.rng.uniform(*think_range)
            if pool and now - last_swap >= every:
                leaving = self.rng.choice(current)
                arriving = self.rng.choice(pool)
                current.remove(leaving)
                next_due.pop(leaving, None)
                pool.remove(arriving)
                pool.append(leaving)
                current.append(arriving)
                arriving.work_once(after_wake=True)
                next_due[arriving] = now + self.rng.uniform(*think_range)
                last_swap = now
                self.collect_wake_latencies([arriving])
                self.record_phase("rotate", f"swap_in_{arriving.username}")
            time.sleep(1.0)
        self.collect_wake_latencies(current)

    def close_population(self):
        """Ask everybody to play his tail, and wait for them."""
        for user in self.residents:
            user.exit_event.set()
            user.work_event.set()
        for user in self.residents:
            user.join(timeout=120)

    def print_wake_summary(self):
        """The two measures, side by side and never added together."""
        requested, started, completed, coalesced = self.operation_counts
        print(f"operazioni: richieste {requested}, iniziate {started}, "
              f"completate {completed}, coalescenti {coalesced}")
        for label, values in (("prima chiamata dopo il thaw", self.wake_latencies),
                              ("intera operazione dopo il thaw", self.wake_operation_ms)):
            if not values:
                print(f"{label}: nessuna misura")
                continue
            ordered = sorted(values)
            print(f"{label}, {len(ordered)} utenti: "
                  f"p50 {self.percentile_of(ordered, 50)} ms, "
                  f"p95 {self.percentile_of(ordered, 95)} ms, "
                  f"max {ordered[-1]} ms")

    def percentile_of(self, values, which):
        if len(values) == 1:
            return values[0]
        return round(statistics.quantiles(values, n=100)[which - 1], 1)

    def run(self):
        """The plan, start to finish, with every phase written down."""
        sampler = SecondSampler(self, f"{self.arguments.out}_seconds.csv")
        sampler.start()
        arguments = self.arguments
        think = (arguments.min_think, arguments.max_think)
        try:
            self.record_phase("start", f"plan_{arguments.plan}")
            failures = self.populate(arguments.count, arguments.batch)
            if self.memory_stop:
                # No thaw phases after a safety stop: what follows would
                # measure a stack that was already out of room.
                self.record_phase("stopped", "memoria_durante_il_popolamento")
                return
            if failures and not arguments.ignore_failures:
                raise SystemExit(f"{failures} utenti non materializzati: mi fermo")
            self.rest(arguments.rest_seconds, "first_rest")
            working_set = self.wake(arguments.working_set, arguments.wake_spread)
            self.work(working_set, arguments.work_seconds, think)
            self.rotate(working_set, arguments.rotate_seconds, think, arguments.swap_every)
            self.rest(arguments.rest_seconds, "second_rest")
            self.record_phase("end", "before_logout")
        finally:
            self.close_population()
            self.record_phase("closed", "after_logout")
            time.sleep(arguments.settle)
            sampler.stop_event.set()
            sampler.join()
            self.calls.close()
            self.phase_log.close()
            self.write_summary()
        self.print_wake_summary()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("--base", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--accounts", default=ACCOUNTS)
    parser.add_argument("--census", help="the inspector census URL, bridge only")
    parser.add_argument("--plan", default="pilot", choices=("pilot", "full"))
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--batch", type=int, default=50)
    parser.add_argument("--arrival-gap", type=float, default=1.0,
                        help="seconds between two arrivals: slower than the freeze window")
    parser.add_argument("--batch-settle", type=float, default=20.0)
    parser.add_argument("--rest-seconds", type=float, default=600.0)
    parser.add_argument("--working-set", type=int, default=60)
    parser.add_argument("--wake-spread", type=float, default=180.0)
    parser.add_argument("--work-seconds", type=float, default=1200.0)
    parser.add_argument("--rotate-seconds", type=float, default=1500.0)
    parser.add_argument("--swap-every", type=float, default=25.0)
    parser.add_argument("--min-think", type=float, default=10.0)
    parser.add_argument("--max-think", type=float, default=120.0)
    parser.add_argument("--sample-every", type=int, default=10)
    parser.add_argument("--settle", type=float, default=120.0)
    parser.add_argument("--seed", type=int, default=20260828)
    parser.add_argument("--password", default="a")
    parser.add_argument("--ignore-failures", action="store_true")
    parser.add_argument("--stop-file",
                        help="file the memory check writes; its presence stops the population")
    # inherited by SessionBench.__init__, unused here but required by it
    parser.add_argument("--speed", type=float, default=2.0)
    parser.add_argument("--max-wait", type=float, default=3.0)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--peak", type=int, default=0)
    parser.add_argument("--login-every", type=float, default=0.0)
    parser.add_argument("--users", type=int, default=2000)
    # Prova 3 is not a capacity ramp: the saturation guard has nothing to
    # judge here, and would file windows that mean nothing. Off, and not
    # switchable — an option nobody should turn on is a trap, not a choice.
    parser.add_argument("--think-trace", default=None)
    arguments = parser.parse_args()
    arguments.guard = False
    if arguments.plan == "full":
        arguments.count = max(arguments.count, 2000)
    PopulationRun(arguments).run()


if __name__ == "__main__":
    main()
