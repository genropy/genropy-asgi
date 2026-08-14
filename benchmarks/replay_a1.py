"""A1 faithful replay benchmark for the GenroPy in-process vs daemon register.

Model (mirrors the real browser):
- one HTTP session (cookie jar) per simulated user  == one server connection
- the "frame" page (GET /) only authenticates the connection (login)
- the real work lives in TH pages opened as iframes (/sys/thpage/invc/<table>),
  each with its OWN page_id minted by the server in the returned HTML.

For each user we:
  1. GET /                         -> frame page_id
  2. login_checkAvatar + login_doLogin (replayed from the capture, frame page_id)
  3. for each captured TH page (iframe):
       GET /sys/thpage/...         -> iframe page_id (from HTML)
       POST main                   -> server builds the page contexts
       POST app.getSelection (xN)  -> the heavy query/selection calls

page_id and callcounter are re-minted per user; every other form value is taken
verbatim from the capture, so the server does the real work on real contexts.

Heavy reads only (A1): we replay main + app.getSelection (the bulk of the load),
not loadRecordCluster/saveRecordCluster (those need per-session pkeys -> A2).

Stdlib only. Usage:
  python3 replay_a1.py --users 10 --rounds 1 --base http://127.0.0.1:8099
"""

import argparse
import http.cookiejar
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict

PAGE_ID_RE = re.compile(r"page_id:'([A-Za-z0-9_-]{22})'")
CAPTURE = "session_capture.jsonl"

# form keys whose value is the per-page identity; re-minted per user, never replayed
IDENTITY_KEYS = {"page_id", "callcounter", "_calling_page_id",
                 "_parent_page_id", "_root_page_id"}

# login_doLogin carries the identity INSIDE the `login` Bag XML, not as flat
# form keys: replaying the captured form verbatim logs every session in as the
# captured user, whatever username the harness passes.
BAG_USER_RE = re.compile(r"<user>[^<]*</user>")
BAG_PASSWORD_RE = re.compile(r"<password>[^<]*</password>")


def inject_identity(form, username, password):
    """Return a copy of *form* with the credentials in every place they live.

    login_checkAvatar has flat user=/password= keys; login_doLogin has them
    inside the `login` Bag XML. Both get rewritten; no key is ever ADDED — a
    spurious password= on doLogin crashes getAvatar() with multiple values.
    """
    f = dict(form)
    if "user" in f:
        f["user"] = username
    if "password" in f:
        f["password"] = password
    if "login" in f:  # the doLogin Bag XML carries identity inside the payload
        bag = BAG_USER_RE.sub(f"<user>{username}</user>", f["login"])
        bag = BAG_PASSWORD_RE.sub(f"<password>{password}</password>", bag)
        f["login"] = bag
    return f


def load_capture(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def build_plan(rows):
    """Return (login_calls, pages) extracted from the capture.

    login_calls: list of form dicts for the two login pageCalls (frame page).
    pages: OrderedDict { th_path_with_query : {"get_path":..., "main": form,
            "heavy": [forms of app.getSelection]} }
    """
    posts = [r for r in rows if r.get("rpc_method") and r["method"] == "POST"]
    gets = [r for r in rows if r["method"] == "GET"]

    login_calls = [r["form"] for r in posts if "login" in (r["rpc_method"] or "").lower()]

    # map TH iframe GET requests by table path (strip query for grouping key)
    th_gets = OrderedDict()
    for r in gets:
        p = r["path"]
        if p.startswith("/sys/thpage/") or p == "/sys/lookup_page":
            base = p.split("?")[0]
            th_gets.setdefault(base, r["path"])  # keep first (with query) for replay

    pages = OrderedDict()
    for r in posts:
        base = r["path"].split("?")[0]
        if not (base.startswith("/sys/thpage/") or base == "/sys/lookup_page"):
            continue
        m = r["rpc_method"]
        entry = pages.setdefault(base, {"get_path": th_gets.get(base, base),
                                        "main": None, "heavy": []})
        if m == "main":
            entry["main"] = r["form"]
        elif m == "app.getSelection":
            entry["heavy"].append(r["form"])
    # keep only pages that actually have a main and at least one heavy call
    return login_calls, OrderedDict(
        (k, v) for k, v in pages.items() if v["main"] and v["heavy"]
    )


class User:
    def __init__(self, base, login_calls, pages, user, password):
        self.base = base.rstrip("/")
        self.login_calls = login_calls
        self.pages = pages
        self.user = user
        self.password = password
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        self.counter = 0
        self.timings = []  # (label, seconds, status, resp_len)

    def _next_cc(self):
        self.counter += 1
        return str(self.counter)

    def _get(self, path):
        t = time.time()
        with self.opener.open(self.base + path, timeout=60) as r:
            body = r.read()
            status = r.status
        dt = time.time() - t
        self.timings.append(("GET " + path.split("?")[0], dt, status, len(body), False))
        return body.decode("utf-8", "replace")

    def _post(self, path, form, page_id, label):
        f = dict(form)
        # re-mint identity values
        if "page_id" in f:
            f["page_id"] = page_id
        if "callcounter" in f:
            f["callcounter"] = self._next_cc()
        data = urllib.parse.urlencode(f).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        req.add_header("Content-Type",
                       "application/x-www-form-urlencoded; charset=UTF-8")
        t = time.time()
        with self.opener.open(req, timeout=60) as r:
            body = r.read()
            status = r.status
        dt = time.time() - t
        # A 200 can still be a GenroPy error envelope (expired / forbidden /
        # server_exception). Detect by CONTENT, not by length: <error>…</error>
        # is ~186 bytes and would slip past any size threshold.
        app_error = b"<error>" in body
        self.timings.append((label, dt, status, len(body), app_error))
        if app_error and not label.startswith("login"):
            raise RuntimeError(
                f"app error in {label}: {body[:160].decode('utf-8', 'replace')}")
        return body

    def _page_id_from(self, html):
        m = PAGE_ID_RE.search(html)
        if not m:
            raise RuntimeError("page_id not found in HTML")
        return m.group(1)

    def run_once(self):
        # 1. frame page
        html = self._get("/")
        frame_pid = self._page_id_from(html)
        # 2. login (replay captured forms with frame page_id, identity rewritten
        # both in the flat keys and inside the doLogin `login` Bag).
        for form in self.login_calls:
            f = inject_identity(form, self.user, self.password)
            self._post("/", f, frame_pid, "login:" + f.get("method", "")[-20:])
        # reload home after login so the server re-issues the authenticated
        # connection cookie (the browser does GET / right after doLogin)
        self._get("/")
        # 3. each TH iframe
        for base, entry in self.pages.items():
            html = self._get(entry["get_path"])
            pid = self._page_id_from(html)
            self._post(base, entry["main"], pid, "main " + base)
            for hv in entry["heavy"]:
                self._post(base, hv, pid, "getSelection " + base)


def run_user(args, login_calls, pages, username, results, idx, deadline=None):
    u = User(args.base, login_calls, pages, username, args.password)
    try:
        if deadline is not None:
            # duration-based: keep replaying rounds until the deadline
            while time.time() < deadline:
                u.run_once()
        else:
            for _ in range(args.rounds):
                u.run_once()
    except Exception as exc:
        results[idx] = ("ERROR", str(exc), u.timings)
        return
    results[idx] = ("OK", None, u.timings)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    ap.add_argument("--users", type=int, default=1)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--duration", type=float, default=0,
                    help="if >0, each user replays rounds until N seconds elapse")
    ap.add_argument("--user", default="amelia.martin")
    ap.add_argument("--password", default="a")
    ap.add_argument("--capture", default=CAPTURE)
    args = ap.parse_args()

    rows = load_capture(args.capture)
    login_calls, pages = build_plan(rows)
    calls_per_round = (len(login_calls)
                       + sum(1 + len(p["heavy"]) for p in pages.values())
                       + len(pages))  # +GET per page
    print(f"plan: {len(pages)} TH pages, {len(login_calls)} login calls, "
          f"~{calls_per_round} reqs/round/user")
    print(f"running {args.users} users x {args.rounds} rounds against {args.base}")

    results = [None] * args.users
    threads = []
    t0 = time.time()
    deadline = (t0 + args.duration) if args.duration > 0 else None
    for i in range(args.users):
        th = threading.Thread(target=run_user,
                              args=(args, login_calls, pages, args.user, results, i,
                                    deadline))
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    wall = time.time() - t0

    ok = sum(1 for r in results if r and r[0] == "OK")
    errs = [r for r in results if r and r[0] == "ERROR"]
    all_t = [t for r in results if r for t in r[2]]
    total_reqs = len(all_t)
    non200 = [t for t in all_t if t[2] != 200]
    # real validity check: <error> in the response body (content, not length)
    app_errors = [t for t in all_t if len(t) > 4 and t[4]]
    durs = sorted(t[1] for t in all_t)

    def pct(p):
        if not durs:
            return 0
        return durs[min(len(durs) - 1, int(len(durs) * p))]

    print("\n=== RESULT ===")
    print(f"users OK: {ok}/{args.users}   errors: {len(errs)}")
    print(f"total requests: {total_reqs}   non-200: {len(non200)}   "
          f"app-errors(<error>): {len(app_errors)}")
    print(f"wall: {wall:.2f}s   throughput: {total_reqs / wall:.1f} req/s")
    if durs:
        print(f"latency ms  p50={pct(.5)*1000:.0f}  p90={pct(.9)*1000:.0f}  "
              f"p99={pct(.99)*1000:.0f}  max={durs[-1]*1000:.0f}")
    for r in errs[:5]:
        print("  ERROR:", r[1])
    if non200[:3]:
        print("  non-200 sample:", [(t[0], t[2]) for t in non200[:3]])
    # compact machine-parsable line for grid aggregation
    print(f"GRID users={args.users} reqs={total_reqs} wall={wall:.2f} "
          f"rps={total_reqs / wall:.1f} p50={pct(.5)*1000:.0f} "
          f"p90={pct(.9)*1000:.0f} non200={len(non200)} errs={len(errs)} "
          f"apperr={len(app_errors)}")


if __name__ == "__main__":
    main()
