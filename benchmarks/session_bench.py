"""Session bench: the owner's recorded session, replayed at a fixed speed
coefficient by one or many emulated users, against a live stack.

The design, decided with the owner (2026-08-27) — FIXED:

- The tour is the archive's user exchanges, in recorded order: statics and
  pings excluded, everything else in — login, page bursts, the heavy export,
  the closing beacons and the logout.
- Time is reshaped in three steps, in this order (owner, 2026-08-27):
  (1) the ORIGINAL recording is altered first — an idle stretch longer than
  ``--max-wait`` seconds becomes ``--max-wait`` (default 3: a user who did
  nothing for 10 seconds is emulated as doing nothing for 3);
  (2) everything is then divided by ONE coefficient (``--speed``, 2 = double
  speed), so the shape of the work is the recorded shape, only faster;
  (3) the compression has a floor: no distance ever drops below the SMALLEST
  distance the recording itself holds — the emulation never produces a rhythm
  denser than the real browser did, because past that point the run would not
  be faster, it would be a different measurement.
  The ping does NOT compress: one every 3 seconds on its own timer, because
  the real browser pings on a timer of its own.
- ``--login-every N``: one user enters every N seconds and replays the whole
  tour with an account of his own, then leaves. 0 = one single user: the
  BASELINE, whose sum of response times and per-5s medians are the reference
  every later run is compared against. The population self-regulates: with a
  200 s tour and one entry every 3 s it settles around 67, and it exceeds that
  number exactly when the stack slows the tours down — the excess IS degradation.
- Per user, the run keeps a PROFILE: every call stamped with the user, its
  index in the tour, and the population at the instant it was sent. Profiles
  aggregate after the run, from the calls CSV.
- Buckets are cut on the SCHEDULED offset, not the actual one, so bucket 12 of
  any user on any stack always holds the same calls of the tour.
- The tour is played as an ITERATOR in three segments (owner, 2026-08-27):
  the HEAD (the login calls, once), the BODY (the work, looped, a flag checked
  before every call), the TAIL (the closing beacons and the logout, once).
  Every mode is only a policy on who raises the flag: the default raises it
  after ``--rounds`` passes of the body; churn mode raises it by draw.
- ``--peak N`` turns on CHURN MODE: one user enters every ``--login-every``
  seconds, every ``--churn-every`` seconds a RANDOM logged user is told to
  leave (a user still working abandons: he finishes the call in flight and
  plays his tail), and accounts re-enter, so the population climbs anyway.
  A user whose body is done stays logged in, pings only, until drawn. When
  the population reaches the peak: no more entries, no more churn draws — the
  CLOSURE begins, one random exit every ``--drain-every`` seconds, to zero.
  The per-user viewpoint dies with the churn (tours are no longer comparable):
  these runs are read machine-side, all calls per interval.
- ``--settle N``: after the last user leaves, sampling continues for N seconds —
  the window where the memory that came back, and the memory that did not, is
  read (from the outside, by docker stats; this file never asks the stacks).

PROVISIONAL: the archive did not record the ping's body (the recorder drops
it), so the replayed ping carries the one field the site's ``serve_ping``
dispatches on — the page_id of the user's current frame page. The real browser
channels the ping on the master page; this is that, minus fields we cannot know.

Identifier adaptation follows the replica's rules (compare/replica.py):
page ids the target mints replace the trace's wherever they appear, in form
values and query strings. This file adds ONE learning the replica did not
need: the export download path ``/_page/<connection>/<page>/output/...`` comes
back inside the RPC reply, so both tokens are learned from the reply pair and
the GET that follows is rewritten whole. The trace reader and the identity
map live HERE, not imported from the replica: this driver runs in the lab's
measuring container, which is stdlib-only by design, while the replica's
module chain imports genropy for the parity check.

Outputs, given ``--out PREFIX``:

- ``PREFIX_calls.csv`` — one row per call: wall, user, round, index, kind
  (tour|ping), label, population at send, scheduled offset, lateness, ms,
  status, expected, ok.
- ``PREFIX_seconds.csv`` — one row per second, same columns as churn_driver's
  sampler (census included when ``--census`` is given), so the existing
  merge with docker stats works unchanged.

Run from benchmarks/ (data files are opened by relative name):

  python3 session_bench.py ~/genro_bench/runs/legacy-20260827T081722.sqlite \\
      --base http://127.0.0.1:8099 --speed 2 --login-every 0 \\
      --out /lab/runtime/legacy_base_x2
"""

import argparse
import datetime
import http.client
import json
import os
import random
import re
import sqlite3
import statistics
import sys
import threading
import time
import urllib.parse
import urllib.request

BENCH = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BENCH)

from scaling_probe import StickyClient, inject_identity  # noqa: E402

PAGE_ID_RE = re.compile(r"page_id:'([A-Za-z0-9_-]{22})'")


class TraceReader:
    """The HTTP exchanges of one archived run, in the order they happened."""

    def __init__(self, path):
        self.path = path
        self.connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    @property
    def records(self):
        """Every recorded HTTP line, oldest first."""
        rows = self.connection.execute(
            "SELECT line FROM record WHERE kind = 'http' ORDER BY ts, id").fetchall()
        return [json.loads(row[0]) for row in rows]


class IdentityMap:
    """Trace tokens and the tokens the target minted in their place."""

    def __init__(self):
        self.tokens = {}

    def learn_page_id(self, trace_body, target_body):
        """Pair the page the trace was given with the page the target minted."""
        trace_id = self.get_page_id(trace_body)
        target_id = self.get_page_id(target_body)
        if trace_id and target_id:
            self.tokens[trace_id] = target_id

    def get_page_id(self, html):
        found = PAGE_ID_RE.search(html or "")
        return found.group(1) if found else None

    def get_adapted(self, text):
        """The same text with every known trace token replaced by the target's."""
        for trace_id, target_id in self.tokens.items():
            text = text.replace(trace_id, target_id)
        return text

ACCOUNTS = "usernames_all.txt"
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=UTF-8"
PING_SECONDS = 3.0
# The two tokens of a download path, as the RPC reply carries them:
# /_page/<connection>/<page>/output/... — learned as a pair from the reply.
PAGE_PATH_RE = re.compile(r"/_page/([A-Za-z0-9_-]{22})/([A-Za-z0-9_-]{22})/")


class SessionIdentityMap(IdentityMap):
    """The replica's map, plus the download-path pair the export needs."""

    def learn_page_path(self, trace_body, target_body):
        """Pair the /_page/<connection>/<page>/ tokens of the two replies."""
        trace_found = PAGE_PATH_RE.search(trace_body or "")
        target_found = PAGE_PATH_RE.search(target_body or "")
        if trace_found and target_found:
            self.tokens[trace_found.group(1)] = target_found.group(1)
            self.tokens[trace_found.group(2)] = target_found.group(2)


class Tour:
    """The recorded session as a replayable plan: (offset, record) pairs.

    Offsets are seconds from the FIRST user exchange, unscaled: the scaling
    is the player's business, so one Tour serves every speed.
    """

    def __init__(self, archive_path):
        self.trace = TraceReader(archive_path)
        self.exchanges = [record for record in self.trace.records
                          if not self.get_skip_reason(record)]
        first = self.get_timestamp(self.exchanges[0])
        self.offsets = [self.get_timestamp(record) - first
                        for record in self.exchanges]

    def get_skip_reason(self, record):
        """Why this exchange stays out of the tour, or None when it is in."""
        path = record.get("path") or ""
        if record.get("filtered") == "static":
            return "static"
        if path.startswith("/_rsrc") or path.startswith("/_static"):
            return "static"
        if path == "/_ping":
            return "ping"
        return None

    def get_timestamp(self, record):
        return datetime.datetime.fromisoformat(record["ts"]).timestamp()

    @property
    def login_end(self):
        """Index of the last login call: the middle begins after it."""
        for index in range(len(self.exchanges) - 1, -1, -1):
            method = (self.exchanges[index].get("form") or {}).get("method") or ""
            if "login_doLogin" in method:
                return index
        raise SystemExit("the archive holds no login_doLogin: not a session tour")

    @property
    def tail_start(self):
        """Index of the first closing beacon: the middle ends before it.

        The closing block is the run of /_beacon calls that leads into
        connection.logout, plus everything after it.
        """
        for index, record in enumerate(self.exchanges):
            if (record.get("rpc_method") or "") == "connection.logout":
                start = index
                while start > 0 and self.exchanges[start - 1].get("path") == "/_beacon":
                    start -= 1
                return start
        raise SystemExit("the archive holds no connection.logout: not a session tour")

    def get_plan(self, rounds):
        """The (offset, record) list of one whole session, middle repeated."""
        login_end, tail_start = self.login_end, self.tail_start
        plan = [(self.offsets[i], self.exchanges[i]) for i in range(login_end + 1)]
        middle = [(self.offsets[i], self.exchanges[i])
                  for i in range(login_end + 1, tail_start)]
        middle_span = self.offsets[tail_start - 1] - self.offsets[login_end]
        shift = 0.0
        for _ in range(rounds):
            plan.extend((offset + shift, record) for offset, record in middle)
            shift += middle_span
        plan.extend((self.offsets[i] + shift - middle_span if rounds else self.offsets[i],
                     self.exchanges[i])
                    for i in range(tail_start, len(self.exchanges)))
        return plan


class CallLog:
    """Every call of the run, appended thread-safe, drained by the sampler."""

    COLUMNS = ("wall,user,username,round,seq,kind,label,population,"
               "offset_s,late_ms,duration_ms,status,expected,ok")

    def __init__(self, csv_path):
        self.csv = open(csv_path, "w")
        self.csv.write(self.COLUMNS + "\n")
        self.lock = threading.Lock()
        self.second_latencies = []
        self.second_errors = 0
        self.second_late = []

    def record_call(self, row):
        """One finished call: written now, counted into the current second."""
        with self.lock:
            self.csv.write(",".join(str(value) for value in row) + "\n")
            duration_ms, ok = float(row[10]), row[13]
            self.second_late.append(float(row[9]))
            if ok:
                self.second_latencies.append(duration_ms / 1000.0)
            else:
                self.second_errors += 1

    def take_second(self):
        """Drain what the ending second accumulated.

        The lateness rides along because the saturation guard reads it: a call
        that leaves later than its scheduled offset is the client's own view of
        a stack that is not keeping up.
        """
        with self.lock:
            latencies, self.second_latencies = self.second_latencies, []
            errors, self.second_errors = self.second_errors, 0
            late, self.second_late = self.second_late, []
        return latencies, errors, late

    def close(self):
        with self.lock:
            self.csv.close()


class EmulatedUser(threading.Thread):
    """One user: the whole tour at speed, with his own account and cookie jar."""

    #: The two client clocks the site reads to decide who has gone quiet, in
    #: the typed-text shape the recording carries: "YYYY-MM-DD HH:MM:SS::DH".
    CLIENT_CLOCK_FIELDS = ("_lastUserEventTs", "_lastRpc")
    CLIENT_CLOCK_FORMAT = "%Y-%m-%d %H:%M:%S"
    CLIENT_CLOCK_SUFFIX = "::DH"

    def __init__(self, bench, number, username, think_times=()):
        super().__init__(daemon=True, name=f"user-{number}")
        self.bench = bench
        self.number = number
        self.username = username
        self.think_times = list(think_times)
        #: This user's own activity clock. The recording carries the clocks of
        #: the day it was made; replaying them would tell the site every user
        #: has been idle since August, and the freeze valve would park
        #: everybody the moment it is switched on. So the values are REWRITTEN
        #: from here, and this is what makes silence mean silence: a real RPC
        #: moves both hands, a ping moves neither and merely reports them.
        self.last_user_event_ts = time.time()
        self.last_rpc_ts = time.time()
        self.identity = SessionIdentityMap()
        self.client = None
        self.frame_page_id = None
        self.pinger = None
        self.ping_stop = threading.Event()
        self.exit_event = threading.Event()
        self.cursor = 0.0

    def run(self):
        """The iterator: head once, body until the flag, tail once."""
        self.client = StickyClient(self.bench.host, self.bench.port)
        self.cursor = time.time()
        try:
            self.play(self.bench.head_rows, 0)
            round_number = 1
            while not self.exit_event.is_set():
                self.play(self.bench.body_rows, round_number)
                self.take_think_pause(round_number)
                if self.bench.arguments.peak:
                    # the body is done: stay logged in, pings only, until drawn
                    self.exit_event.wait()
                    break
                if round_number >= max(1, self.bench.arguments.rounds):
                    break
                round_number += 1
            self.cursor = time.time()
            self.play(self.bench.tail_rows, 0)
        finally:
            self.ping_stop.set()
            self.bench.leave(self)

    def take_think_pause(self, round_number):
        """Sleep the pause the trace drew for this burst, if there is one.

        WHAT THIS IS, precisely: one pass of the body is a BURST OF RECORDED
        WORK, and the pause follows the burst. It is NOT "a pause after each
        operator action", and calling it that would claim a boundary the
        recording does not carry.

        The inventory that settles it — 101 exchanges in the body over 192.7
        recorded seconds, of which 38 getRemoteTranslation, 17 app.getSelection,
        6 main, 5 each of resolverRecall, loadRecordCluster, app.dbSelect and
        relationExplorer. The only visible candidates for an action boundary
        are the five GETs of ``/sys/thpage/...`` that open a page; the other
        ninety-odd calls carry nothing that separates one intention from the
        next. So no boundary is claimed, and the whole body is replayed as one
        burst.

        The pauses are not drawn here: they were drawn into the trace before
        the run, so both legs replay the same ones in the same order.
        """
        if not self.think_times:
            return
        pause = self.think_times[(round_number - 1) % len(self.think_times)]
        self.exit_event.wait(pause)

    def play(self, rows, round_number):
        """One segment on the schedule; the body checks the flag every call."""
        abandonable = rows is self.bench.body_rows
        for gap, sequence, record in rows:
            if abandonable and self.exit_event.is_set():
                return
            self.cursor += gap
            delay = self.cursor - time.time()
            if delay > 0:
                time.sleep(delay)
            self.replay_call(record, round_number, sequence,
                             (time.time() - self.cursor) * 1000)

    def replay_call(self, record, round_number, sequence, late_ms):
        """Send one recorded exchange; log what it cost from here."""
        label = (f"{record.get('method')} {record.get('path')} "
                 f"{record.get('rpc_method') or ''}").strip().replace(",", ";")
        population = self.bench.population
        began = time.time()
        try:
            status, body = self.send_exchange(record)
            failure = None
        except Exception as error:
            failure = f"{type(error).__name__}"
            status, body = 0, b""
            self.reconnect()
        duration_ms = (time.time() - began) * 1000
        expected = record.get("status")
        ok = failure is None and status == expected
        self.bench.calls.record_call((
            f"{began:.3f}", self.number, self.username, round_number, sequence,
            "tour", label, population, f"{began - self.bench.started_at:.1f}",
            f"{late_ms:.0f}", f"{duration_ms:.1f}", status, expected, ok))
        text = body.decode("utf-8", "replace")
        self.identity.learn_page_id(record.get("resp_body"), text)
        self.identity.learn_page_path(record.get("resp_body"), text)
        self.learn_frame(record, text)

    def send_exchange(self, record):
        """The recorded method, path and body, identifiers rewritten."""
        self.touch_activity(record)
        path = self.identity.get_adapted(record.get("path") or "/")
        query = record.get("query")
        if query:
            path = f"{path}?{self.identity.get_adapted(query)}"
        if record.get("method") == "GET":
            return self.send_request("GET", path)
        if record.get("form") is not None:
            form = inject_identity(self.get_adapted_form(record),
                                   self.username, self.bench.arguments.password)
            return self.send_request("POST", path,
                                     urllib.parse.urlencode(form, doseq=True),
                                     FORM_CONTENT_TYPE)
        headers = record.get("req_headers") or {}
        return self.send_request("POST", path, record.get("req_body") or "",
                                 headers.get("Content-Type"))

    def get_adapted_form(self, record):
        """The recorded form: identifiers rewritten, client clocks made ours.

        Ninety-three of the body's calls carry ``_lastUserEventTs`` and
        ``_lastRpc``. Left as recorded they are fossils — the site would read
        an idleness of days on a user who is working right now. They are
        replaced with this user's own clock, which is what a browser sends.
        """
        adapted = {}
        for key, value in record["form"].items():
            if key in self.CLIENT_CLOCK_FIELDS:
                continue
            if isinstance(value, str):
                adapted[key] = self.identity.get_adapted(value)
            elif isinstance(value, list):
                adapted[key] = [self.identity.get_adapted(item) for item in value]
            else:
                adapted[key] = value
        for key in self.CLIENT_CLOCK_FIELDS:
            if key in record["form"]:
                adapted[key] = self.get_client_clock(key)
        return adapted

    def get_client_clock(self, field):
        """One clock as typed text, from the hand this field reports."""
        stamp = (self.last_user_event_ts if field == "_lastUserEventTs"
                 else self.last_rpc_ts)
        return (datetime.datetime.fromtimestamp(stamp).strftime(self.CLIENT_CLOCK_FORMAT)
                + self.CLIENT_CLOCK_SUFFIX)

    def touch_activity(self, record):
        """A real call moves both hands; a sysrpc and a ping move neither.

        Verified in the site's own client: a non-sysrpc RPC stamps ``_lastRpc``
        with the current time and sends both clocks, while the ping sends the
        stored values untouched. So a browser left open on an untouched page
        stays freezable, and this reproduces that instead of approximating it
        by leaving the fields out.
        """
        if (record.get("rpc_method") or "").startswith("sys"):
            return
        now = time.time()
        self.last_rpc_ts = now
        self.last_user_event_ts = now

    def send_request(self, method, path, body=None, content_type=None):
        """One request, one reply — reconnecting once on a dropped keep-alive.

        The server closes an idle keep-alive connection (gunicorn after 30 s,
        the bridge sooner) and the browser silently reconnects: the retry is
        that, not an error — the cookie jar survives, only the socket is new.
        """
        headers = self.client._headers()
        if content_type:
            headers["Content-Type"] = content_type
        for attempt in (1, 2):
            try:
                self.client.conn.request(method, path, body=body, headers=headers)
                response = self.client.conn.getresponse()
                answer = response.read()
                self.client._store_cookies(response)
                return response.status, answer
            except (ConnectionError, OSError, http.client.HTTPException):
                self.reconnect()
                if attempt == 2:
                    raise

    def reconnect(self):
        """A new socket under the same cookie jar: the session survives."""
        cookies = self.client.cookies
        self.client = StickyClient(self.bench.host, self.bench.port)
        self.client.cookies = cookies

    def learn_frame(self, record, target_body):
        """Track the current frame page: the ping is channelled on it.

        The frame is the page a GET of ``/`` or ``/index`` bootstraps; every
        reload replaces it. The first frame learned also starts the pinger.
        """
        if record.get("method") != "GET" or record.get("path") not in ("/", "/index"):
            return
        found = self.identity.get_page_id(target_body)
        if not found:
            return
        self.frame_page_id = found
        if self.pinger is None:
            self.pinger = threading.Thread(target=self.ping_forever, daemon=True,
                                           name=f"ping-{self.number}")
            self.pinger.start()

    def ping_forever(self):
        """One ping every 3 seconds, uncompressed, on a connection of its own.

        The ping REPORTS the two client clocks and does not move them, which is
        what the site's own client does. That is why a browser left open on a
        page nobody touches still becomes freezable: its pings keep saying the
        same, ageing, hour.
        """
        client = StickyClient(self.bench.host, self.bench.port)
        client.cookies = self.client.cookies
        while not self.ping_stop.wait(PING_SECONDS):
            population = self.bench.population
            began = time.time()
            try:
                status, _ = client.post("/_ping", {
                    "page_id": self.frame_page_id,
                    "_lastUserEventTs": self.get_client_clock("_lastUserEventTs"),
                    "_lastRpc": self.get_client_clock("_lastRpc"),
                })
                ok = status == 200
            except Exception:
                status, ok = 0, False
                client = StickyClient(self.bench.host, self.bench.port)
                client.cookies = self.client.cookies
            self.bench.calls.record_call((
                f"{began:.3f}", self.number, self.username, 0, 0,
                "ping", "POST /_ping", population,
                f"{began - self.bench.started_at:.1f}", 0,
                f"{(time.time() - began) * 1000:.1f}", status, 200, ok))


class SecondSampler(threading.Thread):
    """One CSV row per second: churn_driver's columns, census included."""

    #: The per-second percentiles are the campaign's own: p95 is the criterion
    #: the SaturationGuard judges a window by, so the per-second file carries
    #: the same figure and not a p90 nobody decides anything on.
    COLUMNS = ("wall,t,phase,users_in,logins,logouts,calls,errors,"
               "p50_ms,p95_ms,p99_ms,workers,users_placed,occupancy_max,"
               "occupancy_all,memory_pct")

    def __init__(self, bench, csv_path):
        super().__init__(daemon=True, name="sampler")
        self.bench = bench
        self.csv = open(csv_path, "w")
        self.csv.write(self.COLUMNS + "\n")
        self.stop_event = threading.Event()

    def read_census(self):
        if not self.bench.arguments.census:
            return "", "", "", "", ""
        try:
            with urllib.request.urlopen(self.bench.arguments.census, timeout=2) as answer:
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
            deadline = time.time() + 1.0
            self.stop_event.wait(max(0.0, deadline - time.time()))
            latencies, errors, late = self.bench.calls.take_second()
            if self.bench.guard:
                self.bench.guard.take_second(latencies, errors, late,
                                             self.bench.population)
            logins, logouts = self.bench.take_movements()
            workers, placed, occupancy_max, occupancy_all, memory = self.read_census()
            milliseconds = sorted(value * 1000 for value in latencies)
            self.csv.write(
                f"{time.time():.1f},{time.time() - self.bench.started_at:.0f},"
                f"{self.bench.phase},{self.bench.population},{logins},{logouts},"
                f"{len(milliseconds)},{errors},"
                f"{self.percentile(milliseconds, 50)},{self.percentile(milliseconds, 95)},"
                f"{self.percentile(milliseconds, 99)},"
                f"{workers},{placed},{occupancy_max},{occupancy_all},{memory}\n")
            self.csv.flush()
        self.csv.close()

    def percentile(self, values, which):
        if not values:
            return ""
        if len(values) == 1:
            return f"{values[0]:.1f}"
        return f"{statistics.quantiles(values, n=100)[which - 1]:.1f}"


class SaturationGuard:
    """Stops the ramp when the p95 stays above the limit for two windows.

    One criterion, because the question has one: how many continuous users are
    served before the answers stay above a second. One bad window is a spike;
    two in a row are a state. Errors and completed calls are written to the
    windows CSV as well, for reading afterwards, but they do not stop anything:
    the only other thing that stops a run is the memory check, and that one is
    a safety stop and says so.
    """

    COLUMNS = ("window,elapsed_s,population,calls,errors,error_percent,"
               "p50_ms,p95_ms,over_limit")

    def __init__(self, bench, csv_path):
        self.bench = bench
        self.window_seconds = bench.arguments.saturation_window
        self.p95_limit = bench.arguments.p95_limit
        self.saturated = threading.Event()
        self.csv = open(csv_path, "w")
        self.csv.write(self.COLUMNS + "\n")
        self.number = 0
        self.seconds_in_window = 0
        self.latencies = []
        self.errors = 0
        self.population = 0
        self.streak = 0
        self.first_over = None
        self.second_over = None
        self.last_under = 0

    def take_second(self, latencies, errors, late, population):
        """Fold one sampled second into the open window; close it when full."""
        self.latencies.extend(latencies)
        self.errors += errors
        self.population = population
        self.seconds_in_window += 1
        if self.seconds_in_window >= self.window_seconds:
            self.close_window()

    def close_window(self):
        """Judge the window just ended and write it."""
        self.number += 1
        milliseconds = sorted(value * 1000 for value in self.latencies)
        calls, errors = len(milliseconds), self.errors
        offered = calls + errors
        p50 = self.get_percentile(milliseconds, 50)
        p95 = self.get_percentile(milliseconds, 95)
        over = bool(p95 and p95 > self.p95_limit)
        self.csv.write(f"{self.number},{time.time() - self.bench.started_at:.0f},"
                       f"{self.population},{calls},{errors},"
                       f"{100.0 * errors / offered if offered else 0:.2f},"
                       f"{p50},{p95},{int(over)}\n")
        self.csv.flush()
        if over:
            self.streak += 1
            if self.first_over is None:
                self.first_over = (self.population, p50, p95, calls, errors)
            elif self.second_over is None:
                self.second_over = (self.population, p50, p95, calls, errors)
            print(f"finestra {self.number}: {self.population} utenti, "
                  f"p95 {p95} ms sopra {self.p95_limit:.0f} "
                  f"({self.streak} di fila)", flush=True)
            if self.streak >= 2 and not self.saturated.is_set():
                self.saturated.set()
                print(f"SATURA a {self.population} utenti", flush=True)
        else:
            self.streak = 0
            self.last_under = max(self.last_under, self.population)
        self.seconds_in_window = 0
        self.latencies = []
        self.errors = 0

    def get_percentile(self, values, which):
        """The requested percentile of an already sorted list, or 0 if empty."""
        if not values:
            return 0.0
        if len(values) == 1:
            return round(values[0], 1)
        return round(statistics.quantiles(values, n=100)[which - 1], 1)

    def close(self):
        self.csv.close()


class SessionBench:
    """The whole run: arrivals, the population, the settle, the summary."""

    def __init__(self, arguments):
        self.arguments = arguments
        parsed = urllib.parse.urlparse(arguments.base)
        self.host, self.port = parsed.hostname, parsed.port or 80
        tour = Tour(os.path.expanduser(arguments.archive))
        self.head_rows, self.body_rows, self.tail_rows = self.get_segments(tour)
        self.accounts = [line.strip() for line in open(arguments.accounts) if line.strip()]
        self.calls = CallLog(f"{arguments.out}_calls.csv")
        self.guard = (SaturationGuard(self, f"{arguments.out}_windows.csv")
                      if arguments.guard else None)
        self.think_trace = self.get_think_trace()
        self.active = set()
        self.active_users = {}
        self.lock = threading.Lock()
        self.entered = 0
        self.left = 0
        self.second_logins = 0
        self.second_logouts = 0
        self.phase = "run"
        self.started_at = time.time()
        body_span = sum(gap for gap, _, _ in self.body_rows)
        nominal = (sum(gap for gap, _, _ in self.head_rows + self.tail_rows)
                   + body_span * max(1, arguments.rounds))
        print(f"tour: {len(tour.exchanges)} calls, nominal {nominal:.0f}s "
              f"at x{arguments.speed} with waits capped at {arguments.max_wait:g}s, "
              f"rounds={arguments.rounds}"
              + (f", churn to peak {arguments.peak}" if arguments.peak else ""))

    def get_think_trace(self):
        """The pauses drawn before the run, one row per user, or None.

        Both legs of a pair read the SAME file, so the pauses are not merely
        reproducible — they are identical, in the same order, on both targets.
        """
        if not self.arguments.think_trace:
            return None
        with open(self.arguments.think_trace) as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        print(f"think trace: {len(rows)} users, "
              f"{len(rows[0]['think_times'])} pauses each, "
              f"from {self.arguments.think_trace}")
        return rows

    def get_segments(self, tour):
        """The tour as three lists of (gap, sequence, record): head, body, tail.

        The gap is the SCHEDULED distance from the previous call, three steps
        per distance: the recorded gap is capped at ``--max-wait`` (on the
        ORIGINAL time), divided by the speed, and never allowed below the
        smallest gap of the recording itself. Segments carry gaps, not offsets,
        so the player can loop the body and re-base the tail freely.
        """
        floor = min(second - first
                    for first, second in zip(tour.offsets, tour.offsets[1:])
                    if second > first)
        rows = []
        previous = None
        for sequence, (offset, record) in enumerate(zip(tour.offsets, tour.exchanges)):
            gap = 0.0
            if previous is not None:
                altered = min(offset - previous, self.arguments.max_wait)
                gap = max(altered / self.arguments.speed, min(altered, floor))
            previous = offset
            rows.append((gap, sequence, record))
        login_end, tail_start = tour.login_end, tour.tail_start
        return (rows[:login_end + 1], rows[login_end + 1:tail_start],
                rows[tail_start:])

    @property
    def population(self):
        return len(self.active)

    def enter(self, user):
        with self.lock:
            self.active.add(user.number)
            self.active_users[user.number] = user
            self.entered += 1
            self.second_logins += 1

    def leave(self, user):
        with self.lock:
            self.active.discard(user.number)
            self.active_users.pop(user.number, None)
            self.left += 1
            self.second_logouts += 1

    def draw_user(self, rng):
        """One random logged user not yet told to leave, or None."""
        with self.lock:
            candidates = [user for user in self.active_users.values()
                          if not user.exit_event.is_set()]
        return rng.choice(candidates) if candidates else None

    def take_movements(self):
        with self.lock:
            logins, self.second_logins = self.second_logins, 0
            logouts, self.second_logouts = self.second_logouts, 0
        return logins, logouts

    def run(self):
        sampler = SecondSampler(self, f"{self.arguments.out}_seconds.csv")
        sampler.start()
        if self.arguments.peak:
            users = self.run_churn()
        else:
            users = self.run_tours()
        for user in users:
            user.join()
        if self.arguments.settle:
            self.phase = "settle"
            print(f"all users out, settling {self.arguments.settle:.0f}s")
            time.sleep(self.arguments.settle)
        sampler.stop_event.set()
        sampler.join()
        self.calls.close()
        if self.guard:
            self.guard.close()
        self.write_summary()
        self.print_summary(users)

    def run_tours(self):
        """The default mode: each user plays his whole tour once and leaves.

        With ``--guard`` the same ramp becomes a capacity ramp: arrivals stop
        when the guard calls saturation, the standing population keeps working
        for ``--hold`` seconds — the window that tells a saturated stack from a
        merely queued one — and only then is everybody asked to play his tail.
        """
        users = []
        available = len(self.think_trace) if self.think_trace else len(self.accounts)
        count = 1 if not self.arguments.login_every else min(
            self.arguments.users, available)
        for number in range(count):
            if self.guard and self.guard.saturated.is_set():
                print(f"rampa fermata a {len(users)} utenti dalla guardia", flush=True)
                break
            if self.memory_stop:
                print(f"rampa fermata a {len(users)} utenti: memoria", flush=True)
                break
            row = self.think_trace[number] if self.think_trace else None
            username = row["username"] if row else self.accounts[number % len(self.accounts)]
            user = EmulatedUser(self, number + 1, username,
                                row["think_times"] if row else ())
            self.enter(user)
            user.start()
            users.append(user)
            if self.arguments.login_every and number + 1 < count:
                self.wait_for_entry(number + 1)
        if self.guard:
            self.hold_population()
            self.stop_users(users)
        return users

    @property
    def memory_stop(self):
        """The reason the memory check stopped this run, or None.

        The check runs outside this process and leaves a file. Reading it here
        is the whole mechanism: no callbacks, no supervisor.
        """
        path = self.arguments.stop_file
        if path and os.path.exists(path):
            with open(path) as handle:
                return handle.read().strip()
        return None

    def write_summary(self):
        """One file saying how the run ended and with which numbers."""
        stop = self.memory_stop
        guard = self.guard
        with open(f"{self.arguments.out}_summary.txt", "w") as handle:
            if stop:
                handle.write(f"ARRESTO DI SICUREZZA (memoria)\n{stop}\n"
                             "la popolazione raggiunta non è una capacità\n")
            elif guard and guard.saturated.is_set():
                handle.write("SATURO: due finestre con p95 oltre il limite\n")
            else:
                handle.write("NON SATURO: la rampa è finita prima\n"
                             "la capacità è almeno la popolazione raggiunta\n")
            if guard:
                handle.write(f"massimo con p95 sotto il limite: {guard.last_under}\n")
                for label, window in (("prima finestra sopra", guard.first_over),
                                      ("seconda consecutiva", guard.second_over)):
                    if window:
                        population, p50, p95, calls, errors = window
                        handle.write(f"{label}: utenti {population}, p50 {p50} ms, "
                                     f"p95 {p95} ms, chiamate {calls}, errori {errors}\n")
        print(open(f"{self.arguments.out}_summary.txt").read(), flush=True)

    def wait_for_entry(self, number):
        """Wait until the next user is due: the trace's clock, or the plain gap."""
        if self.think_trace:
            due = self.started_at + self.think_trace[number]["entry_offset_s"]
            time.sleep(max(0.0, due - time.time()))
        else:
            time.sleep(self.arguments.login_every)

    def hold_population(self):
        """Let whoever is in keep working for the hold, adding nobody."""
        self.phase = "hold"
        print(f"holding {self.population} users for "
              f"{self.arguments.hold:.0f}s", flush=True)
        time.sleep(self.arguments.hold)

    def stop_users(self, users):
        """Raise every user's exit flag: each finishes his round, then his tail."""
        self.phase = "closing"
        for user in users:
            user.exit_event.set()

    def run_churn(self):
        """Churn mode: climb with turnover to the peak, then the closure."""
        rng = random.Random(self.arguments.seed)
        if self.arguments.login_every <= 0:
            raise SystemExit("churn mode needs --login-every > 0")
        self.phase = "climb"
        users = []
        last_entry = last_churn = 0.0
        while self.population < self.arguments.peak:
            now = time.time()
            if now - last_entry >= self.arguments.login_every:
                number = len(users) + 1
                user = EmulatedUser(self, number,
                                    self.accounts[(number - 1) % len(self.accounts)])
                self.enter(user)
                user.start()
                users.append(user)
                last_entry = now
            if now - last_churn >= self.arguments.churn_every:
                drawn = self.draw_user(rng)
                if drawn is not None:
                    drawn.exit_event.set()
                last_churn = now
            time.sleep(0.1)
        self.phase = "drain"
        print(f"peak {self.arguments.peak} reached with {len(users)} entries: closure")
        while True:
            drawn = self.draw_user(rng)
            if drawn is None:
                break
            drawn.exit_event.set()
            time.sleep(self.arguments.drain_every)
        return users

    def print_summary(self, users):
        """The closing lines; the baseline table when the user was one."""
        rows = self.read_call_rows()
        tour_rows = [row for row in rows if row["kind"] == "tour"]
        failures = [row for row in rows if row["ok"] != "True"]
        wall = time.time() - self.started_at
        print(f"\n{len(users)} user(s), {len(tour_rows)} tour calls, "
              f"{len(failures)} failures, {wall:.0f}s of wall clock")
        by_user = {}
        for row in tour_rows:
            by_user.setdefault(int(row["user"]), []).append(float(row["duration_ms"]))
        sums = sorted(sum(values) for values in by_user.values())
        print(f"per-user sum of response ms: min {sums[0]:.0f}  "
              f"p50 {sums[len(sums) // 2]:.0f}  max {sums[-1]:.0f}")
        if len(users) == 1:
            self.print_baseline(tour_rows)
        for row in failures[:10]:
            print(f"  FAIL user {row['user']} seq {row['seq']} {row['label']}: "
                  f"got {row['status']}, expected {row['expected']}")

    def print_baseline(self, tour_rows):
        """The baseline: total and the median of each 5-second bucket."""
        buckets = {}
        for row in tour_rows:
            buckets.setdefault(int(float(row["offset_s"]) // 5), []).append(
                float(row["duration_ms"]))
        print("baseline — median ms per 5s bucket (scheduled time):")
        for bucket in sorted(buckets):
            values = buckets[bucket]
            print(f"  {bucket * 5:>4}-{bucket * 5 + 5:<4}s  n={len(values):<3} "
                  f"p50={statistics.median(values):7.1f} ms  "
                  f"sum={sum(values):8.1f} ms")
        total = sum(float(row["duration_ms"]) for row in tour_rows)
        print(f"  total: {total:.0f} ms over {len(tour_rows)} calls")

    def read_call_rows(self):
        with open(f"{self.arguments.out}_calls.csv") as csv:
            header = csv.readline().strip().split(",")
            return [dict(zip(header, line.strip().split(",")))
                    for line in csv if line.strip()]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", help="the .sqlite of the recorded session")
    parser.add_argument("--base", required=True, help="http://host:port of the stack")
    parser.add_argument("--out", required=True, help="prefix of the two CSVs")
    parser.add_argument("--speed", type=float, default=2.0,
                        help="time coefficient: 2 = double speed (default 2)")
    parser.add_argument("--max-wait", type=float, default=3.0,
                        help="idle cap in seconds, applied to the ORIGINAL "
                             "recording before the speed scaling (default 3)")
    parser.add_argument("--login-every", type=float, default=0.0,
                        help="seconds between two user entries; 0 = single user")
    parser.add_argument("--users", type=int, default=332,
                        help="how many users enter in all (default: every account)")
    parser.add_argument("--rounds", type=int, default=1,
                        help="how many times each user repeats the tour's middle")
    parser.add_argument("--settle", type=float, default=0.0,
                        help="seconds of sampling after the last user leaves")
    parser.add_argument("--peak", type=int, default=0,
                        help="churn mode: climb with turnover to this population, "
                             "then the closure (0 = off, default)")
    parser.add_argument("--churn-every", type=float, default=5.0,
                        help="churn mode: seconds between two drawn exits "
                             "during the climb (default 5)")
    parser.add_argument("--drain-every", type=float, default=3.0,
                        help="churn mode: seconds between two drawn exits "
                             "during the closure (default 3)")
    parser.add_argument("--seed", type=int, default=20260827,
                        help="seed of the draws, so a churn run can be repeated")
    parser.add_argument("--census", help="the inspector census URL, bridge only")
    parser.add_argument("--stop-file",
                        help="file the memory check writes; its presence stops the ramp")
    parser.add_argument("--think-trace",
                        help="JSONL of pauses drawn before the run; both legs read the same one")
    parser.add_argument("--guard", action="store_true",
                        help="watch for saturation and stop the ramp when it comes")
    parser.add_argument("--saturation-window", type=float, default=30.0,
                        help="seconds per judged window (two bad ones in a row stop it)")
    parser.add_argument("--p95-limit", type=float, default=1000.0,
                        help="the p95 in ms past which a window is bad")
    parser.add_argument("--error-limit", type=float, default=1.0,
                        help="the percent of real errors past which a window is bad")
    parser.add_argument("--hold", type=float, default=60.0,
                        help="seconds the standing population keeps working after the ramp stops")
    parser.add_argument("--accounts", default=ACCOUNTS,
                        help="file of usernames, one per line; the tour count "
                             "cannot exceed how many it holds")
    parser.add_argument("--password", default="a")
    arguments = parser.parse_args()
    SessionBench(arguments).run()
