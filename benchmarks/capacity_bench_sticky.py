"""Capacity benchmark THROUGH the sticky proxy — N workers, light-DB load.

Like capacity_bench_record.py (loadRecordCluster, real pkeys), but each user
drives ONE persistent keep-alive TCP connection to the proxy (http.client),
not a fresh connection per request. This is what a browser does, and it is
required by the sticky proxy: the affinity key is the cookie connection_id
when present, else the TCP source (host, port). A client that opens a new TCP
socket per request (urllib) gets a new source port each time -> a different
worker each time -> a new GenroPy connection (new connection_id) each time ->
the pin never settles. A persistent socket keeps tcp:host:port stable so the
first worker is pinned, the connection_id it issues stays coherent, and every
later request lands on that same worker (its in-process register holds the page).

One user = one socket = one server connection, pinned to one worker. With 6
workers and N users, the proxy spreads users across workers; in-process each
worker has its own register -> the load scales with workers.

Stdlib only. Usage:
  python3 capacity_bench_sticky.py --users 12 --duration 30 --base 127.0.0.1:8090
"""

import argparse
import http.client
import json
import re
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie

from replay_a1 import build_plan, load_capture

PAGE_ID_RE = re.compile(rb"page_id:'([A-Za-z0-9_-]{22})'")
CAPTURE = "session_capture.jsonl"
PKEYS_FILE = "cust_pkeys.txt"
CUSTOMER = "/sys/thpage/invc/customer"


def load_lrc_form(capture):
    for line in open(capture):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("rpc_method") == "loadRecordCluster":
            return dict(r["form"])
    raise RuntimeError("no loadRecordCluster in capture")


class StickyClient:
    """One persistent keep-alive connection to the proxy, with a cookie jar."""

    def __init__(self, host, port):
        self.conn = http.client.HTTPConnection(host, port, timeout=60)
        self.cookies: dict[str, str] = {}
        self.alloc = None  # last x-sticky-alloc seen

    def _cookie_header(self):
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def _store_cookies(self, resp):
        for hk, hv in resp.getheaders():
            if hk.lower() == "set-cookie":
                jar = SimpleCookie()
                jar.load(hv)
                for name, morsel in jar.items():
                    self.cookies[name] = morsel.value
            if hk.lower() == "x-sticky-alloc":
                self.alloc = hv

    def get(self, path):
        headers = {"Connection": "keep-alive"}
        if self.cookies:
            headers["Cookie"] = self._cookie_header()
        self.conn.request("GET", path, headers=headers)
        resp = self.conn.getresponse()
        body = resp.read()
        self._store_cookies(resp)
        return resp.status, body

    def post(self, path, form):
        data = urllib.parse.urlencode(form)
        headers = {
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        if self.cookies:
            headers["Cookie"] = self._cookie_header()
        self.conn.request("POST", path, body=data, headers=headers)
        resp = self.conn.getresponse()
        body = resp.read()
        self._store_cookies(resp)
        return resp.status, body

    def close(self):
        self.conn.close()


def page_id_from(html):
    m = PAGE_ID_RE.search(html)
    if not m:
        raise RuntimeError("page_id not found in HTML")
    return m.group(1).decode()


def setup_user(host, port, login_calls, pages, username, password):
    """Login + open customer page over one persistent connection. Return (client, pid)."""
    c = StickyClient(host, port)
    st, html = c.get("/")
    frame = page_id_from(html)
    for form in login_calls:
        f = dict(form)
        if "user" in f:
            f["user"] = username
        if "password" in f:
            f["password"] = password
        f["page_id"] = frame
        st, b = c.post("/", f)
        if b"<error>" in b:
            raise RuntimeError(f"login error: {b[:120]!r}")
    c.get("/")  # reload home after doLogin
    entry = pages[CUSTOMER]
    st, html = c.get(entry["get_path"])
    pid = page_id_from(html)
    main = dict(entry["main"])
    main["page_id"] = pid
    c.post(CUSTOMER, main)
    return c, pid


def run_user(args, login_calls, pages, lrc, pkeys, results, idx, deadline):
    c = None
    try:
        c, pid = setup_user(args.host, args.port, login_calls, pages,
                            args.user, args.password)
        timings = []
        n = 0
        npk = len(pkeys)
        while time.time() < deadline:
            f = dict(lrc)
            f["page_id"] = pid
            f["callcounter"] = str(n + 100)
            f["pkey"] = pkeys[n % npk]
            t = time.time()
            st, body = c.post(CUSTOMER, f)
            dt = time.time() - t
            app_error = b"<error>" in body
            timings.append((dt, st, app_error))
            if app_error:
                raise RuntimeError(f"app error: {body[:140].decode('utf-8', 'replace')}")
            n += 1
        results[idx] = ("OK", c.alloc, timings)
    except Exception as exc:
        results[idx] = ("ERROR", str(exc), [])
    finally:
        if c:
            c.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="127.0.0.1:8090",
                    help="proxy host:port (no scheme)")
    ap.add_argument("--users", type=int, default=1)
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--user", default="amelia.martin")
    ap.add_argument("--password", default="a")
    ap.add_argument("--capture", default=CAPTURE)
    ap.add_argument("--pkeys", default=PKEYS_FILE)
    args = ap.parse_args()
    args.host, _, port = args.base.partition(":")
    args.port = int(port or "8090")

    rows = load_capture(args.capture)
    login_calls, pages = build_plan(rows)
    lrc = load_lrc_form(args.capture)
    pkeys = [line.strip() for line in open(args.pkeys) if line.strip()]
    print(f"sticky bench: {args.users} users (persistent conn), "
          f"{len(pkeys)} pkeys, {args.duration}s -> {args.base}")

    results = [None] * args.users
    threads = []
    t0 = time.time()
    deadline = t0 + args.duration
    for i in range(args.users):
        th = threading.Thread(target=run_user,
                              args=(args, login_calls, pages, lrc, pkeys,
                                    results, i, deadline))
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    wall = time.time() - t0

    ok = [r for r in results if r and r[0] == "OK"]
    errs = [r for r in results if r and r[0] == "ERROR"]
    all_t = [t for r in ok for t in r[2]]
    non200 = [t for t in all_t if t[1] != 200]
    app_errors = [t for t in all_t if t[2]]
    durs = sorted(t[0] for t in all_t)
    allocs = {}
    for r in ok:
        allocs[r[1]] = allocs.get(r[1], 0) + 1

    def pct(p):
        if not durs:
            return 0
        return durs[min(len(durs) - 1, int(len(durs) * p))]

    rps = len(all_t) / wall if wall else 0
    print("\n=== RESULT ===")
    print(f"users OK: {len(ok)}/{args.users}   errors: {len(errs)}")
    print(f"requests: {len(all_t)}   non-200: {len(non200)}   "
          f"app-errors: {len(app_errors)}")
    print(f"worker distribution (users per alloc): {allocs}")
    print(f"wall: {wall:.2f}s   throughput: {rps:.1f} req/s")
    if durs:
        print(f"latency ms  p50={pct(.5)*1000:.0f}  p90={pct(.9)*1000:.0f}  "
              f"p99={pct(.99)*1000:.0f}  max={durs[-1]*1000:.0f}")
    for r in errs[:5]:
        print("  ERROR:", r[1])
    print(f"GRID users={args.users} reqs={len(all_t)} wall={wall:.2f} "
          f"rps={rps:.1f} p50={pct(.5)*1000:.0f} p90={pct(.9)*1000:.0f} "
          f"p99={pct(.99)*1000:.0f} non200={len(non200)} errs={len(errs)} "
          f"apperr={len(app_errors)} nworkers={len(allocs)}")


if __name__ == "__main__":
    main()
