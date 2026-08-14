"""Single-record DB rung: getSelection on adm.user by username, one row.

Sits between /_ping (no DB) and the 100-row customer getSelection: the full
stack plus ONE indexed query and a ~700-byte envelope. Each level's sessions
hammer on their own keep-alive connection; the looked-up username rotates
over usernames.txt so the row is not a single hot record.

Usage (from temp/benchmark/assets):
  PGGSSENCMODE=disable python3 single_record_bench.py --levels 1,2,4,8 --duration 15
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

WHERE = ('<?xml version="1.0" encoding="utf-8"?>\n'
         '<GenRoBag><c_0 op="equal" column="username" column_dtype="T" '
         'column_caption="Username">{username}</c_0></GenRoBag>::bag')


def pct(ms, q):
    return statistics.quantiles(ms, n=100)[q - 1] if len(ms) > 1 else ms[0]


class Session:
    """One logged browser flattened to (cookie header, page_id, keep-alive host)."""

    def __init__(self, base, login_calls, pages, username, password):
        self.netloc = urllib.parse.urlparse(base).netloc
        u = User(base, login_calls, pages, username, password)
        html = u._get("/")
        self.page_id = u._page_id_from(html)
        for form in login_calls:
            f = inject_identity(form, username, password)
            u._post("/", f, self.page_id, "login")
        u._get("/")
        jar = None
        for handler in u.opener.handlers:
            if hasattr(handler, "cookiejar"):
                jar = handler.cookiejar
        self.cookie = "; ".join(f"{c.name}={c.value}" for c in jar)

    def form(self, lookup_username, callcounter):
        return {
            "method": "app.getSelection",
            "table": "adm.user",
            "where": WHERE.format(username=lookup_username),
            "queryMode": "S",
            "sortedBy": "username",
            "selectionName": "*V_adm_user_bench",
            "recordResolver": "false::B",
            "sqlContextName": "standard_list",
            "totalRowCount": "false::B",
            "row_start": "0",
            "excludeLogicalDeleted": "true::B",
            "excludeDraft": "true::B",
            "columns": "$username",
            "checkPermissions": "true::B",
            "row_count": "1::L",
            "storepath": ".store",
            "page_id": self.page_id,
            "callcounter": str(callcounter),
        }

    def hammer(self, deadline, usernames, out, errors):
        conn = http.client.HTTPConnection(self.netloc, timeout=30)
        headers = {"Cookie": self.cookie,
                   "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
        n = 0
        while time.time() < deadline:
            lookup = usernames[n % len(usernames)]
            body = urllib.parse.urlencode(self.form(lookup, n + 100))
            t = time.time()
            try:
                conn.request("POST", "/", body=body, headers=headers)
                resp = conn.getresponse()
                text = resp.read()
                if resp.status != 200 or b"<error>" in text:
                    errors.append(f"status={resp.status} {text[:100]!r}")
                    break
            except Exception as exc:
                errors.append(str(exc))
                conn.close()
                conn = http.client.HTTPConnection(self.netloc, timeout=30)
                continue
            out.append(time.time() - t)
            n += 1
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    ap.add_argument("--levels", default="1,2,4,8")
    ap.add_argument("--duration", type=float, default=15)
    ap.add_argument("--password", default="a")
    args = ap.parse_args()
    levels = [int(x) for x in args.levels.split(",")]

    rows = load_capture("session_capture.jsonl")
    login_calls, pages = build_plan(rows)
    usernames = [line.strip() for line in open("usernames.txt") if line.strip()]

    sessions = []
    print(f"single-record ramp on {args.base} — levels {levels}, {args.duration}s each")
    for level in levels:
        while len(sessions) < level:
            username = usernames[len(sessions) % len(usernames)]
            sessions.append(Session(args.base, login_calls, pages,
                                    username, args.password))
        errors = []
        outs = [[] for _ in sessions]
        deadline = time.time() + args.duration
        threads = [threading.Thread(target=s.hammer,
                                    args=(deadline, usernames, outs[i], errors))
                   for i, s in enumerate(sessions)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.time() - t0
        ms = sorted(x * 1000 for o in outs for x in o)
        print(f"users={level:3d}  {len(ms):6d} req in {wall:.1f}s -> "
              f"{len(ms) / wall:6.1f} req/s   p50={pct(ms, 50):6.1f}ms  "
              f"p90={pct(ms, 90):6.1f}ms  p99={pct(ms, 99):7.1f}ms"
              + (f"   ERRORS={len(errors)} ({errors[0]})" if errors else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
