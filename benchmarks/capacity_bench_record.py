"""Capacity benchmark for ONE worker — light-DB / register-visible load.

Same harness as capacity_bench.py but the unit of work is loadRecordCluster
(read ONE customer record by pkey) instead of getSelection (a 100-row grid
with relation JOINs). Rationale (decided with the user): getSelection on
14 relation columns spends ~69ms in SQL (JOINs) which dominates and MASKS the
register cost; reading a single record is ~5ms SQL — the DB is touched but not
predominant, so the register cost (~56 calls/request, the in-process vs daemon
difference) becomes VISIBLE.

Each user = one HTTP session (cookie jar) = one server connection, its OWN
page_id (no per-page lock contention across threads). The loop reads a record
per pkey, cycling through real pkeys (loaded from cust_pkeys.txt) so every read
hits a different row (no full caching) and is faithful (the pkey exists).

No think-time: hammer to saturation. Ramp concurrency, repeat per register mode.

Stdlib only. Usage:
  python3 capacity_bench_record.py --users 8 --duration 30 --base http://127.0.0.1:8099
"""

import argparse
import json
import threading
import time
import urllib.parse
import urllib.request

from replay_a1 import User, build_plan, load_capture

CAPTURE = "session_capture.jsonl"
PKEYS_FILE = "cust_pkeys.txt"
CUSTOMER = "/sys/thpage/invc/customer"


def load_lrc_form(capture):
    """Return the captured loadRecordCluster form dict (table=invc.customer)."""
    for line in open(capture):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("rpc_method") == "loadRecordCluster":
            return dict(r["form"])
    raise RuntimeError("no loadRecordCluster in capture")


def make_user(base, login_calls, pages, username, password):
    """Authenticate, open the customer page, return (User, page_id)."""
    u = User(base, login_calls, pages, username, password)
    html = u._get("/")
    frame = u._page_id_from(html)
    for form in login_calls:
        f = dict(form)
        if "user" in f:
            f["user"] = username
        if "password" in f:
            f["password"] = password
        u._post("/", f, frame, "login")
    u._get("/")
    entry = pages[CUSTOMER]
    html = u._get(entry["get_path"])
    pid = u._page_id_from(html)
    u._post(CUSTOMER, entry["main"], pid, "main")
    return u, pid


def run_user(args, login_calls, pages, lrc, pkeys, results, idx, deadline):
    u = None
    try:
        u, pid = make_user(args.base, login_calls, pages, args.user, args.password)
        n = 0
        npk = len(pkeys)
        while time.time() < deadline:
            pk = pkeys[n % npk]
            f = dict(lrc)
            f["page_id"] = pid
            f["callcounter"] = str(n + 100)
            f["pkey"] = pk
            data = urllib.parse.urlencode(f).encode("utf-8")
            req = urllib.request.Request(args.base + CUSTOMER,
                                         data=data, method="POST")
            req.add_header("Content-Type",
                           "application/x-www-form-urlencoded; charset=UTF-8")
            t = time.time()
            with u.opener.open(req, timeout=60) as r:
                body = r.read()
                status = r.status
            dt = time.time() - t
            app_error = b"<error>" in body
            u.timings.append(("loadRecord", dt, status, len(body), app_error))
            if app_error:
                raise RuntimeError(
                    f"app error: {body[:160].decode('utf-8', 'replace')}")
            n += 1
    except Exception as exc:
        results[idx] = ("ERROR", str(exc), u.timings if u else [])
        return
    results[idx] = ("OK", None, u.timings)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    ap.add_argument("--users", type=int, default=1)
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--user", default="amelia.martin")
    ap.add_argument("--password", default="a")
    ap.add_argument("--capture", default=CAPTURE)
    ap.add_argument("--pkeys", default=PKEYS_FILE)
    args = ap.parse_args()

    rows = load_capture(args.capture)
    login_calls, pages = build_plan(rows)
    lrc = load_lrc_form(args.capture)
    pkeys = [line.strip() for line in open(args.pkeys) if line.strip()]
    if CUSTOMER not in pages:
        print("customer page not in capture")
        return
    print(f"capacity bench (loadRecordCluster): {args.users} users, "
          f"{len(pkeys)} pkeys, hammer for {args.duration}s")

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

    ok = sum(1 for r in results if r and r[0] == "OK")
    errs = [r for r in results if r and r[0] == "ERROR"]
    all_t = [t for r in results if r for t in r[2]]
    load_t = [t for t in all_t if t[0] == "loadRecord"]
    non200 = [t for t in load_t if t[2] != 200]
    app_errors = [t for t in all_t if len(t) > 4 and t[4]]
    durs = sorted(t[1] for t in load_t)

    def pct(p):
        if not durs:
            return 0
        return durs[min(len(durs) - 1, int(len(durs) * p))]

    rps = len(load_t) / wall if wall else 0
    print("\n=== RESULT ===")
    print(f"users OK: {ok}/{args.users}   errors: {len(errs)}")
    print(f"loadRecord calls: {len(load_t)}   non-200: {len(non200)}   "
          f"app-errors(<error>): {len(app_errors)}")
    print(f"wall: {wall:.2f}s   throughput: {rps:.1f} req/s")
    if durs:
        print(f"latency ms  p50={pct(.5)*1000:.0f}  p90={pct(.9)*1000:.0f}  "
              f"p99={pct(.99)*1000:.0f}  max={durs[-1]*1000:.0f}")
    for r in errs[:5]:
        print("  ERROR:", r[1])
    print(f"GRID users={args.users} reqs={len(load_t)} wall={wall:.2f} "
          f"rps={rps:.1f} p50={pct(.5)*1000:.0f} p90={pct(.9)*1000:.0f} "
          f"p99={pct(.99)*1000:.0f} non200={len(non200)} errs={len(errs)} "
          f"apperr={len(app_errors)}")


if __name__ == "__main__":
    main()
