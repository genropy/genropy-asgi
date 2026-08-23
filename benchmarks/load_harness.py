"""Load harness for the elastic-pool v1.1 benchmark scenarios (S1-S6).

The pool decides on MEASURED OCCUPANCY, not head count: an idle logged user costs
~0 cpu and ~1 MB, so logging users in moves nothing. Occupancy is driven by WORK
(requests). This harness therefore models each user as a persistent connection that
can generate real getSelection traffic at a controllable per-user rps, and can be
paused/resumed (parked logged-in but idle) so a scenario can make occupancy rise and
fall on demand.

Reused, not reinvented:
- ``StickyClient`` / ``login_user`` / ``inject_identity`` / ``page_id_from`` from
  ``scaling_probe`` (keep-alive connection + cookie jar pinning the worker via the
  spa_connection_id cookie; distinct identities injected into the doLogin Bag XML).
- the real load unit from ``capacity_bench``: POST app.getSelection on the customer
  TH page, varying the ``where`` filter letter each call so every query is fresh
  (~45 KB, 0 errors).
- ``build_plan`` / ``load_capture`` from ``replay_a1``.

Three moving parts:
- ``LoadUser``  -- one thread, one connection, a target-rps work loop with pause/resume.
- ``Fleet``     -- logs users in at a rate, drives rps/pause on subsets, tears down.
- ``MonitorSampler`` -- a thread polling /_server/monitor_state and /_commander/population,
  appending a JSONL timeseries (occupancy/rps/worker-count/population + client-side C/O/H).

macOS note: no /proc rss, so the memory occupancy component is absent; cpu + executor
drive occupancy. Reactions lag 5-35s (sensor 5s, smoothing ~30s over 6 rows): call
``settle`` before asserting.

Stdlib only. Not a CLI itself -- scenarios.py drives it.
"""

import json
import re
import string
import threading
import time
import urllib.request

from replay_a1 import build_plan, load_capture
from scaling_probe import StickyClient, inject_identity, page_id_from

CAPTURE = "session_capture.jsonl"
USERNAMES = "usernames.txt"
CUSTOMER = "/sys/thpage/invc/customer"
# the <c_0 ...>LETTER</c_0> text node inside the captured `where` Bag
WHERE_LETTER_RE = re.compile(r"(>)[a-z](</c_0>)")
LETTERS = string.ascii_lowercase

SETTLE_MIN = 35.0  # smoothing is 6 rows x 5s -> assertions below this are unreliable


class LoadUser:
    """One simulated user: a keep-alive connection running a target-rps work loop.

    The user logs in with a DISTINCT identity, opens the customer TH page, then loops
    POST app.getSelection at ``rps`` requests/second (think-time = 1/rps). ``pause()``
    parks the loop (the user stays logged in on its worker, occupancy decays);
    ``resume()`` restarts it. ``rps`` is mutable at runtime. Every call is timed:
    ``timings`` holds ``(t, dt, status, resp_len, app_error)`` tuples.
    """

    def __init__(self, host, port, login_calls, pages, username, password, rps=1.0):
        self.host = host
        self.port = port
        self.login_calls = login_calls
        self.pages = pages
        self.username = username
        self.password = password
        self.rps = rps
        self.client = None
        self.page_id = None
        self.where_tpl = None
        self.base_form = None
        self.timings = []
        self.error = None
        self._active = threading.Event()
        self._active.set()          # start active; a scenario may pause before login
        self._stop = threading.Event()
        self._thread = None

    def open_session(self):
        """Login (distinct identity) and open the customer page. Blocks until ready."""
        self.client = login_user(self.host, self.port, self.login_calls,
                                 self.username, self.password)
        entry = self.pages[CUSTOMER]
        st, html = self.client.get(entry["get_path"])
        self.page_id = page_id_from(html)
        main = dict(entry["main"])
        main["page_id"] = self.page_id
        self.client.post(CUSTOMER, main)
        heavy0 = dict(entry["heavy"][0])
        self.where_tpl = heavy0["where"]
        self.base_form = heavy0

    def _one_call(self, n):
        letter = LETTERS[n % len(LETTERS)]
        f = dict(self.base_form)
        f["page_id"] = self.page_id
        f["callcounter"] = str(n + 100)
        f["where"] = WHERE_LETTER_RE.sub(r"\g<1>" + letter + r"\g<2>", self.where_tpl)
        t = time.time()
        status, body = self.client.post(CUSTOMER, f)
        dt = time.time() - t
        app_error = b"<error>" in body
        self.timings.append((t, dt, status, len(body), app_error))
        if app_error:
            raise RuntimeError(f"app error for {self.username}: {body[:160]!r}")

    def _run(self):
        try:
            self.open_session()
        except Exception as exc:
            self.error = f"login: {exc}"
            return
        n = 0
        while not self._stop.is_set():
            if not self._active.is_set():
                self._active.wait(timeout=0.5)
                continue
            cycle_start = time.time()
            try:
                self._one_call(n)
            except Exception as exc:
                self.error = str(exc)
                return
            n += 1
            interval = 1.0 / self.rps if self.rps > 0 else 0.5
            elapsed = time.time() - cycle_start
            if elapsed < interval and not self._stop.is_set():
                self._stop.wait(timeout=interval - elapsed)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self):
        self._active.clear()

    def resume(self):
        self._active.set()

    def stop(self):
        self._stop.set()
        self._active.set()
        if self._thread:
            self._thread.join(timeout=5)


class Fleet:
    """Manages a set of LoadUsers: staggered login, rps/pause control, teardown."""

    def __init__(self, base, capture=CAPTURE, usernames_file=USERNAMES, password="a"):
        self.host, _, port = base.partition(":")
        self.port = int(port or "8081")
        self.password = password
        self.login_calls, self.pages = build_plan(load_capture(capture))
        if CUSTOMER not in self.pages:
            raise SystemExit(f"customer page not in capture ({CUSTOMER})")
        self.usernames = [line.strip() for line in open(usernames_file) if line.strip()]
        self.users = []

    def login_all(self, count, rate_r, rps=1.0, active=True):
        """Log ``count`` distinct users in, one every 1/rate_r seconds. Returns them.

        Each user starts its work loop immediately; pass ``active=False`` to have them
        park (logged in, idle) right after opening the session.
        """
        start = len(self.users)  # advance the cursor: each call takes the NEXT slice,
        end = start + count      # so repeated login_all(1) gets DISTINCT identities
        if end > len(self.usernames):
            raise SystemExit(
                f"need {end} usernames, have {len(self.usernames)} (Trap 1: distinct "
                f"identities — same username collapses to one user)")
        launched = []
        for username in self.usernames[start:end]:
            u = LoadUser(self.host, self.port, self.login_calls, self.pages,
                         username, self.password, rps=rps)
            if not active:
                u.pause()
            u.start()
            self.users.append(u)
            launched.append(u)
            time.sleep(1.0 / rate_r if rate_r > 0 else 0)
        return launched

    def set_rps(self, users, rps):
        for u in (users or self.users):
            u.rps = rps

    def pause(self, users):
        for u in users:
            u.pause()

    def resume(self, users):
        for u in users:
            u.resume()

    def pause_all(self):
        self.pause(self.users)

    def errors(self):
        return [(u.username, u.error) for u in self.users if u.error]

    def stop_all(self):
        for u in self.users:
            u.stop()


class MonitorSampler:
    """Polls the commander observables into a JSONL timeseries on a background thread.

    Each sample: from /_server/monitor_state -> per-worker occupancy/components/rates/
    forward + tracked worker count; from /_commander/population -> per-user worker
    placement + consumption. Recomputes the capacity ledger C/O/H client-side using the
    known thresholds so a scenario can see the compaction trigger cross.
    """

    def __init__(self, base, out_path, reception_threshold=0.5,
                 admission_threshold=0.8, compaction_margin=1.5, interval=2.0):
        self.base = base
        self.out_path = out_path
        self.reception_threshold = reception_threshold
        self.admission_threshold = admission_threshold
        self.compaction_margin = compaction_margin
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.samples = []

    def _get_json(self, path):
        with urllib.request.urlopen(f"http://{self.base}{path}", timeout=10) as r:
            return json.load(r)

    def _app_node(self, state):
        apps = state.get("apps", {})
        mount = next((k for k in apps if k != "_server"), None)
        return apps.get(mount, {})

    def sample(self):
        ts = time.time()
        row = {"ts": ts}
        try:
            state = self._get_json("/_server/monitor_state")
            app = self._app_node(state)
            groups = app.get("groups", {})
            tracked = sum(g.get("workers", 0) for g in groups.values())
            metrics = app.get("metrics", {})
            per_worker = {}
            occ_sum = 0.0
            for name, m in metrics.items():
                occ = m.get("occupancy", 0)
                per_worker[name] = {
                    "occ": occ,
                    "components": m.get("components", {}),
                    "rps": m.get("rates", {}).get("rps"),
                    "lat": m.get("rates", {}).get("latency_ms"),
                    "forward": m.get("forward", {}),
                }
                occ_sum += occ / 100.0
            n = len(metrics)
            capacity = (self.reception_threshold
                        + max(0, n - 1) * self.admission_threshold)
            row.update({
                "tracked": tracked,
                "routable": n,
                "workers": sorted(metrics.keys()),
                "per_worker": per_worker,
                "surface": app.get("surface", {}),
                "C": round(capacity, 3),
                "O": round(occ_sum, 3),
                "H": round(capacity - occ_sum, 3),
                "compact_trigger": round(self.compaction_margin
                                         * self.admission_threshold, 3),
            })
        except Exception as exc:
            row["monitor_error"] = str(exc)
        try:
            pop = self._get_json("/_commander/population")
            placement = {}
            for w in pop.get("workers", []):
                wid = w.get("id")
                for user in w.get("users", []):
                    placement[user.get("user")] = {
                        "worker": wid,
                        "consumption": user.get("consumption"),
                    }
            row["placement"] = placement
        except Exception as exc:
            row["population_error"] = str(exc)
        return row

    def _run(self):
        with open(self.out_path, "a") as fh:
            while not self._stop.is_set():
                row = self.sample()
                self.samples.append(row)
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                self._stop.wait(timeout=self.interval)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


def login_user(host, port, login_calls, username, password):
    """Login over one persistent connection; return the client (kept open).

    Local copy of scaling_probe.login_user to avoid importing its argparse main; the
    identity is injected in BOTH the flat user/password fields and the doLogin Bag XML.

    The first GET / on a freshly spawned worker can 500 (cold-start race, genro-asgi
    #46): the body then has no page_id. A single re-GET after a short pause absorbs it
    — this is test robustness (a worker spawned mid-scenario is born cold), NOT the fix
    for #46, which is genro-asgi's to make.
    """
    c = StickyClient(host, port)
    st, html = c.get("/")
    try:
        frame = page_id_from(html)
    except RuntimeError:
        time.sleep(0.3)
        st, html = c.get("/")
        frame = page_id_from(html)
    for form in login_calls:
        f = inject_identity(form, username, password)
        f["page_id"] = frame
        st, body = c.post("/", f)
        if b"<error>" in body:
            raise RuntimeError(f"login error for {username}: {body[:160]!r}")
    c.get("/")  # reload home after doLogin (what the browser does)
    return c


def wait_pool_ready(base, timeout=90.0):
    """Poll /_server/monitor_state until the app has >=1 routable worker (metrics)."""
    deadline = time.time() + timeout
    sampler = MonitorSampler(base, "/dev/null")
    while time.time() < deadline:
        try:
            state = sampler._get_json("/_server/monitor_state")
            app = sampler._app_node(state)
            if len(app.get("metrics", {})) >= 1:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    raise RuntimeError(f"pool not ready within {timeout}s at {base}")


def settle(seconds, label="", fast=False):
    """Sleep to let the smoothed occupancy catch up before asserting."""
    wait = seconds if not fast else max(5.0, seconds / 3)
    if fast and seconds >= SETTLE_MIN:
        print(f"  [settle {label}] FAST mode: {wait:.0f}s (<{SETTLE_MIN:.0f}s "
              f"-> assertions unreliable)")
    else:
        print(f"  [settle {label}] {wait:.0f}s")
    time.sleep(wait)
