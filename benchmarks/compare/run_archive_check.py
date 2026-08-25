"""Isolation checks for the run archive: the schema, the run row, the promoted
columns as copies of the JSON, the connection that is never inherited across a
fork, and the join written as a query.

No site, no server, no site database — a throwaway archive in `temp/`.

It runs on the BENCH VENV, like the register check and unlike the HTTP one, and
not because it imports genropy — it does not. Section 5 forks a child out of a
process that has minted the run, which is the legacy shape: the gunicorn master
mints and its workers are forked from it. That shape needs sqlite 3.50.4, which
the bench venv carries, and it dies on sqlite 3.51.0 — measured on the pyenv
python 3.12.9 of this machine, 2026-08-25: SIGSEGV in the child, no exception to
catch and no line written. What poisons the child is the PARENT having opened a
connection at all: not WAL, not the same file, and closing before the fork does
not help. It is also INTERMITTENT — two runs in three — so a single green run
proves nothing. Same family as the libpq/Kerberos segfault that makes
`PGGSSENCMODE=disable` mandatory.

The bridge runs on that newer sqlite and forks its workers too, which is why the
template process there is held to writing nothing at all: `recording_engine_factory.py`.

Run: temp/legacy_venv/bin/python benchmarks/compare/run_archive_check.py
"""

import json
import os
import sys

from run_archive import RunArchive

ARCHIVE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "temp", "run_archive_check.sqlite")

CONDITIONS = {"stack": "legacy", "sitename": "test_invoice_pg_legacy",
              "workers": "1", "threads": "16", "debug": False,
              "database": {"dbname": "test_invoice_pg", "port": "5432"}}

# the join every reading of the archive starts from: a register line whose
# exchange the HTTP lines of the same run do not contain
UNJOINABLE = """
SELECT count(*) FROM record r
 WHERE r.kind = 'register' AND r.exchange_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM record h
                    WHERE h.kind = 'http' AND h.run_id = r.run_id
                      AND h.exchange_id = r.exchange_id)
"""


def drop_archive():
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(ARCHIVE + suffix):
            os.remove(ARCHIVE + suffix)


def fresh():
    drop_archive()
    return RunArchive(ARCHIVE, run_id="check-run", conditions=CONDITIONS)


def rows(archive, *columns):
    selected = ", ".join(columns)
    return archive.connection.execute(
        f"SELECT {selected} FROM record ORDER BY id").fetchall()


failures = []


def check(label, condition):
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


# 1. minting: the file, the schema, one run row carrying the conditions
archive = fresh()
check("the archive file exists where it was asked for", os.path.exists(ARCHIVE))
run_rows = archive.connection.execute(
    "SELECT run_id, started, conditions FROM run").fetchall()
check("one run row, carrying the run id", len(run_rows) == 1
      and run_rows[0][0] == "check-run")
check("the declared conditions are stored as data",
      json.loads(run_rows[0][2]) == CONDITIONS)
check("WAL is on, so concurrent writers serialise instead of failing",
      archive.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal")

# 2. attaching: another process reads the run back from the file alone
attached = RunArchive(ARCHIVE)
check("attaching reads the run id back", attached.run_id == "check-run")
check("attaching reads the conditions back", attached.conditions == CONDITIONS)
check("the stack is the run's declared one", attached.stack == "legacy")

# 3. the promoted columns are copies of what the JSON already holds
archive = fresh()
http_record = {"exchange_id": "ex1", "ts": "2026-08-23T10:00:00", "thread": 42,
               "path": "/site/index", "status": 200, "resp_body": "<answer/>"}
register_record = {"exchange_id": "ex1", "ts": "2026-08-23T10:00:01", "thread": 42,
                   "verb": "get_item", "surface": "client", "wire_calls": 1,
                   "site_caller": "gnr/web/gnrwebpage.py:1640 pageStore"}
boot_record = {"ts": "2026-08-23T09:59:00", "thread": 7, "verb": "refresh"}
archive.append_record("http", http_record)
archive.append_record("register", register_record)
archive.append_record("register", boot_record)
stored = rows(archive, "kind", "exchange_id", "ts", "thread", "subject",
              "status", "stack", "line", "site_caller")
check("every promoted column repeats a value the JSON still holds",
      all(json.loads(row[7]).get(field) == row[index]
          for row in stored
          for index, field in ((1, "exchange_id"), (2, "ts"), (3, "thread"),
                               (5, "status"), (8, "site_caller"))))
check("the whole record survives inside the JSON column",
      json.loads(stored[0][7]) == http_record)
check("subject is the path of an HTTP line", stored[0][4] == "/site/index")
check("subject is the verb of a register line", stored[1][4] == "get_item")
check("the stack is stamped on every row from the run's conditions",
      [row[6] for row in stored] == ["legacy"] * 3)
check("an absent exchange goes in as NULL, never faked",
      stored[2][1] is None and "exchange_id" not in json.loads(stored[2][7]))
check("the caller is promoted for grouping and still lives in the JSON",
      stored[1][8] == register_record["site_caller"]
      and json.loads(stored[1][7])["site_caller"] == register_record["site_caller"])
check("a line with no caller promotes NULL, never a made-up path",
      stored[0][8] is None and stored[2][8] is None)

# 4. the join: no register line names an exchange the HTTP lines do not carry
check("the join finds nothing unjoinable in a consistent run",
      archive.connection.execute(UNJOINABLE).fetchone()[0] == 0)
archive.append_record("register", {"exchange_id": "ghost", "verb": "get_item"})
check("the join does find an invented exchange",
      archive.connection.execute(UNJOINABLE).fetchone()[0] == 1)

# 5. the connection is never inherited across a fork
archive = fresh()
archive.append_record("http", {"exchange_id": "parent", "path": "/before-fork"})
child = os.fork()
if child == 0:
    archive.append_record("http", {"exchange_id": "child", "path": "/in-child"})
    os._exit(0)
os.waitpid(child, 0)
subjects = [row[0] for row in rows(archive, "subject")]
check("parent and child both wrote, on connections of their own",
      subjects == ["/before-fork", "/in-child"])
check("the parent kept exactly one connection, its own",
      list(archive.connections) == [os.getpid()])

drop_archive()
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
