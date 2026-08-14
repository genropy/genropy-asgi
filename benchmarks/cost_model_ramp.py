"""Cost-model ramp: real numbers for base + per-user marginal cost on ONE worker.

Phases (against a dedicated commander on --base, workers=1, huge caps):
  0. baseline      -> two occupancy ticks with 0 users: the worker's base cost
  1. gentle ramp   -> add ONE distinct logged user at a time (login + customer
                      page + one real getSelection), wait two ticks, sample the
                      /_server/monitor_state pressure (rss/cpu/executor/counts)
  2. pressure      -> every user hammers getSelection for --duration seconds,
                      sampled every ~5s (saturation numbers)
  3. cooldown      -> two ticks after the burst (memory retention)

Output: CSV rows on stdout + a least-squares fit of rss vs users on the ramp.

Usage:
  python3 cost_model_ramp.py --base http://127.0.0.1:8123 --users 12
"""

import argparse
import json
import subprocess
import time
import threading
import urllib.parse
import urllib.request

from replay_a1 import build_plan, load_capture
from capacity_bench import CUSTOMER, LETTERS, WHERE_LETTER_RE, make_user

USERNAMES = [
    "alexander.king", "amelia.martin", "aria.carter", "ava.robinson",
    "benjamin.lopez", "charlotte.thomas", "chloe.wright", "daniel.scott",
    "ella.lewis", "emily.green", "emma.taylor", "ethan.walker",
    "grace.hall", "hannah.baker", "henry.martinez", "isabella.garcia",
]

TICK_WAIT = 11.0  # > 2 occupancy ticks at the default 5s interval


def worker_pid(address):
    """Resolve the worker process pid from its announced host:port (macOS lsof)."""
    port = address.rsplit(":", 1)[-1]
    out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                         capture_output=True, text=True)
    return int(out.stdout.split()[0]) if out.stdout.strip() else None


def rss_of(pid):
    """RSS in MB via ps (the report's rss is None on macOS: no /proc)."""
    if pid is None:
        return None
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                         capture_output=True, text=True)
    return round(int(out.stdout.strip()) / 1024, 1) if out.stdout.strip() else None


def sample(base):
    """One monitor_state sample -> flat dict of the first worker's readings."""
    with urllib.request.urlopen(base + "/_server/monitor_state", timeout=10) as r:
        state = json.load(r)
    app = next(a for a in state["apps"].values() if a.get("workers"))
    workers = app["workers"]
    row = workers[0]
    pressure = row.get("pressure") or {}
    executor = pressure.get("executor") or {}
    backlog = pressure.get("backlog") or {}
    rss = pressure.get("rss")
    rss_mb = round(rss / (1024 * 1024), 1) if rss else rss_of(worker_pid(row["address"]))
    return {
        "workers": len(workers),
        "surface_users": (app.get("surface") or {}).get("users"),
        "users": pressure.get("users"),
        "active_users": pressure.get("active_users"),
        "connections": pressure.get("connections"),
        "pages": pressure.get("pages"),
        "rss_mb": rss_mb,
        "cpu": round(pressure.get("cpu"), 4) if pressure.get("cpu") is not None else None,
        "busy": executor.get("busy"),
        "queue": executor.get("queue_depth"),
        "outbox": backlog.get("outbox"),
        "load": pressure.get("load"),
    }


def heavy_call(user, pid, form, counter):
    """One real getSelection with a varied where letter; returns elapsed seconds."""
    letter = LETTERS[counter % len(LETTERS)].encode()
    f = dict(form)
    f["page_id"] = pid
    f["callcounter"] = str(counter + 100)
    f["where"] = WHERE_LETTER_RE.sub(
        rb"\g<1>" + letter + rb"\g<2>", form["where"].encode("utf-8")
    ).decode("utf-8")
    data = urllib.parse.urlencode(f).encode("utf-8")
    req = urllib.request.Request(user.opener_base + CUSTOMER, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    t = time.time()
    with user.opener.open(req, timeout=60) as r:
        body = r.read()
    if b"<error>" in body:
        raise RuntimeError(body[:160].decode("utf-8", "replace"))
    return time.time() - t


def hammer(user, pid, form, deadline, timings):
    n = 0
    while time.time() < deadline:
        try:
            timings.append(heavy_call(user, pid, form, n))
        except Exception as exc:  # noqa: BLE001 - a failing user must not stop the bench
            timings.append(None)
            print(f"    hammer error: {exc}")
            return
        n += 1


def emit(tag, row):
    print(f"CSV,{tag}," + ",".join(f"{k}={v}" for k, v in row.items()), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8123")
    ap.add_argument("--users", type=int, default=12)
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--password", default="a")
    ap.add_argument("--capture", default="session_capture.jsonl")
    args = ap.parse_args()

    rows = load_capture(args.capture)
    login_calls, pages = build_plan(rows)

    print(f"[baseline] waiting {TICK_WAIT}s for two occupancy ticks")
    time.sleep(TICK_WAIT)
    base_row = sample(args.base)
    emit("n=0", base_row)

    sessions = []
    ramp = [(0, base_row["rss_mb"])]
    for i, username in enumerate(USERNAMES[: args.users], start=1):
        t0 = time.time()
        try:
            user, pid, h0 = make_user(args.base, login_calls, pages,
                                      username, args.password)
            user.opener_base = args.base
            dt = heavy_call(user, pid, h0, i)
            sessions.append((user, pid, h0))
        except Exception as exc:  # noqa: BLE001 - report and keep ramping
            print(f"[ramp {i}] {username} FAILED: {exc}")
            continue
        time.sleep(TICK_WAIT)
        row = sample(args.base)
        ramp.append((len(sessions), row["rss_mb"]))
        print(f"[ramp {i}] {username} login+query {time.time()-t0-TICK_WAIT:.1f}s "
              f"(getSelection {dt*1000:.0f}ms)")
        emit(f"n={len(sessions)}", row)

    print(f"[pressure] {len(sessions)} users hammering for {args.duration}s")
    deadline = time.time() + args.duration
    all_timings = [[] for _ in sessions]
    threads = [
        threading.Thread(target=hammer, args=(u, p, h, deadline, all_timings[j]))
        for j, (u, p, h) in enumerate(sessions)
    ]
    for th in threads:
        th.start()
    while time.time() < deadline:
        time.sleep(5)
        emit("pressure", sample(args.base))
    for th in threads:
        th.join()
    done = [t for ts in all_timings for t in ts if t is not None]
    if done:
        done.sort()
        total = sum(done)
        print(f"[pressure] {len(done)} calls, avg {total/len(done)*1000:.0f}ms, "
              f"p95 {done[int(len(done)*0.95)]*1000:.0f}ms, "
              f"rps {len(done)/args.duration:.1f}")

    print(f"[cooldown] waiting {TICK_WAIT}s")
    time.sleep(TICK_WAIT)
    emit("cooldown", sample(args.base))

    if len(ramp) >= 3:
        n_mean = sum(n for n, _ in ramp) / len(ramp)
        r_mean = sum(r for _, r in ramp) / len(ramp)
        num = sum((n - n_mean) * (r - r_mean) for n, r in ramp)
        den = sum((n - n_mean) ** 2 for n, _ in ramp)
        slope = num / den if den else 0.0
        print(f"[fit] rss = {r_mean - slope * n_mean:.1f} MB base "
              f"+ {slope:.2f} MB per logged user (ramp, least squares)")


if __name__ == "__main__":
    main()
