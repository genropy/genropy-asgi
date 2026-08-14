"""Framework-floor benchmark: no DB, no page logic.

Two rungs, hammered with N concurrent sessions for --duration seconds each:
  metrics : GET /metrics            -> asgi core alone (no legacy site)
  ping    : GET /_ping?page_id=...  -> full stack + register chain, empty envelope

The ping rung logs real users in first (replay harness), so every ping crosses
the authenticated register path exactly like the browser's polling.

Usage: python3 floor_bench.py --users 4 --duration 20 --base http://127.0.0.1:8099
(run from temp/benchmark/assets so replay_a1 and the capture are importable)
"""

import argparse
import statistics
import sys
import threading
import time
import urllib.request

sys.path.insert(0, ".")
from replay_a1 import User, build_plan, load_capture  # noqa: E402


def pct(values, q):
    return statistics.quantiles(values, n=100)[q - 1] if len(values) > 1 else values[0]


def hammer(fn, n_threads, duration):
    """Run fn() in n_threads for duration seconds; return (count, latencies)."""
    latencies = []
    lock = threading.Lock()
    deadline = time.time() + duration
    errors = []

    def loop():
        local = []
        while time.time() < deadline:
            t = time.time()
            try:
                fn()
            except Exception as exc:  # count and stop the thread
                errors.append(str(exc))
                break
            local.append(time.time() - t)
        with lock:
            latencies.extend(local)

    threads = [threading.Thread(target=loop) for _ in range(n_threads)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0
    return latencies, wall, errors


def report(label, latencies, wall, errors):
    n = len(latencies)
    ms = sorted(x * 1000 for x in latencies)
    print(f"{label}: {n} req in {wall:.1f}s -> {n / wall:.0f} req/s   "
          f"p50={pct(ms, 50):.1f}ms p90={pct(ms, 90):.1f}ms p99={pct(ms, 99):.1f}ms"
          + (f"   ERRORS={len(errors)} ({errors[0]})" if errors else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    ap.add_argument("--users", type=int, default=4)
    ap.add_argument("--duration", type=float, default=20)
    ap.add_argument("--user", default="amelia.martin")
    ap.add_argument("--password", default="a")
    args = ap.parse_args()

    # rung 1: /metrics — asgi core alone, no session needed
    def get_metrics():
        with urllib.request.urlopen(args.base + "/metrics", timeout=30) as r:
            r.read()

    latencies, wall, errors = hammer(get_metrics, args.users, args.duration)
    report("metrics (core asgi)  ", latencies, wall, errors)

    # rung 2: /_ping — one logged session per thread, the browser's poll call
    rows = load_capture("session_capture.jsonl")
    login_calls, pages = build_plan(rows)
    sessions = []
    for _ in range(args.users):
        u = User(args.base, login_calls, pages, args.user, args.password)
        html = u._get("/")
        pid = u._page_id_from(html)
        for form in login_calls:
            f = dict(form)
            if "user" in f:
                f["user"] = args.user
            if "password" in f:
                f["password"] = args.password
            u._post("/", f, pid, "login")
        u._get("/")
        sessions.append((u, pid))

    counter = threading.local()

    def ping():
        u, pid = sessions[int(threading.current_thread().name.split("-")[-1]) % len(sessions)] \
            if False else getattr(counter, "session")
        req = urllib.request.Request(f"{args.base}/_ping?page_id={pid}")
        with u.opener.open(req, timeout=30) as r:
            body = r.read()
        if b"<error>" in body:
            raise RuntimeError(body[:120].decode("utf-8", "replace"))

    # bind one session per thread deterministically
    latencies = []
    lock = threading.Lock()
    deadline = time.time() + args.duration
    errors = []

    def loop(session):
        counter.session = session
        local = []
        while time.time() < deadline:
            t = time.time()
            try:
                ping()
            except Exception as exc:
                errors.append(str(exc))
                break
            local.append(time.time() - t)
        with lock:
            latencies.extend(local)

    threads = [threading.Thread(target=loop, args=(s,)) for s in sessions]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0
    report("ping (stack, no DB)  ", latencies, wall, errors)


if __name__ == "__main__":
    main()
