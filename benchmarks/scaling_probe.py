"""Scaling probe — N DISTINCT users log in sequentially, watch the pool grow.

Each user gets one persistent keep-alive connection (the sticky_cid cookie pins
the worker). Credentials are injected BOTH in the flat user=/password= fields
(login_checkAvatar) AND inside the Bag XML of the `login` parameter of
login_doLogin (<user>...</user> / <password>...</password>) — the flat
replacement alone leaves everybody logged in as the captured user.

After every login the probe samples /_server/monitor_state and waits until no
worker is booting (spawn in flight) before the next login, so the placement
matches the sequential model instead of stacking logins on the last full worker.

Run from temp/benchmark/assets with the pool on 8081:
  PYTHONPATH=. python3 scaling_probe.py --users 15 --base 127.0.0.1:8081
"""

import argparse
import http.client
import json
import re
import time
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie

from replay_a1 import build_plan, load_capture

PAGE_ID_RE = re.compile(rb"page_id:'([A-Za-z0-9_-]{22})'")
BAG_USER_RE = re.compile(r"<user>[^<]*</user>")
BAG_PASSWORD_RE = re.compile(r"<password>[^<]*</password>")
CAPTURE = "session_capture.jsonl"
USERNAMES = "usernames.txt"


class StickyClient:
    """One persistent keep-alive connection to the commander, with a cookie jar."""

    def __init__(self, host, port):
        self.conn = http.client.HTTPConnection(host, port, timeout=60)
        self.cookies: dict[str, str] = {}

    def _headers(self):
        headers = {"Connection": "keep-alive"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return headers

    def _store_cookies(self, resp):
        for hk, hv in resp.getheaders():
            if hk.lower() == "set-cookie":
                jar = SimpleCookie()
                jar.load(hv)
                for name, morsel in jar.items():
                    self.cookies[name] = morsel.value

    def get(self, path):
        self.conn.request("GET", path, headers=self._headers())
        resp = self.conn.getresponse()
        body = resp.read()
        self._store_cookies(resp)
        return resp.status, body

    def post(self, path, form):
        data = urllib.parse.urlencode(form)
        headers = self._headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        self.conn.request("POST", path, body=data, headers=headers)
        resp = self.conn.getresponse()
        body = resp.read()
        self._store_cookies(resp)
        return resp.status, body


def page_id_from(html):
    m = PAGE_ID_RE.search(html)
    if not m:
        raise RuntimeError("page_id not found in HTML")
    return m.group(1).decode()


def inject_identity(form, username, password):
    """Return a copy of *form* with the credentials in every place they live."""
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


def login_user(host, port, login_calls, username, password):
    """Login over one persistent connection; return the client (kept open)."""
    c = StickyClient(host, port)
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


def monitor_pool(base):
    """Return (workers, tracked) from /_server/monitor_state.

    workers: [(worker_id, status, users), ...] — the ROUTABLE workers.
    tracked: the pool's tracked children count (groups.default.workers), which
    includes a child still booting — tracked > len(workers) is a spawn in flight.
    """
    with urllib.request.urlopen(f"http://{base}/_server/monitor_state", timeout=10) as r:
        state = json.load(r)
    app = state["apps"][""]
    workers = [(w["id"], w["status"], w["users"]) for w in app["workers"]]
    tracked = sum(g["workers"] for g in app["groups"].values())
    return workers, tracked


def wait_pool_settled(base, timeout=60.0):
    """Wait until no spawn is in flight: tracked == routable, all running.

    The worker boot (a full GnrWsgiSite) takes seconds; logging in faster than
    the spawn stacks every login on the last full worker and hides the scaling.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        workers, tracked = monitor_pool(base)
        routable_running = [w for w in workers if w[1] == "running"]
        if tracked == len(routable_running) == len(workers):
            return workers
        time.sleep(0.5)
    raise RuntimeError(f"pool not settled within {timeout}s: {monitor_pool(base)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="127.0.0.1:8081", help="commander host:port")
    ap.add_argument("--users", type=int, default=15)
    ap.add_argument("--password", default="a")
    ap.add_argument("--capture", default=CAPTURE)
    ap.add_argument("--usernames", default=USERNAMES)
    args = ap.parse_args()
    host, _, port = args.base.partition(":")
    port = int(port or "8081")

    usernames = [line.strip() for line in open(args.usernames) if line.strip()][: args.users]
    if len(usernames) < args.users:
        raise SystemExit(f"only {len(usernames)} usernames for --users {args.users}")
    login_calls, _pages = build_plan(load_capture(args.capture))

    print(f"scaling probe: {args.users} distinct users -> {args.base}")
    print(f"start: {monitor_pool(args.base)}")
    clients = []
    for i, username in enumerate(usernames, 1):
        wait_pool_settled(args.base)  # never log in while a spawn is in flight
        clients.append(login_user(host, port, login_calls, username, args.password))
        workers, tracked = monitor_pool(args.base)
        dist = "  ".join(f"{wid}={users}" for wid, _, users in workers)
        spawn = f"  (spawn in flight: {tracked - len(workers)})" if tracked > len(workers) else ""
        print(f"[{i:2d}] {username:<22} workers={len(workers)}  {dist}{spawn}")

    workers = wait_pool_settled(args.base)
    print("\n=== RESULT ===")
    print(f"users logged in: {len(clients)}")
    print(f"workers: {len(workers)}")
    for wid, status, users in workers:
        print(f"  {wid}: status={status} users={users}")


if __name__ == "__main__":
    main()
