"""Churn driver: a population that climbs, holds and drains, sampled every second.

The shape the owner asked for (2026-08-26), and the reason for each half:

- **climb** — one login per second, and every ``--up-logout-every`` seconds a
  RANDOM logged user leaves. The net climb is slower than the login rate, and
  the leaving user is the point: a pool that only ever grows is never asked to
  reuse the room somebody freed.
- **hold** — the peak, held for ``--hold`` seconds, so the sampler sees a
  steady state and not only slopes.
- **drain** — one logout per second, and one login every ``--down-login-every``
  seconds, until nobody is left. The arriving user is the mirror point: while
  the pool shrinks it must still place newcomers.

Who leaves and who arrives is drawn at random from the account pool, with a
seed so a run can be repeated (``--seed``).

Every logged user then behaves like a person, not a hammer: one getSelection
every ``--user-period`` seconds (default 3 s). At the peak that is a real load
— 256 users is ~85 calls per second — while leaving the stack enough room that
what the run measures is the COST OF THE POPULATION and not the depth of a
queue. A hammering run is a different measurement, and it already exists
(``single_record_bench.py``).

Sampling, once per second, into one CSV:

- client side: calls completed in that second, their latency percentiles,
  errors, logins and logouts performed, users currently in;
- server side, when ``--census`` is given: living workers, users placed, each
  worker's occupancy as the pool judges it, the group's memory percentage.

Memory and cpu of the processes are NOT read here: in the lab they are read
from the outside by ``docker stats``, whose stream is merged with this CSV by
wall-clock timestamp (see ``benchmarks/docker/README.md``). That keeps the
instrument out of the measure.

Run from benchmarks/ (data files are opened by relative name):

  python3 churn_driver.py --base http://bridge:8098 --peak 256 \
      --census http://bridge:8098/_server/inspector/census \
      --csv /lab/runtime/bridge_churn.csv
"""

import argparse
import http.client
import json
import random
import statistics
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, ".")
from replay_a1 import User, build_plan, inject_identity, load_capture  # noqa: E402
from single_record_bench import WHERE  # noqa: E402

CAPTURE = "session_capture.jsonl"
ACCOUNTS = "usernames_all.txt"


class LoggedUser:
    """One account logged in, with its own connection, its page and its thread.

    The login is the real two-call GenroPy dance (``inject_identity`` writes the
    identity into BOTH places, flat fields and the Bag of ``login_doLogin``);
    anything less logs every session in as the captured user and the whole run
    measures one identity.
    """

    def __init__(self, base, login_calls, pages, username, password, lookups, period):
        self.username = username
        self.period = period
        self.lookups = lookups
        self.netloc = urllib.parse.urlparse(base).netloc
        user = User(base, login_calls, pages, username, password)
        html = user._get("/")
        self.page_id = user._page_id_from(html)
        for form in login_calls:
            user._post("/", inject_identity(form, username, password), self.page_id, "login")
        user._get("/")
        jar = None
        for handler in user.opener.handlers:
            if hasattr(handler, "cookiejar"):
                jar = handler.cookiejar
        self.cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
        self.connection = http.client.HTTPConnection(self.netloc, timeout=30)
        self.stop_event = threading.Event()
        self.thread = None

    @property
    def headers(self):
        return {"Cookie": self.cookie,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}

    def get_call_form(self, lookup, callcounter):
        """The load unit: one indexed getSelection, its filter rotating."""
        return {
            "method": "app.getSelection", "table": "adm.user",
            "where": WHERE.format(username=lookup), "queryMode": "S",
            "sortedBy": "username", "selectionName": "*V_adm_user_churn",
            "recordResolver": "false::B", "sqlContextName": "standard_list",
            "totalRowCount": "false::B", "row_start": "0",
            "excludeLogicalDeleted": "true::B", "excludeDraft": "true::B",
            "columns": "$username", "checkPermissions": "true::B",
            "row_count": "1::L", "storepath": ".store",
            "page_id": self.page_id, "callcounter": str(callcounter),
        }

    def start_traffic(self, record, record_failure):
        """Begin this user's own paced traffic; *record* takes (latency, failed)."""
        self.thread = threading.Thread(target=self.generate_traffic,
                                       args=(record, record_failure), daemon=True)
        self.thread.start()

    def generate_traffic(self, record, record_failure):
        """One call every ``period`` seconds until told to leave."""
        counter = 0
        while not self.stop_event.is_set():
            started = time.time()
            lookup = self.lookups[counter % len(self.lookups)]
            body = urllib.parse.urlencode(self.get_call_form(lookup, counter + 100))
            failed = self.send_call(body, record_failure)
            record(time.time() - started, failed)
            counter += 1
            self.stop_event.wait(max(0.0, self.period - (time.time() - started)))

    def send_call(self, body, record_failure):
        """Send one call, reconnecting once if the kept-alive socket was closed.

        A server that closed an idle keep-alive connection has not failed, and a
        browser does not report it: it opens another and asks again. Counting
        that as an error measures the server's keep-alive window instead of its
        behaviour under load — which is exactly what a first run of this driver
        did against gunicorn (2 s idle window, users calling every 3 s), and it
        read as half the calls failing.

        Returns:
            True when the call really failed — the retry included.
        """
        for attempt in (1, 2):
            try:
                self.connection.request("POST", "/", body=body, headers=self.headers)
                answer = self.connection.getresponse()
                payload = answer.read()
                if answer.status != 200 or b"<error>" in payload:
                    record_failure(f"http {answer.status}: {payload[:300]!r}")
                    return True
                return False
            except Exception as failure:
                self.connection.close()
                self.connection = http.client.HTTPConnection(self.netloc, timeout=30)
                if attempt == 2:
                    record_failure(f"{type(failure).__name__} twice: {failure}")
                    return True
        return True

    def log_out(self):
        """Stop the traffic, then tell the site the connection is over.

        The logout is what frees the user's placement: without it the register
        keeps him and the pool never learns the room is back.
        """
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        try:
            body = urllib.parse.urlencode({"method": "connection.logout", "page_id": self.page_id,
                                           "callcounter": "9999"})
            self.connection.request("POST", "/", body=body, headers=self.headers)
            self.connection.getresponse().read()
        except Exception:
            pass
        finally:
            self.connection.close()


class SecondSampler(threading.Thread):
    """One CSV row per second: what the clients saw, and what the pool says."""

    COLUMNS = ("wall,t,phase,users_in,logins,logouts,calls,errors,"
               "p50_ms,p90_ms,p99_ms,workers,users_placed,occupancy_max,occupancy_all,memory_pct")

    def __init__(self, churn_run, csv_path, census_url=None, interval=1.0):
        super().__init__(daemon=True)
        self.churn_run = churn_run
        self.census_url = census_url
        self.interval = interval
        self.csv = open(csv_path, "w")
        self.csv.write(self.COLUMNS + "\n")
        self.stop_event = threading.Event()
        self.started_at = time.time()

    def read_census(self):
        """Living workers, placements, occupancies — or empty when unreachable."""
        if not self.census_url:
            return "", "", "", "", ""
        try:
            with urllib.request.urlopen(self.census_url, timeout=2) as answer:
                payload = json.load(answer)
        except Exception:
            return "", "", "", "", ""
        if "groups" not in payload:
            payload = next(iter(payload.values()), {})
        group = next(iter(payload.get("groups", {}).values()), {})
        workers = group.get("workers", {})
        occupancies = [worker["occupancy_percent"] for worker in workers.values()]
        return (len(workers),
                len(group.get("user_worker_map", {})),
                f"{max(occupancies):.1f}" if occupancies else "",
                "|".join(f"{value:.1f}" for value in occupancies),
                f"{group.get('memory_occupied_percent', 0):.1f}")

    def run(self):
        while not self.stop_event.is_set():
            deadline = time.time() + self.interval
            self.stop_event.wait(max(0.0, deadline - time.time()))
            latencies, errors, logins, logouts = self.churn_run.take_second()
            workers, placed, occupancy_max, occupancy_all, memory = self.read_census()
            milliseconds = sorted(value * 1000 for value in latencies)
            self.csv.write(
                f"{time.time():.1f},{time.time()-self.started_at:.0f},{self.churn_run.phase},"
                f"{self.churn_run.users_in},{logins},{logouts},{len(milliseconds)},{errors},"
                f"{self.percentile(milliseconds, 50)},{self.percentile(milliseconds, 90)},"
                f"{self.percentile(milliseconds, 99)},"
                f"{workers},{placed},{occupancy_max},{occupancy_all},{memory}\n")
            self.csv.flush()
        self.csv.close()

    def percentile(self, values, which):
        """The percentile of a sorted list, blank when the second was silent."""
        if not values:
            return ""
        if len(values) == 1:
            return f"{values[0]:.1f}"
        return f"{statistics.quantiles(values, n=100)[which - 1]:.1f}"


class ChurnRun:
    """The whole population: who is in, who arrives, who leaves, and when."""

    def __init__(self, arguments):
        self.arguments = arguments
        self.random = random.Random(arguments.seed)
        rows = load_capture(CAPTURE)
        self.login_calls, self.pages = build_plan(rows)
        self.accounts = [line.strip() for line in open(ACCOUNTS) if line.strip()]
        self.lookups = self.accounts[:32]
        self.logged_map = {}
        self.phase = "start"
        self.lock = threading.Lock()
        self.latencies = []
        self.errors = 0
        self.logins = 0
        self.logouts = 0
        self.failed_logins = 0
        self.failure_kinds = {}

    @property
    def users_in(self):
        return len(self.logged_map)

    @property
    def free_accounts(self):
        return [name for name in self.accounts if name not in self.logged_map]

    def record_failure(self, description):
        """Keep every DISTINCT failure once, with how many times it happened.

        A run that says "half the calls failed" and cannot say what the failure
        was has measured nothing anybody can act on.
        """
        with self.lock:
            self.failure_kinds[description[:200]] = \
                self.failure_kinds.get(description[:200], 0) + 1

    def record_call(self, latency, failed):
        with self.lock:
            self.latencies.append(latency)
            if failed:
                self.errors += 1

    def take_second(self):
        """Hand the sampler what happened since it last asked, and start over."""
        with self.lock:
            taken = (self.latencies, self.errors, self.logins, self.logouts)
            self.latencies, self.errors, self.logins, self.logouts = [], 0, 0, 0
        return taken

    def warm_up(self):
        """Log one throwaway user in, and out, before anybody is measured.

        The first request to a fresh stack builds the site — on the bridge it
        builds a worker — and everything attempted meanwhile fails. Those
        failures belong to the start, not to the run, so they are absorbed here
        and the sampler only opens afterwards.
        """
        deadline = time.time() + self.arguments.warmup_timeout
        attempts = 0
        while time.time() < deadline:
            attempts += 1
            try:
                logged = LoggedUser(self.arguments.base, self.login_calls, self.pages,
                                    self.accounts[-1], self.arguments.password,
                                    self.lookups, self.arguments.user_period)
            except Exception:
                time.sleep(2)
                continue
            logged.log_out()
            return f"ready after {attempts} attempt(s)"
        return f"NOT READY after {attempts} attempts — the run starts on a cold stack"

    def log_in_random(self):
        """One free account, drawn at random, logged in and put to work."""
        free = self.free_accounts
        if not free:
            return False
        username = self.random.choice(free)
        try:
            logged = LoggedUser(self.arguments.base, self.login_calls, self.pages,
                                username, self.arguments.password,
                                self.lookups, self.arguments.user_period)
        except Exception:
            with self.lock:
                self.failed_logins += 1
            return False
        logged.start_traffic(self.record_call, self.record_failure)
        self.logged_map[username] = logged
        with self.lock:
            self.logins += 1
        return True

    def log_out_random(self):
        """One logged user, drawn at random, sent away for good."""
        if not self.logged_map:
            return False
        username = self.random.choice(list(self.logged_map))
        self.logged_map.pop(username).log_out()
        with self.lock:
            self.logouts += 1
        return True

    def run_phase(self, phase, arrival_period, departure_period, keep_going):
        """Drive arrivals and departures on their own clocks while *keep_going*."""
        self.phase = phase
        now = time.time()
        next_arrival = now + arrival_period if arrival_period else float("inf")
        next_departure = now + departure_period if departure_period else float("inf")
        while keep_going():
            now = time.time()
            if now >= next_arrival:
                self.log_in_random()
                next_arrival = now + arrival_period
            if now >= next_departure:
                self.log_out_random()
                next_departure = now + departure_period
            time.sleep(0.05)

    def climb(self):
        """Up to the peak: one login a second, a random leaver every few."""
        self.run_phase("climb", self.arguments.up_login_every,
                       self.arguments.up_logout_every,
                       lambda: self.users_in < self.arguments.peak)

    def hold(self):
        """The peak, still: nobody arrives, nobody leaves."""
        self.phase = "hold"
        time.sleep(self.arguments.hold)

    def drain(self):
        """Down to nobody: one logout a second, a newcomer every few."""
        deadline = time.time() + self.arguments.drain_timeout
        self.run_phase("drain", self.arguments.down_login_every,
                       self.arguments.down_logout_every,
                       lambda: self.users_in > 0 and time.time() < deadline)

    def stop_everybody(self):
        """Whoever is still in leaves now — the run is over."""
        self.phase = "stop"
        for username in list(self.logged_map):
            self.logged_map.pop(username).log_out()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--census")
    parser.add_argument("--peak", type=int, default=256)
    parser.add_argument("--hold", type=float, default=60)
    parser.add_argument("--user-period", type=float, default=3.0)
    parser.add_argument("--up-login-every", type=float, default=1.0)
    parser.add_argument("--up-logout-every", type=float, default=5.0)
    parser.add_argument("--down-logout-every", type=float, default=1.0)
    parser.add_argument("--down-login-every", type=float, default=5.0)
    parser.add_argument("--drain-timeout", type=float, default=900)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--warmup-timeout", type=float, default=180)
    parser.add_argument("--seed", type=int, default=20260826)
    parser.add_argument("--password", default="a")
    arguments = parser.parse_args()

    run = ChurnRun(arguments)
    print(f"accounts available: {len(run.accounts)}, peak asked: {arguments.peak}", flush=True)
    print(f"warm-up: {run.warm_up()}", flush=True)
    sampler = SecondSampler(run, arguments.csv, arguments.census, arguments.sample_interval)
    sampler.start()
    started = time.time()
    for name, step in (("climb", run.climb), ("hold", run.hold), ("drain", run.drain)):
        step()
        print(f"{name} done at {time.time()-started:.0f}s — users in: {run.users_in}", flush=True)
    run.stop_everybody()
    time.sleep(3)
    sampler.stop_event.set()
    sampler.join()
    print(f"done in {time.time()-started:.0f}s, failed logins: {run.failed_logins}, "
          f"csv={arguments.csv}", flush=True)
    if run.failure_kinds:
        print("failures, by kind:", flush=True)
        for description, count in sorted(run.failure_kinds.items(),
                                         key=lambda pair: -pair[1])[:8]:
            print(f"  {count:6d}x {description}", flush=True)


if __name__ == "__main__":
    main()
