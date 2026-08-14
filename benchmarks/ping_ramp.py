"""Ping ramp: how the authenticated /_ping scales as logged sessions climb.

Levels of concurrency (default 4,8,16,32,64,128): at each level every session
hammers /_ping on its OWN keep-alive connection (http.client, one per thread —
no ephemeral-port exhaustion) for --duration seconds. Sessions are logged in
once (replay harness) and reused across levels; usernames rotate over
usernames.txt so the register sees real distinct users.

Usage (from temp/benchmark/assets):
  PGGSSENCMODE=disable python3 ping_ramp.py --levels 4,8,16,32,64,128 --duration 15
"""

import argparse
import http.client
import statistics
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, ".")
from replay_a1 import User, build_plan, inject_identity, load_capture  # noqa: E402


def pct(ms, q):
    return statistics.quantiles(ms, n=100)[q - 1] if len(ms) > 1 else ms[0]


class PingSession:
    """One logged browser: cookie jar flattened to a header, page_id in hand."""

    def __init__(self, base, login_calls, pages, username, password):
        self.netloc = urllib.parse.urlparse(base).netloc
        u = User(base, login_calls, pages, username, password)
        html = u._get("/")
        self.page_id = u._page_id_from(html)
        for form in login_calls:
            f = inject_identity(form, username, password)
            u._post("/", f, self.page_id, "login")
        u._get("/")
        jar = u.opener.handlers[-1].cookiejar if False else None
        # flatten the jar: urllib keeps it inside the HTTPCookieProcessor
        for handler in u.opener.handlers:
            if hasattr(handler, "cookiejar"):
                jar = handler.cookiejar
        self.cookie = "; ".join(f"{c.name}={c.value}" for c in jar)

    def hammer(self, deadline, out, errors):
        conn = http.client.HTTPConnection(self.netloc, timeout=30)
        local = []
        path = f"/_ping?page_id={self.page_id}"
        while time.time() < deadline:
            t = time.time()
            try:
                conn.request("GET", path, headers={"Cookie": self.cookie})
                resp = conn.getresponse()
                body = resp.read()
                if resp.status != 200 or b"<error>" in body:
                    errors.append(f"status={resp.status} {body[:80]!r}")
                    break
            except Exception as exc:
                errors.append(str(exc))
                conn.close()
                conn = http.client.HTTPConnection(self.netloc, timeout=30)
                continue
            local.append(time.time() - t)
        conn.close()
        out.extend(local)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    ap.add_argument("--levels", default="4,8,16,32,64,128")
    ap.add_argument("--duration", type=float, default=15)
    ap.add_argument("--password", default="a")
    args = ap.parse_args()
    levels = [int(x) for x in args.levels.split(",")]

    rows = load_capture("session_capture.jsonl")
    login_calls, pages = build_plan(rows)
    usernames = [line.strip() for line in open("usernames.txt") if line.strip()]

    sessions = []
    print(f"ping ramp on {args.base} — levels {levels}, {args.duration}s each")
    for level in levels:
        while len(sessions) < level:
            username = usernames[len(sessions) % len(usernames)]
            t0 = time.time()
            try:
                sessions.append(PingSession(args.base, login_calls, pages,
                                            username, args.password))
            except Exception as exc:
                print(f"  login {username} FAILED: {exc}")
                return 1
            if len(sessions) % 16 == 0:
                print(f"  ...{len(sessions)} sessions logged "
                      f"(last login {time.time() - t0:.2f}s)")
        lat, errors = [], []
        deadline = time.time() + args.duration
        outs = [[] for _ in sessions]
        threads = [threading.Thread(target=s.hammer, args=(deadline, outs[i], errors))
                   for i, s in enumerate(sessions)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.time() - t0
        for o in outs:
            lat.extend(o)
        ms = sorted(x * 1000 for x in lat)
        print(f"users={level:4d}  {len(ms):7d} req in {wall:.1f}s -> "
              f"{len(ms) / wall:7.0f} req/s   p50={pct(ms, 50):6.1f}ms  "
              f"p90={pct(ms, 90):6.1f}ms  p99={pct(ms, 99):7.1f}ms"
              + (f"   ERRORS={len(errors)} ({errors[0]})" if errors else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
