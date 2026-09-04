"""Generate the think-time trace both targets of Prova 2 will replay.

The trace is written BEFORE the runs and read by both legs, so legacy and
bridge get the identical sequence: same accounts, same entry times, same
pauses, in the same order. A seed alone would not be enough — it would tie the
draws to the order in which the code happens to ask for them — so the draws are
materialised into a file, and the file is what the two legs share.

One line per user, JSON:

    {"user": 1, "username": "loaduser0001", "entry_offset_s": 0.0,
     "think_times": [88.4, 31.0, ...]}

``entry_offset_s`` is when that user logs in, counted from the start of the
run: one arrival every ``--login-every`` seconds, no randomness there — the
ramp is a fact of the protocol, not a draw.

``think_times`` are the pauses that follow each BURST OF RECORDED WORK — one
full pass of the archive's body — drawn uniformly between ``--min-think`` and
``--max-think``. They are deliberately not called "a pause after each operator
action": the body holds 101 exchanges over 192.7 recorded seconds and carries
no reliable boundary between one intention and the next, so the burst is the
unit and the name says so. Enough pauses are drawn to outlast the longest run
the trace could be used for.

    python3 make_think_trace.py --users 500 --seed 20260828 \\
        --out docker/runtime/p2_trace.jsonl
"""

import argparse
import hashlib
import json
import random

ACCOUNTS = "load_users.txt"


class ThinkTrace:
    """The whole trace: who enters when, and how long each pause lasts."""

    def __init__(self, arguments):
        self.arguments = arguments
        self.accounts = [line.strip() for line in open(arguments.accounts) if line.strip()]
        if arguments.users > len(self.accounts):
            raise SystemExit(f"{arguments.users} utenti chiesti, "
                             f"{len(self.accounts)} account disponibili in {arguments.accounts}")
        self.rng = random.Random(arguments.seed)

    @property
    def rows(self):
        """One row per user, in entry order."""
        return [
            {
                "user": number + 1,
                "username": self.accounts[number],
                "entry_offset_s": round(number * self.arguments.login_every, 3),
                "think_times": [
                    round(self.rng.uniform(self.arguments.min_think,
                                           self.arguments.max_think), 2)
                    for _ in range(self.arguments.operations)
                ],
            }
            for number in range(self.arguments.users)
        ]

    def write(self):
        rows = self.rows
        with open(self.arguments.out, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        digest = hashlib.sha256(open(self.arguments.out, "rb").read()).hexdigest()
        pauses = [pause for row in rows for pause in row["think_times"]]
        print(f"trace: {len(rows)} users, {self.arguments.operations} pauses each, "
              f"seed {self.arguments.seed}")
        print(f"  entries: one every {self.arguments.login_every}s, "
              f"last at {rows[-1]['entry_offset_s']:.0f}s")
        print(f"  think times: {min(pauses):.1f}..{max(pauses):.1f}s, "
              f"mean {sum(pauses) / len(pauses):.1f}s")
        print(f"  file: {self.arguments.out}")
        print(f"  sha256: {digest}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--accounts", default=ACCOUNTS)
    parser.add_argument("--users", type=int, default=500)
    parser.add_argument("--login-every", type=float, default=3.0)
    parser.add_argument("--min-think", type=float, default=10.0)
    parser.add_argument("--max-think", type=float, default=120.0)
    parser.add_argument("--bursts", type=int, default=200, dest="operations",
                        help="pauses drawn per user, one per burst of recorded "
                             "work: enough to outlast the run")
    parser.add_argument("--seed", type=int, default=20260828)
    ThinkTrace(parser.parse_args()).write()


if __name__ == "__main__":
    main()
