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
owes a legitimate call. The rule itself lives in the declared-rules table of
`structural_diff.py`, together with everything else the bench recognises instead
of stopping on it.

**And the comparison.** Given the archive the TARGET is recording into, the
replay compares as it goes: after every exchange, the register lines of the
reference and the register lines the target just wrote must carry the same
sequence of calls and the same shape of arguments and answers
(`structural_diff.py`). At the first divergence nothing declares, the replay
stops and prints the report — the two stacks are still standing at that moment,
which is the whole reason this is a replica and not an offline diff of two
finished traces. Without that archive the replay only replays, as it did before.

An exchange the reference raced is replayed and NOT compared: its recorded reply
is a 400 the site owed nobody, so its register lines are the lines of a refused
call. The skip is printed.

**Parity first.** The run refuses to start while the two stacks carry different
genropy source (`genropy_parity_check.py`).

**And the databases stay apart.** The bridge writes from the first exchange —
that is what the copied db is for — so a cross-stack comparison refuses to start
while the target writes into the database the reference was recorded on: the
reference's own data would move under it, and no later run could reproduce what
it holds. The question is asked of the PAIR and only ACROSS stacks: a run
replayed against its own stack shares the database with its reference by
construction, and that self-check is what validates the comparison itself.

Run, from the repository root:

  python benchmarks/compare/replica.py ~/genro_bench/runs/<reference>.sqlite \\
      --target 127.0.0.1:8099 \\
      --target-archive ~/genro_bench/runs/<the run the target just minted>.sqlite
"""

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BENCH)

from genropy_parity_check import GenropyParity    # noqa: E402
from scaling_probe import StickyClient            # noqa: E402
from structural_diff import DeclaredRules, StructuralDiff   # noqa: E402

REPLICA_HEADER = "X-Bench-Replica-Of"
PAGE_ID_RE = re.compile(r"page_id:'([A-Za-z0-9_-]{22})'")
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=UTF-8"

# How long the replay waits for the target to finish writing the exchange it just
# answered. The HTTP recorder writes its line in the generator's `finally`, which
# runs after the last chunk has left, so the client can hold the whole reply a
# moment before the line exists.
EXCHANGE_WAIT_SECONDS = 5.0
EXCHANGE_POLL_SECONDS = 0.02


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

    @property
    def conditions(self):
        """The declared conditions of the run this archive holds."""
        row = self.connection.execute(
            "SELECT conditions FROM run ORDER BY started LIMIT 1").fetchone()
        return json.loads(row[0]) if row else {}

    def get_register_lines(self, exchange_id):
        """Every register line of one exchange, in the order the site made them."""
        rows = self.connection.execute(
            "SELECT line FROM record WHERE kind = 'register' AND exchange_id = ? "
            "ORDER BY ts, id", (exchange_id,)).fetchall()
        records = [json.loads(row[0]) for row in rows]
        records.sort(key=lambda record: record.get("ordinal") or 0)
        return records

    @property
    def cold_start_exchanges(self):  # wf:phase-7:new
        """The exchanges this run performed before its first RPC.

        Each stack finishes building lazily during them, and it builds in a
        different process: the bridge's site is built in the TEMPLATE its workers
        fork from, whose register lines are dropped by construction (a template
        that touches sqlite kills the children it forks), while the legacy site
        builds the same things in the process that serves the request and records
        them. Measured on 2026-08-25: the template makes the very two calls the
        bridge's first exchange was missing — the freshness check that instantiates
        `storage_gnr` — so the two stacks make the same call and only one of them
        is in an archive. The comparison therefore reads no register line from
        these exchanges (owner, 2026-08-25). They are still REPLAYED: the page the
        RPCs need is created there.

        A run with no RPC at all has no cold start by this definition: the rule
        must never be able to silence a whole comparison.
        """
        exchanges = self.exchanges
        for index, record in enumerate(exchanges):
            if record.get("rpc_method"):
                return exchanges[:index]
        return []

    @property
    def last_record_id(self):
        """The id of the last row written so far: where a replay starting now begins."""
        row = self.connection.execute("SELECT max(id) FROM record").fetchone()
        return row[0] or 0

    def get_exchange_replaying(self, reference_exchange_id, after_id=0):
        """The exchange this run sent to reproduce that reference one, or None.

        `after_id` is what keeps two replays into one archive apart: a stack
        answers every cycle into the archive it minted at startup, so without it
        the second replay would compare itself against the first one's lines and
        find them identical — a comparison that always passes and says nothing.
        """
        rows = self.connection.execute(
            "SELECT line FROM record WHERE kind = 'http' AND id > ? ORDER BY ts, id",
            (after_id,)).fetchall()
        for row in rows:
            record = json.loads(row[0])
            if (record.get("req_headers") or {}).get(REPLICA_HEADER) == reference_exchange_id:
                return record
        return None

    def get_skip_reason(self, record):
        """Why this exchange is not replayed, or None when it is."""
        if record.get("filtered") == "static":
            return "static"
        if "_ping" in (record.get("path") or "").split("/"):
            return "ping"
        return None

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


class DatabaseSeparation:
    """Do the two runs of a cross-stack comparison write into different databases?"""

    def __init__(self, reference, target):
        self.reference = reference
        self.target = target

    @property
    def reference_stack(self):
        """The stack the reference run declares."""
        return self.reference.conditions.get("stack")

    @property
    def target_stack(self):
        """The stack the target run declares."""
        return self.target.conditions.get("stack")

    @property
    def reference_dbname(self):
        """The database the reference was recorded on."""
        return (self.reference.conditions.get("database") or {}).get("dbname")

    @property
    def target_dbname(self):
        """The database the target is writing into."""
        return (self.target.conditions.get("database") or {}).get("dbname")

    @property
    def cross_stack(self):
        """Are the two runs on different stacks? Only then is the question asked."""
        return self.reference_stack != self.target_stack

    @property
    def separated(self):
        """Nothing to refuse: one stack, or two stacks on two databases."""
        return not self.cross_stack or self.reference_dbname != self.target_dbname

    @property
    def report(self):
        """What a human reads: where each run writes, and how to part them."""
        copy = f"{self.reference_dbname}_replica"
        lines = [f"reference run: {self.reference_stack} on {self.reference_dbname}",
                 f"target run:    {self.target_stack} on {self.target_dbname}"]
        if self.separated:
            lines.append("\nthe two runs write into different databases")
        else:
            lines.append(
                f"\nthe target writes into the database the reference was recorded "
                f"on: a replay would move the reference's own data. Remedy:\n"
                f"  dropdb --if-exists {copy}\n"
                f"  createdb -T {self.reference_dbname} {copy}\n"
                f"then serve the twin instance {copy}, whose instanceconfig.xml "
                f"names that database.\n"
                f"\nUntil this holds, no cross-stack comparison may start.")
        return "\n".join(lines)


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

    def __init__(self, trace, host, port, parity=None, target=None, rules=None):
        self.trace = trace
        self.host = host
        self.port = port
        self.parity = parity or GenropyParity()
        self.target = target
        self.rules = rules or DeclaredRules()
        self.diff = StructuralDiff(trace, target, self.rules) if target else None
        self.separation = DatabaseSeparation(trace, target) if target else None
        self.identity = IdentityMap()
        self.cold_start = {record.get("exchange_id")
                           for record in trace.cold_start_exchanges}
        self.client = ReplicaClient(host, port)
        self.failures = []
        self.races = []
        self.divergence = None
        self.compared = []
        self.uncompared = []
        self.replayed = {}
        self.rows = []
        self.elapsed_ms = 0.0
        # where this replay begins in the target's archive: the stack answers
        # every cycle into the file it minted at startup.
        self.target_start = target.last_record_id if target else 0

    def run(self):
        """Replay every exchange in order; return the failures met on the way.

        With a target archive the replay also COMPARES, exchange by exchange, and
        stops at the first divergence nothing declares: the two stacks are still
        standing when the report is printed, which an offline diff of two finished
        traces cannot offer.
        """
        if not self.parity.aligned:
            raise SystemExit(self.parity.report)
        if self.separation and not self.separation.separated:
            raise SystemExit(self.separation.report)
        exchanges = self.trace.exchanges
        print(f"replaying {len(exchanges)} exchanges of {self.trace.path} "
              f"against {self.host}:{self.port}")
        if self.diff:
            print(self.diff.header)
        started = time.time()
        for position, record in enumerate(exchanges, 1):
            status = self.replay_exchange(record)
            expected = record.get("status")
            race = (None if status == expected
                    else self.rules.get_status_reason(self.trace, record))
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
            if self.target is not None:
                self.replayed[record["exchange_id"]] = self.get_replayed_exchange(record)
            divergence = self.compare_exchange(record, position, race)
            row = self.get_row(record, position, status)
            self.rows.append(row)
            print(f"[{position:2d}/{len(exchanges)}] {row['label']} -> {status}  "
                  f"{self.get_timing(row)}{mark}")
            if self.uncompared and self.uncompared[-1][0] == position:
                print(f"     not compared — {self.uncompared[-1][2]}")
            if divergence is not None:
                break
        self.elapsed_ms = (time.time() - started) * 1000
        return self.failures

    @property
    def summary(self):  # wf:phase-7:new
        """The closing table: one row per exchange, everything the run measured.

        Register calls are the ones the structural comparison read; an exchange it
        did not compare shows a dash there and its reason under the table. The
        milliseconds are the RESPONSE TIME — request in, last chunk of the reply
        out — measured by the same HTTP recorder in the process that served the
        request on BOTH stacks, so the two columns are the same metre on the same
        call: the replay sends the recorded method, path and body, with only the
        identifiers the target mints rewritten.

        Not a benchmark: both stacks run under two recorders, and the reference was
        served while a driver or a browser was driving it. Macro-phase 3 measures
        performance, under its own declared conditions.
        """
        reference, replica = self.reference_stack, self.replica_stack
        headers = ["#", "exchange", "status",
                   f"register {reference}", f"register {replica}",
                   f"ms {reference}", f"ms {replica}", "delta"]
        rows = [[row["position"], row["label"], row["status"],
                 self.get_cell(row["reference_lines"]),
                 self.get_cell(row["replica_lines"]),
                 self.get_cell(row["reference_ms"], "{:.0f}"),
                 self.get_cell(row["replica_ms"], "{:.0f}"),
                 self.get_delta(row["reference_ms"], row["replica_ms"])]
                for row in self.rows]
        rows.append(self.total_row)
        lines = [f"{len(self.rows)} exchange(s) replayed, {len(self.compared)} compared, "
                 f"{self.elapsed_ms / 1000:.1f} s of wall clock", ""]
        lines.append(self.get_table(headers, rows))
        lines.append("")
        for position, label, reason in self.uncompared:
            lines.append(f"  [{position}] {label}: register calls not compared — {reason}")
        lines.append("  ms = response time, request in to reply out, the same metre on "
                     "both stacks; not a benchmark, both run under two recorders")
        return "\n".join(lines)

    @property
    def total_row(self):  # wf:phase-7:new
        """The last row of the table: what the whole session cost on each stack."""
        def total(key):
            values = [row[key] for row in self.rows if row[key] is not None]
            return sum(values) if values else None
        reference_ms, replica_ms = total("reference_ms"), total("replica_ms")
        return ["", "total", "",
                self.get_cell(total("reference_lines")),
                self.get_cell(total("replica_lines")),
                self.get_cell(reference_ms, "{:.0f}"),
                self.get_cell(replica_ms, "{:.0f}"),
                self.get_delta(reference_ms, replica_ms)]

    def get_cell(self, value, shape="{}"):  # wf:phase-7:new
        """One cell: the value, or a dash where there is nothing to show."""
        return "-" if value is None else shape.format(value)

    def get_table(self, headers, rows):  # wf:phase-7:new
        """Headers, a rule, one line per row; the numbers right, the words left."""
        widths = [max(len(str(cell)) for cell in column)
                  for column in zip(headers, *rows)]

        def line(cells):
            return "  " + "  ".join(
                str(cell).ljust(width) if index == 1 else str(cell).rjust(width)
                for index, (cell, width) in enumerate(zip(cells, widths)))

        return "\n".join([line(headers),
                          "  " + "  ".join("-" * width for width in widths)]
                         + [line(row) for row in rows])

    def compare_exchange(self, record, position, race):
        """Compare the exchange just replayed with its reference; the divergence, or None.

        Two exchanges are not compared, and each skip is printed, never silent.
        One the reference raced: its recorded reply is a 400 the site owed nobody,
        so its register lines are the lines of a refused call and comparing them
        would compare two different things. One from the cold start, before the
        first RPC: see `TraceReader.cold_start_exchanges`.
        """
        if self.diff is None:
            return None
        label = self.get_label(record)
        if race:
            return self.not_compared(position, label, race)
        if record.get("exchange_id") in self.cold_start:
            return self.not_compared(position, label,
                                     "before the first RPC: what each stack still "
                                     "builds lazily, it builds in a different process")
        replayed = self.replayed.get(record["exchange_id"])
        if replayed is None:
            return self.not_compared(position, label,
                                     f"the target archive carries no exchange stamped "
                                     f"with {record.get('exchange_id')}")
        self.compared.append((
            position, label,
            len(self.trace.get_register_lines(record["exchange_id"])),
            len(self.target.get_register_lines(replayed["exchange_id"]))))
        self.divergence = self.diff.get_divergence(record, replayed, position)
        return self.divergence

    def not_compared(self, position, label, reason):  # wf:phase-7:new
        """Record why this exchange was left out; the caller prints it, never silent."""
        self.uncompared.append((position, label, reason))
        return None

    def get_label(self, record):  # wf:phase-7:new
        """The exchange as one readable name: its method, its path, its RPC."""
        return (f"{record.get('method')} {record.get('path')} "
                f"{record.get('rpc_method') or ''}").strip()

    def get_row(self, record, position, status):  # wf:phase-7:new
        """Everything this exchange measured, for the live line and for the table."""
        compared = next((entry for entry in self.compared if entry[0] == position), None)
        replayed = self.replayed.get(record["exchange_id"]) or {}
        return {"position": position, "label": self.get_label(record), "status": status,
                "reference_lines": compared[2] if compared else None,
                "replica_lines": compared[3] if compared else None,
                "reference_ms": record.get("duration_ms"),
                "replica_ms": replayed.get("duration_ms")}

    def get_timing(self, row):  # wf:phase-7:new
        """The live line's timing: what each stack recorded for this exchange."""
        if row["reference_ms"] is None:
            return ""
        if row["replica_ms"] is None:
            return f"{self.reference_stack} {row['reference_ms']:.0f} ms"
        return (f"{self.reference_stack} {row['reference_ms']:.0f} ms  "
                f"{self.replica_stack} {row['replica_ms']:.0f} ms  "
                f"({self.get_delta(row['reference_ms'], row['replica_ms'])})")

    def get_delta(self, reference_ms, replica_ms):  # wf:phase-7:new
        """The replica's time against the reference's, as a signed percentage."""
        if not reference_ms or replica_ms is None:
            return "-"
        return f"{(replica_ms - reference_ms) / reference_ms * 100:+.0f}%"

    @property
    def reference_stack(self):  # wf:phase-7:new
        """The stack the reference archive declares."""
        return self.trace.conditions.get("stack") or "reference"

    @property
    def replica_stack(self):  # wf:phase-7:new
        """The stack the target archive declares."""
        return (self.target.conditions.get("stack") if self.target else None) or "replica"

    def get_replayed_exchange(self, record):
        """The target's own exchange for this one, once the target has written it."""
        deadline = time.time() + EXCHANGE_WAIT_SECONDS
        while time.time() < deadline:
            replayed = self.target.get_exchange_replaying(record.get("exchange_id"),
                                                         self.target_start)
            if replayed is not None:
                return replayed
            time.sleep(EXCHANGE_POLL_SECONDS)
        return None

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
    parser.add_argument("--target-archive",
                        help="the .sqlite the target stack is recording into; "
                             "with it the replay compares and stops at the first "
                             "divergence, without it it only replays")
    arguments = parser.parse_args()
    host, _, port = arguments.target.partition(":")
    target = (TraceReader(os.path.expanduser(arguments.target_archive))
              if arguments.target_archive else None)
    replica = Replica(TraceReader(os.path.expanduser(arguments.archive)),
                      host, int(port or "8099"), target=target)
    failures = replica.run()
    print()
    for position, path, rpc_method, expected, status, race in replica.races:
        print(f"recognised race [{position}] {path} {rpc_method or ''}: "
              f"the trace carries {expected}, the replay got {status} — {race}")
    if replica.diff:
        for known in replica.diff.known:
            print(known.report)
    if replica.divergence:
        print(replica.divergence.report)
        print()
        print(replica.summary)
        sys.exit(1)
    if failures:
        print(f"{len(failures)} exchange(s) answered a status the trace does not carry:")
        for position, path, rpc_method, expected, status in failures:
            print(f"  [{position}] {path} {rpc_method or ''}: "
                  f"expected {expected}, got {status}")
        sys.exit(1)
    print(replica.summary)
    print(f"every exchange answered the status the trace carries, "
          f"{len(replica.races)} of them as a recognised race of the reference")
    if replica.diff:
        print(f"no divergence left unexplained, "
              f"{len(replica.diff.known)} recognised by a declared rule "
              f"({', '.join(replica.diff.rules.names)})")
