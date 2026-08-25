"""The replica: it reads an archived run and performs it again, by network
calls, against a live stack.

There is no script beside the archive. The recorded lines ARE what the replica
reads, so nothing can drift away from what really happened. Any archived run
serves — the reference session is a role, not a format (foreman decision,
2026-08-25): Phase 2 replays the browser session of 2026-08-23 because it is the
one that exercises every identifier the adaptation has to handle.

**What it replays, and what it leaves out.** Two declared rules, and they are
declared because a silent skip is a divergence nobody can see afterwards:

- `/_ping` stays out. It is a browser heartbeat on a timer, and what a ping
  carries depends on when it fires — a replica cannot make that true again.
  Consequence on record: the delivery of datachanges through a ping is never
  compared. In the 2026-08-23 session the rule costs ONE non-empty ping, the
  412 that precedes the login.
- Statics stay out. In that session they are 223 of the 266 exchanges and each
  one produces the same pair of register calls, `globalStore` and `getItem` —
  446 lines carrying no information. Consequence on record: the serving of
  static assets is never compared between the two stacks.

**Identifiers.** A page id minted by one stack means nothing to the other, so
the replica keeps a map from the token in the trace to the token the target
minted in its place, learned from the HTML the target returns, and rewrites it
wherever it appears: in the form values AND in the query string — the GET of a
TH page carries `_calling_page_id` there. Cookies are never replayed: the
client keeps its own jar, and the target's `spa_connection_id` is the one that
counts. What is NOT adapted is `callcounter`: the sequence replayed is the
sequence recorded, so the counter of the trace is the right one.

**The pairing.** Each request carries the exchange id it is replaying, as the
`X-Bench-Replica-Of` request header. The HTTP recorder already writes every
request header into its line, so the replica run's archive says by itself which
reference exchange each of its own exchanges reproduces — no recorder changes,
no new field in the record.

**Order, not timing.** The exchanges go out in the order the trace holds them,
one after another on a single keep-alive connection, with no waiting in
between. A replica reproduces what was done, never how long the hands took.

**And that is why one recorded status can never be reproduced.** A browser
sends calls that overlap; the bench does not. When the reply recorded in the
trace says the connection had already been rotated, AND the trace shows that
exchange running while an earlier one was still in flight on the same
pre-rotation cookie, the recorded status is a RACE of the reference session,
not a divergence of the stack. The replay names it and carries on; it never
passes it in silence. Measured on the 2026-08-23 session: the two `login_doLogin`
calls overlap by 22.8 ms, the first rotates the connection, the second answers
400 — and a replica replaying them one after the other gets the 200 the site
owes a legitimate call. The rule is Phase 3's to hold as a declared one.

**Parity first.** The run refuses to start while the two stacks carry different
genropy source (`genropy_parity_check.py`).

Run, from the repository root:

  python benchmarks/compare/replica.py ~/genro_bench/runs/<run_id>.sqlite \\
      --target 127.0.0.1:8099
"""

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
import urllib.parse

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BENCH)

from genropy_parity_check import GenropyParity    # noqa: E402
from scaling_probe import StickyClient            # noqa: E402

REPLICA_HEADER = "X-Bench-Replica-Of"
PAGE_ID_RE = re.compile(r"page_id:'([A-Za-z0-9_-]{22})'")
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=UTF-8"

# What the site answers a call arriving on a connection a login already replaced.
# Copied verbatim from `gnr/web/gnrwebpage.py:307`, typo and all: it is a literal
# the site writes, not a sentence, and correcting it here would match nothing.
CONNECTION_ROTATED = "The connection is not longer valid"


class TraceReader:
    """The HTTP exchanges of one archived run, in the order they happened."""

    def __init__(self, path):
        self.path = path
        self.connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    @property
    def records(self):
        """Every recorded HTTP line, oldest first."""
        rows = self.connection.execute(
            "SELECT line FROM record WHERE kind = 'http' ORDER BY ts, id").fetchall()
        return [json.loads(row[0]) for row in rows]

    @property
    def exchanges(self):
        """The lines the replica performs again: everything not skipped."""
        return [record for record in self.records
                if self.get_skip_reason(record) is None]

    def get_skip_reason(self, record):
        """Why this exchange is not replayed, or None when it is."""
        if record.get("filtered") == "static":
            return "static"
        if "_ping" in (record.get("path") or "").split("/"):
            return "ping"
        return None

    def get_race_reason(self, record):
        """Why this exchange's recorded status is a race of the session, or None.

        Two conditions, and both are read from the trace itself: the recorded
        reply says the connection had already been rotated, and the exchange was
        running while an earlier one was still in flight on the same cookie. A
        reply of the first kind alone proves nothing — a stale tab produces one
        too, and that one IS reproducible.
        """
        if CONNECTION_ROTATED not in (record.get("resp_body") or ""):
            return None
        overlapped = self.get_overlapped_exchange(record)
        if overlapped is None:
            return None
        return (f"the connection was rotated by {overlapped.get('rpc_method') or overlapped.get('path')}, "
                f"still in flight on the same cookie")

    def get_overlapped_exchange(self, record):
        """The earlier exchange still running when this one started, or None."""
        started = datetime.datetime.fromisoformat(record["ts"])
        cookie = (record.get("req_headers") or {}).get("Cookie")
        for earlier in self.records:
            # by id, never by identity: `records` answers with fresh dicts every
            # time it is read, so the record handed in here is not the one this
            # loop meets again.
            if (earlier.get("exchange_id") == record.get("exchange_id")
                    or not earlier.get("duration_ms")):
                continue
            began = datetime.datetime.fromisoformat(earlier["ts"])
            ended = began + datetime.timedelta(milliseconds=earlier["duration_ms"])
            if began < started < ended and (earlier.get("req_headers") or {}).get("Cookie") == cookie:
                return earlier
        return None


class IdentityMap:
    """Trace tokens and the tokens the target minted in their place."""

    def __init__(self):
        self.tokens = {}

    def learn_page_id(self, trace_body, target_body):
        """Pair the page the trace was given with the page the target minted."""
        trace_id = self.get_page_id(trace_body)
        target_id = self.get_page_id(target_body)
        if trace_id and target_id:
            self.tokens[trace_id] = target_id

    def get_page_id(self, html):
        found = PAGE_ID_RE.search(html or "")
        return found.group(1) if found else None

    def get_adapted(self, text):
        """The same text with every known trace token replaced by the target's."""
        for trace_id, target_id in self.tokens.items():
            text = text.replace(trace_id, target_id)
        return text


class ReplicaClient(StickyClient):
    """A StickyClient that stamps each request with the exchange it replays."""

    def __init__(self, host, port):
        super().__init__(host, port)
        self.replaying = None

    def _headers(self):
        headers = super()._headers()
        if self.replaying:
            headers[REPLICA_HEADER] = self.replaying
        return headers

    def send_request(self, method, path, body=None, content_type=None):
        """One request, one reply: the body verbatim, the cookies remembered."""
        headers = self._headers()
        if content_type:
            headers["Content-Type"] = content_type
        self.conn.request(method, path, body=body, headers=headers)
        response = self.conn.getresponse()
        answer = response.read()
        self._store_cookies(response)
        return response.status, answer


class Replica:
    """One archived run, performed again against a live stack."""

    def __init__(self, trace, host, port, parity=None):
        self.trace = trace
        self.host = host
        self.port = port
        self.parity = parity or GenropyParity()
        self.identity = IdentityMap()
        self.client = ReplicaClient(host, port)
        self.failures = []
        self.races = []

    def run(self):
        """Replay every exchange in order; return the failures met on the way."""
        if not self.parity.aligned:
            raise SystemExit(self.parity.report)
        exchanges = self.trace.exchanges
        print(f"replaying {len(exchanges)} exchanges of {self.trace.path} "
              f"against {self.host}:{self.port}")
        for position, record in enumerate(exchanges, 1):
            status = self.replay_exchange(record)
            expected = record.get("status")
            race = None if status == expected else self.trace.get_race_reason(record)
            if status == expected:
                mark = ""
            elif race:
                mark = f"   RACE of the reference, expected {expected}"
                self.races.append((position, record.get("path"),
                                   record.get("rpc_method"), expected, status, race))
            else:
                mark = f"   FAIL expected {expected}"
                self.failures.append((position, record.get("path"),
                                      record.get("rpc_method"), expected, status))
            print(f"[{position:2d}/{len(exchanges)}] {record.get('method')} "
                  f"{record.get('path')} {record.get('rpc_method') or ''} "
                  f"-> {status}{mark}")
        return self.failures

    def replay_exchange(self, record):
        """Send one recorded exchange to the target; return the status it answered."""
        self.client.replaying = record.get("exchange_id")
        path = self.get_adapted_path(record)
        if record.get("method") == "GET":
            status, body = self.client.send_request("GET", path)
        elif record.get("form") is not None:
            status, body = self.client.send_request(
                "POST", path, urllib.parse.urlencode(self.get_adapted_form(record),
                                                     doseq=True),
                FORM_CONTENT_TYPE)
        else:
            headers = record.get("req_headers") or {}
            status, body = self.client.send_request(
                "POST", path, record.get("req_body") or "",
                headers.get("Content-Type"))
        self.identity.learn_page_id(record.get("resp_body"),
                                    body.decode("utf-8", "replace"))
        return status

    def get_adapted_path(self, record):
        """The recorded path, with the query string's identifiers rewritten."""
        path = record.get("path") or "/"
        query = record.get("query")
        return f"{path}?{self.identity.get_adapted(query)}" if query else path

    def get_adapted_form(self, record):
        """The recorded form, with every identifier rewritten in every value."""
        adapted = {}
        for key, value in record["form"].items():
            if isinstance(value, str):
                adapted[key] = self.identity.get_adapted(value)
            elif isinstance(value, list):
                adapted[key] = [self.identity.get_adapted(item) for item in value]
            else:
                adapted[key] = value
        return adapted


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", help="the .sqlite of the run to replay")
    parser.add_argument("--target", default="127.0.0.1:8099",
                        help="host:port of the stack to replay against")
    arguments = parser.parse_args()
    host, _, port = arguments.target.partition(":")
    replica = Replica(TraceReader(os.path.expanduser(arguments.archive)),
                      host, int(port or "8099"))
    failures = replica.run()
    print()
    for position, path, rpc_method, expected, status, race in replica.races:
        print(f"recognised race [{position}] {path} {rpc_method or ''}: "
              f"the trace carries {expected}, the replay got {status} — {race}")
    if failures:
        print(f"{len(failures)} exchange(s) answered a status the trace does not carry:")
        for position, path, rpc_method, expected, status in failures:
            print(f"  [{position}] {path} {rpc_method or ''}: "
                  f"expected {expected}, got {status}")
        sys.exit(1)
    print(f"every exchange answered the status the trace carries, "
          f"{len(replica.races)} of them as a recognised race of the reference")
