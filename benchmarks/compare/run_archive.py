"""The per-run archive: one SQLite file both recorders write their lines into.

This is the recording TARGET, not a place lines are loaded into afterwards. A
separate load step is a step that can be forgotten, and the reference session of
2026-08-23 was lost exactly in the window between the run and its archiving.
Writing straight into the archive removes the window. A truncated JSONL line is
also possible when a process dies mid-write, while a half-written row is not.

One file per run, OUTSIDE the git tree (the lines carry whole bodies, the login
with the bench password and the session cookies, and this repository is public)
and on a LOCAL filesystem, because WAL does not work over network mounts.

The `record` table is ONE JSON column holding the whole line, plus a few
promoted ones, each promoted because it has a job: `run_id` and `exchange_id`
to JOIN, `stack` to SEPARATE, `ts` and `thread` to ORDER, `kind`, `subject` and
`status` to FILTER, `site_caller` to GROUP — which call path a run spends its
register calls and its milliseconds on is one query on that column, and it is
asked of every run. A promoted column is a COPY of what the JSON already holds,
never the only place a value lives — otherwise the blob stops being the record
and this is a schema again. A field is promoted once it is queried often; an
occasional query reads inside the JSON with SQLite's own `json_extract`.

The one declared exception is `stack`, which is a copy of the run's declared
condition rather than of the record: adding it to the record itself would change
a record shape that has to stay identical on both stacks.

`exchange_id` goes in as NULL when the record has no such key — the faithful
copy of an absence. The calls the master makes while building the site happen
before any exchange exists, and that is information, not a loss.

Two modes, one constructor. With a `run_id` the process MINTS the run: it
creates the file, the schema and the run row carrying the declared conditions.
Without, it ATTACHES to an existing archive and reads the run id back from it.
That is what lets `serve_legacy.py` mint the run in the master and every
recorder — in the forked worker here, in a spawned worker on the bridge —
attach to it by path.

The connection is opened lazily PER PID: a handle inherited across a fork is
the same class of invisible defect as a shared file descriptor. WAL serialises
the writers, and a lock serialises the threads inside one process.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime

# The path of the archive of the run in progress, published by the launcher that
# minted it. The channel has to survive a fork here and a spawn on the bridge,
# so it is the environment and not an inherited object.
RUN_ENV = "GNR_BENCH_RUN"

# Where a minted run lands, unless the launcher is told otherwise.
ARCHIVE_DIR_ENV = "GNR_BENCH_ARCHIVE_DIR"
DEFAULT_ARCHIVE_DIR = os.path.join(os.path.expanduser("~"), "genro_bench", "runs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    run_id     TEXT PRIMARY KEY,
    started    TEXT,
    conditions TEXT
);
CREATE TABLE IF NOT EXISTS record (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    stack       TEXT,
    kind        TEXT,
    exchange_id TEXT,
    site_caller TEXT,
    ts          TEXT,
    thread      INTEGER,
    subject     TEXT,
    status      INTEGER,
    line        TEXT
);
CREATE INDEX IF NOT EXISTS record_exchange ON record (run_id, exchange_id);
CREATE INDEX IF NOT EXISTS record_kind ON record (run_id, kind, subject);
CREATE INDEX IF NOT EXISTS record_caller ON record (run_id, site_caller);
"""

# What each kind of line answers `subject` with: the path for an HTTP exchange,
# the verb for a register call. One column, because reading the archive by hand
# filters on "what is this line about" without caring which recorder wrote it.
SUBJECT_FIELD = {"http": "path", "register": "verb"}


class RunArchive:
    """One SQLite file per run; both recorders append their lines to it."""

    def __init__(self, path, run_id=None, conditions=None):
        self.path = path
        self.lock = threading.Lock()
        self.connections = {}
        if run_id is None:
            self.run_id, self.conditions = self.read_run()
        else:
            self.run_id = run_id
            self.conditions = conditions
            self.create_run()

    @property
    def stack(self):
        return self.conditions.get("stack")

    @property
    def connection(self):
        """The connection of THIS process, opened on first use.

        Never a handle inherited across the fork: the register recorder is born
        in the master and every worker records afterwards.
        """
        pid = os.getpid()
        connection = self.connections.get(pid)
        if connection is None:
            connection = sqlite3.connect(self.path, isolation_level=None,
                                         check_same_thread=False)
            connection.execute("PRAGMA journal_mode=WAL")
            self.connections = {pid: connection}
        return connection

    def create_run(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with self.lock:
            self.connection.executescript(SCHEMA)
            self.connection.execute(
                "INSERT INTO run (run_id, started, conditions) VALUES (?, ?, ?)",
                (self.run_id, datetime.now().isoformat(),
                 json.dumps(self.conditions, ensure_ascii=False)))

    def read_run(self):
        row = self.connection.execute(
            "SELECT run_id, conditions FROM run ORDER BY started LIMIT 1").fetchone()
        return row[0], json.loads(row[1])

    def append_record(self, kind, record):
        """One row: the line as JSON, plus the columns queries need."""
        line = json.dumps(record, ensure_ascii=False, default=repr)
        with self.lock:
            self.connection.execute(
                "INSERT INTO record (run_id, stack, kind, exchange_id, "
                "site_caller, ts, thread, subject, status, line) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.run_id, self.stack, kind, record.get("exchange_id"),
                 record.get("site_caller"),
                 record.get("ts"), record.get("thread"),
                 record.get(SUBJECT_FIELD[kind]), record.get("status"), line))
