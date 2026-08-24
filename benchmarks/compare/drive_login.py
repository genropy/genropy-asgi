"""Replay one login over HTTP against the legacy stack, to exercise the
HTTP recorder on real traffic without a browser.

Reuses the bench machinery: replay_a1.build_plan extracts the two login
pageCalls from the captured session, scaling_probe.login_user replays them on a
single keep-alive connection with the identity rewritten in both places.

Run: python3 benchmarks/compare/drive_login.py [username] [port]

The port defaults to the legacy stack (8099); the bridge answers on 8098, and
the driver is the same on both — that is the point of driving by network calls.
"""

import os
import sys

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BENCH)

from replay_a1 import build_plan, load_capture      # noqa: E402
from scaling_probe import login_user                # noqa: E402

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "alexander.king"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8099
PASSWORD = "a"

login_calls, _pages = build_plan(load_capture(os.path.join(BENCH,
                                                           "session_capture.jsonl")))
print(f"login calls in the capture: "
      f"{[c.get('method') for c in login_calls]}")

client = login_user("127.0.0.1", PORT, login_calls, USERNAME, PASSWORD)
print(f"logged in as {USERNAME} on {PORT}, cookies: {sorted(client.cookies)}")
