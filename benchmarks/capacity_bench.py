"""Capacity benchmark for ONE worker: how many realistic users can it host
without an external daemon? (in-process vs daemon register.)

Each simulated user = one HTTP session (cookie jar) = one server connection:
  1. GET /                 -> frame page_id
  2. login (replayed)      -> authenticate the connection
  3. GET customer iframe   -> live page_id
  4. POST main             -> build the page contexts
  5. loop POST app.getSelection on *V_invc_customer, varying the `where`
     filter letter each call (a..z) so every query is fresh (no cache),
     returns 100 real rows (~45 KB) -> real DB + Bag work, repeatable,
     0 errors (unlike the blind 15-heavy replay whose @rows calls fail).

No think-time: hammer to saturation. Ramp concurrency, repeat per register
mode, derive user capacity = saturation_rps / per_user_rate.

Why only getSelection #0: the captured session has 15 heavy calls but only
#0 (the main customer query) is faithful & repeatable; the invoice subquery
calls depend on a selected pkey not reconstructed in replay (row_count=0) and
#5/#14 reference an ephemeral @rows selection (gnrexception). #0 with a varied
`where` is the clean register+DB-intensive unit of work.

Stdlib only. Usage:
  python3 capacity_bench.py --users 8 --duration 30 --base http://127.0.0.1:8099
"""

import argparse
import re
import string
import threading
import time
import urllib.parse
import urllib.request

from replay_a1 import User, build_plan, load_capture

CAPTURE = "session_capture.jsonl"
CUSTOMER = "/sys/thpage/invc/customer"
# the <c_0 ...>LETTER</c_0> text node inside the captured `where` Bag
WHERE_LETTER_RE = re.compile(rb"(>)[a-z](</c_0>)")
LETTERS = string.ascii_lowercase


def make_user(base, login_calls, pages, username, password):
    """Authenticate, open the customer page, return (User, page_id, heavy0_form)."""
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
    u._get("/")  # browser reloads home after doLogin (re-issues auth cookie)
    entry = pages[CUSTOMER]
    html = u._get(entry["get_path"])
    pid = u._page_id_from(html)
    u._post(CUSTOMER, entry["main"], pid, "main")
    return u, pid, dict(entry["heavy"][0])


def run_user(args, login_calls, pages, results, idx, deadline):
    u = None
    try:
        u, pid, h0 = make_user(args.base, login_calls, pages,
                               args.user, args.password)
        where_tpl = h0["where"].encode("utf-8")
        n = 0
        while time.time() < deadline:
            letter = LETTERS[n % len(LETTERS)].encode()
            f = dict(h0)
            f["page_id"] = pid
            f["callcounter"] = str(n + 100)
            f["where"] = WHERE_LETTER_RE.sub(rb"\g<1>" + letter + rb"\g<2>",
                                             where_tpl).decode("utf-8")
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
            u.timings.append(("getSelection", dt, status, len(body), app_error))
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
    args = ap.parse_args()

    rows = load_capture(args.capture)
    login_calls, pages = build_plan(rows)
    if CUSTOMER not in pages:
        print("customer page not in capture")
        return
    print(f"capacity bench: {args.users} users, hammer getSelection "
          f"(*V_invc_customer, varied where) for {args.duration}s")

    results = [None] * args.users
    threads = []
    t0 = time.time()
    deadline = t0 + args.duration
    for i in range(args.users):
        th = threading.Thread(target=run_user,
                              args=(args, login_calls, pages, results, i, deadline))
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    wall = time.time() - t0

    ok = sum(1 for r in results if r and r[0] == "OK")
    errs = [r for r in results if r and r[0] == "ERROR"]
    all_t = [t for r in results if r for t in r[2]]
    # count only the getSelection load calls for throughput (not login/main warmup)
    load_t = [t for t in all_t if t[0] == "getSelection"]
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
    print(f"getSelection calls: {len(load_t)}   non-200: {len(non200)}   "
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
