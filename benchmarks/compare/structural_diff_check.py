"""Isolation checks for the structural comparison: what counts as a difference,
what a declared rule takes off the table, and whether the report can be read
without opening the code.

No site, no server, no stack: two throwaway archives under `temp/`, written line
by line so that every difference here is one somebody put there on purpose. What
IS exercised is the whole decision — the pairing, the shape, the alignment of two
call sequences, the rules table and the report.

It runs on the bridge interpreter, because `TraceReader` lives in `replica.py`,
which imports the parity check, which imports `gnr` to see which tree this
process resolves it from.

Run: python benchmarks/compare/structural_diff_check.py
"""

import os
import sys

from replica import TraceReader
from run_archive import RunArchive
from structural_diff import DeclaredRule, DeclaredRules, StructuralDiff

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMP = os.path.join(REPO_ROOT, "temp")
REFERENCE = os.path.join(TEMP, "structural_diff_check_reference.sqlite")
REPLICA = os.path.join(TEMP, "structural_diff_check_replica.sqlite")

REFERENCE_CONDITIONS = {"stack": "legacy", "genropy_commit": "6da02feda"}
REPLICA_CONDITIONS = {"stack": "legacy", "genropy_commit": "6da02feda"}

# The two exchanges every archive below carries: one reference id, one the target
# minted, joined by the header the replica stamps.
REFERENCE_EXCHANGE = "r1"
REPLICA_EXCHANGE = "p1"

REFERENCE_ITEM = ("{'register_item_id': 'n61Oj3NIP2CV9v9U5fW89w', "
                  "'start_ts': datetime.datetime(2026, 8, 23, 23, 47, 55, 501599), "
                  "'user': 'guest_cAmyRRDRMFyVTJpe0ta6Sw', 'user_ip': '127.0.0.1'}")
REPLICA_ITEM = ("{'register_item_id': 'bOfiz7FyOM22cVBwuEp2Vw', "
                "'start_ts': datetime.datetime(2026, 8, 25, 8, 5, 22, 499810), "
                "'user': 'guest_Cdp3RyjMPXSIg5bUqs2Qhw', 'user_ip': '127.0.0.1'}")
POORER_ITEM = ("{'register_item_id': 'bOfiz7FyOM22cVBwuEp2Vw', "
               "'start_ts': datetime.datetime(2026, 8, 25, 8, 5, 22, 499810), "
               "'user': 'guest_Cdp3RyjMPXSIg5bUqs2Qhw'}")

REFERENCE_BAG = ("<?xml version='1.0' encoding='UTF-8'?>"
                 "<GenRoBag><language>it</language>"
                 "<workdate _T='D'>2026-08-23</workdate></GenRoBag>")
REPLICA_BAG = ("<?xml version='1.0' encoding='UTF-8'?>"
               "<GenRoBag><language>en</language>"
               "<workdate _T='D'>2026-08-25</workdate></GenRoBag>")
POORER_BAG = ("<?xml version='1.0' encoding='UTF-8'?>"
              "<GenRoBag><language>en</language></GenRoBag>")

CALLER = "gnr/web/gnrwebpage.py:505 db <- gnr/web/gnrwebpage.py:652 _rpcDispatcher"

failures = []


def check(label, condition):
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


def drop_archives():
    for path in (REFERENCE, REPLICA):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


def register_line(exchange_id, ordinal, verb, args, result, surface="client"):
    return {"exchange_id": exchange_id, "ordinal": ordinal, "surface": surface,
            "verb": verb, "args": args, "kwargs": {}, "result": result,
            "site_caller": CALLER, "ts": f"2026-08-25T08:00:{ordinal:02d}"}


def build(path, conditions, exchange_id, lines, replaying=None):
    """One archive: its run row, one HTTP exchange and the register lines under it."""
    archive = RunArchive(path, run_id=os.path.basename(path), conditions=conditions)
    headers = {"X-Bench-Replica-Of": replaying} if replaying else {}
    archive.append_record("http", {"exchange_id": exchange_id,
                                   "ts": "2026-08-25T08:00:00", "method": "POST",
                                   "path": "/sys/thpage/invc/customer",
                                   "rpc_method": "app.getSelection", "status": 200,
                                   "req_headers": headers})
    # written out of order on purpose: the reader orders by ordinal, and a thread
    # that finishes late must not reshuffle the sequence the site made.
    for line in reversed(lines):
        archive.append_record("register", line)
    return archive


def compare(reference_lines, replica_lines, rules=None):
    """Two archives built from the two sequences, and the first divergence."""
    drop_archives()
    build(REFERENCE, REFERENCE_CONDITIONS, REFERENCE_EXCHANGE, reference_lines)
    build(REPLICA, REPLICA_CONDITIONS, REPLICA_EXCHANGE, replica_lines,
          replaying=REFERENCE_EXCHANGE)
    reference, replica = TraceReader(REFERENCE), TraceReader(REPLICA)
    diff = StructuralDiff(reference, replica, rules)
    replayed = replica.get_exchange_replaying(REFERENCE_EXCHANGE)
    return diff, diff.get_divergence(reference.records[0], replayed, 7)


IDENTICAL = [register_line(REFERENCE_EXCHANGE, 1, "connection", ["cAmyRRDRMFyVTJpe0ta6Sw"],
                           REFERENCE_ITEM),
             register_line(REFERENCE_EXCHANGE, 2, "getItem", ["rootenv"],
                           REFERENCE_BAG, surface="store"),
             register_line(REFERENCE_EXCHANGE, 3, "get_dbenv", ["n61Oj3NIP2CV9v9U5fW89w"],
                           REFERENCE_BAG, surface="passthrough")]


def replica_of(lines):
    """The same sequence as the target would have recorded it: its own identifiers."""
    replayed = []
    for line in lines:
        copy = dict(line, exchange_id=REPLICA_EXCHANGE)
        copy["args"] = [REPLICA_ITEM if value == REFERENCE_ITEM
                        else "bOfiz7FyOM22cVBwuEp2Vw" if value == "n61Oj3NIP2CV9v9U5fW89w"
                        else "Cdp3RyjMPXSIg5bUqs2Qhw" if value == "cAmyRRDRMFyVTJpe0ta6Sw"
                        else value for value in line["args"]]
        copy["result"] = (REPLICA_ITEM if line["result"] == REFERENCE_ITEM
                          else REPLICA_BAG if line["result"] == REFERENCE_BAG
                          else line["result"])
        replayed.append(copy)
    return replayed


# 1. the same session run twice: everything a second run legitimately changes
diff, divergence = compare(IDENTICAL, replica_of(IDENTICAL))
check("a run reproduced exactly has no divergence", divergence is None)
check("nothing was recognised by a rule either — there was nothing to recognise",
      diff.known == [])
check("the minted identifiers of the two runs are not a difference",
      divergence is None)

# 2. what the reader owes the comparison
reference = TraceReader(REFERENCE)
check("the reader answers with the declared conditions of its run",
      reference.conditions["genropy_commit"] == "6da02feda")
check("the register lines of an exchange come back in the order the site made them, "
      "not in the order the threads wrote them",
      [line["ordinal"] for line in reference.get_register_lines(REFERENCE_EXCHANGE)]
      == [1, 2, 3])
check("the target's exchange is found by the header the replica stamped",
      TraceReader(REPLICA).get_exchange_replaying(REFERENCE_EXCHANGE)["exchange_id"]
      == REPLICA_EXCHANGE)
check("an exchange nobody replayed is not found",
      TraceReader(REPLICA).get_exchange_replaying("never-sent") is None)

# 2b. two replays into one archive: a stack records every cycle into the file it
# minted at startup, so the lookup has to start where the replay started.
replica_archive = TraceReader(REPLICA)
started = replica_archive.last_record_id
second = RunArchive(REPLICA)
second.append_record("http", {"exchange_id": "p2", "ts": "2026-08-25T09:00:00",
                              "method": "POST", "path": "/sys/thpage/invc/customer",
                              "rpc_method": "app.getSelection", "status": 200,
                              "req_headers": {"X-Bench-Replica-Of": REFERENCE_EXCHANGE}})
check("a replay starting now finds its own exchange, not the earlier one carrying "
      "the same header",
      replica_archive.get_exchange_replaying(REFERENCE_EXCHANGE, started)["exchange_id"]
      == "p2")
check("read from the beginning the archive still answers with the first one",
      replica_archive.get_exchange_replaying(REFERENCE_EXCHANGE)["exchange_id"]
      == REPLICA_EXCHANGE)
check("the header of the report names both runs and the genropy each declared",
      "stack legacy, genropy 6da02feda" in diff.header)

# 3. a call the replica did not make
missing = replica_of(IDENTICAL)[:2]
diff, divergence = compare(IDENTICAL, missing)
check("a call the replica never made is a divergence",
      divergence is not None and divergence.kind == "call missing in the replica")
check("the missing call is named at its own position in the sequence",
      divergence.ordinal == 3)
check("the report names the reference call and says the replica made none",
      "passthrough:get_dbenv" in divergence.report and "(no call)" in divergence.report)

# 4. a call the replica made and the reference did not
extra = replica_of(IDENTICAL)
extra.insert(2, register_line(REPLICA_EXCHANGE, 0, "pageStore",
                              ["bOfiz7FyOM22cVBwuEp2Vw"], REPLICA_ITEM))
# the recorder numbers the calls of an exchange as it makes them, so one more
# call renumbers the ones after it: the test data says the same.
extra = [dict(line, ordinal=position) for position, line in enumerate(extra, 1)]
diff, divergence = compare(IDENTICAL, extra)
check("a call only the replica made is a divergence",
      divergence is not None and divergence.kind == "extra call in the replica")
check("the extra call is named where it was inserted", divergence.ordinal == 3)
check("the report carries the site_caller of both sides — Phase 6 diagnoses with it",
      divergence.report.count("gnrwebpage.py:505 db") >= 1)

# 5. a different call in the same place
changed = replica_of(IDENTICAL)
changed[1] = dict(changed[1], verb="get")
diff, divergence = compare(IDENTICAL, changed)
check("the same position holding a different verb is a divergence",
      divergence is not None and divergence.kind == "different call")

# 6. the same call with different arguments
other_arguments = replica_of(IDENTICAL)
other_arguments[1] = dict(other_arguments[1], args=["pageArgs"])
diff, divergence = compare(IDENTICAL, other_arguments)
check("the same call asking for something else is a divergence of arguments",
      divergence is not None and divergence.kind == "arguments")
check("the report shows both argument lists",
      "rootenv" in divergence.report and "pageArgs" in divergence.report)

# 7. answers: the shape counts, the values do not
poorer_bag = replica_of(IDENTICAL)
poorer_bag[1] = dict(poorer_bag[1], result=POORER_BAG)
diff, divergence = compare(IDENTICAL, poorer_bag)
check("a Bag answer that lost a node is a divergence of the answer",
      divergence is not None and divergence.kind == "answer")
check("a Bag answer whose values differ is NOT a divergence — the customer read "
      "and the workdate change at every run",
      compare(IDENTICAL, replica_of(IDENTICAL))[1] is None)

poorer_item = replica_of(IDENTICAL)
poorer_item[0] = dict(poorer_item[0], result=POORER_ITEM)
diff, divergence = compare(IDENTICAL, poorer_item)
check("a register item that lost a key is a divergence of the answer",
      divergence is not None and divergence.kind == "answer")
check("a register item whose start_ts and identifiers moved is not",
      compare(IDENTICAL, replica_of(IDENTICAL))[1] is None)

# 8. the first divergence, and only it: the run stops there
two = replica_of(IDENTICAL)
two[1] = dict(two[1], verb="get")
two[2] = dict(two[2], args=["something else"])
diff, divergence = compare(IDENTICAL, two)
check("with two differences the first one in the sequence is the one reported",
      divergence.ordinal == 2)

# 9. the declared-rules table: what does not stop the run
check("the table ships with the one rule already measured",
      DeclaredRules().names == ["reference-race"])


class ExtraPageStoreIsKnown(DeclaredRule):
    """A stand-in for the bridge rules Phases 5 and 7 will measure and add."""

    name = "extra-pagestore"

    def get_divergence_reason(self, divergence):
        if divergence.replica and divergence.replica.call == ("client", "pageStore"):
            return "the bridge reads the page store one extra time"
        return None


diff, divergence = compare(IDENTICAL, extra, DeclaredRules([ExtraPageStoreIsKnown()]))
check("a divergence a declared rule recognises does not stop the run",
      divergence is None)
check("it is reported as known, never passed in silence",
      len(diff.known) == 1 and diff.known[0].known.startswith("extra-pagestore:"))
check("the known report says which rule recognised it and why",
      "extra-pagestore: the bridge reads the page store one extra time"
      in diff.known[0].report)
check("a divergence no rule recognises still stops the run, table or no table",
      compare(IDENTICAL, changed, DeclaredRules([ExtraPageStoreIsKnown()]))[1]
      is not None)

# 10. the report reads without the code
diff, divergence = compare(IDENTICAL, extra)
report = divergence.report
check("the report names the exchange the way the replay printed it",
      "/sys/thpage/invc/customer" in report and "app.getSelection" in report)
check("the report names the position of the exchange in the replay",
      "[7]" in report)
check("the report names the two sides by role",
      "reference:" in report and "replica:" in report)
check("the report opens on the verdict",
      report.startswith("DIVERGENCE: extra call in the replica"))
print()
print(report)

drop_archives()
print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("every check passed")
