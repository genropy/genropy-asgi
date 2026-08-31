# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Materialise the L120 trace both stacks will replay, identically.

The trace is written BEFORE any run and read by both legs, so legacy and bridge
receive the same sequence: the same accounts, the same instants, the same
lookups, in the same order. A seed alone would not be enough — it would tie the
draws to the order in which the code happens to ask for them — so the draws are
materialised into a file, and the FILE is what the two legs share. This is the
same reasoning as ``make_think_trace.py`` of the previous campaign, and the
reason the sensitivity trace could never be reproduced: it was materialised by
hand and no tool was left behind.

WHAT IS DERIVED FROM THE CAPTURE, AND WHAT IS NOT — read this before trusting
the word "deterministic":

- the LOGIN sequence comes from ``session_capture.jsonl`` verbatim, through
  ``churn_driver``/``replay_a1``'s ``build_plan``: the two recorded login calls,
  replayed with the identity of each account. The trace does not carry it,
  because it is not a draw: it is a fact of the capture, read at run time.
- the LOAD unit is the indexed ``app.getSelection`` on ``adm.user`` that every
  run of this campaign has used — the one ``LoggedUser.get_call_form`` builds
  from ``single_record_bench.WHERE``. It is one row, one indexed query, an
  envelope of some 700 bytes: cheap enough that the measure is the stack's and
  not PostgreSQL's. The capture's own heavy forms are NOT replayed.
- the DRAWS are which user issues each request and which username it looks up.
  Those are seeded, and materialised here.

PROVISIONAL, and the owner's to change: replaying the capture's own
``app.getSelection`` forms instead of the ``adm.user`` one would be more
faithful to a browser and less comparable with everything measured so far. The
generator keeps the second choice because comparability across the campaign is
what the L120 figure is for.

The plan carries the protocol as well as the calls, so that the two legs cannot
drift on the shape of the run — durations, login cadence, drain and observation
are read from the file, not from each runner's own constants.

    python3 make_trace.py --out traces/l120_plan.json --seed 20260831
"""

import argparse
import hashlib
import json
import os
import random
import sys

BENCHMARKS_DIR = os.path.abspath(
    os.environ.get("BENCHMARKS_DIR")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
SESSION_CAPTURE = os.path.join(BENCHMARKS_DIR, "session_capture.jsonl")
USERNAMES_ALL = os.path.join(BENCHMARKS_DIR, "usernames_all.txt")


class L120Plan:
    """The whole run: its protocol, and every request with its instant."""

    def __init__(self, arguments):
        self.arguments = arguments
        missing = [path for path in (SESSION_CAPTURE, USERNAMES_ALL) if not os.path.isfile(path)]
        if missing:
            raise SystemExit(f"dipendenze assenti in {BENCHMARKS_DIR}: {missing}")
        self.accounts = [line.strip() for line in open(USERNAMES_ALL) if line.strip()]
        if arguments.users > len(self.accounts):
            raise SystemExit(f"{arguments.users} utenti chiesti, "
                             f"{len(self.accounts)} account in {USERNAMES_ALL}")
        self.capture = [json.loads(line) for line in open(SESSION_CAPTURE) if line.strip()]
        self.rng = random.Random(arguments.seed)

    @property
    def capture_facts(self):
        """What the capture contributes, recorded so the plan can be audited."""
        logins = [row for row in self.capture
                  if row.get("rpc_method") and "login" in row["rpc_method"].lower()]
        selections = [row for row in self.capture if row.get("rpc_method") == "app.getSelection"]
        return {
            "path": os.path.relpath(SESSION_CAPTURE, BENCHMARKS_DIR),
            "sha256": hashlib.sha256(open(SESSION_CAPTURE, "rb").read()).hexdigest(),
            "rows": len(self.capture),
            "login_calls": len(logins),
            "get_selection_rows": len(selections),
            "load_unit": "app.getSelection su adm.user, una riga, filtro per username",
            "load_unit_source": "churn_driver.LoggedUser.get_call_form / single_record_bench.WHERE",
        }

    @property
    def windows(self):
        """The three windows, in the order they are played."""
        return [
            {"phase": "warmup", "rate": self.arguments.warmup_rate,
             "seconds": self.arguments.warmup_seconds},
            {"phase": "stabilize_L120", "rate": self.arguments.level_rate,
             "seconds": self.arguments.stabilize_seconds},
            {"phase": "measure_L120", "rate": self.arguments.level_rate,
             "seconds": self.arguments.measure_seconds},
        ]

    def draw_users(self, count):
        """Users spread evenly, then shuffled: no user starves, order is a draw."""
        users = [f"user_{index + 1}" for index in range(self.arguments.users)]
        spread = (users * (count // len(users) + 1))[:count]
        self.rng.shuffle(spread)
        return spread

    @property
    def calls(self):
        """One row per request: who, when, at what declared rate, which lookup."""
        lookups = self.arguments.lookups
        rows = []
        sequence = 0
        for window in self.windows:
            count = int(round(window["rate"] * window["seconds"]))
            owners = self.draw_users(count)
            for index in range(count):
                rows.append({
                    "phase": window["phase"],
                    "rate": window["rate"],
                    "t_rel": round(index / window["rate"], 6),
                    "user": owners[index],
                    "lookup_index": self.rng.randrange(lookups),
                    "op": "app.getSelection",
                    "seq": sequence,
                })
                sequence += 1
        return rows

    @property
    def plan(self):
        calls = self.calls
        return {
            "kind": "l120_comparison_plan",
            "seed": self.arguments.seed,
            "users": self.arguments.users,
            "lookups": self.arguments.lookups,
            "protocol": {
                "baseline_seconds": self.arguments.baseline_seconds,
                "login_period_seconds": self.arguments.login_period,
                "population_timeout_seconds": self.arguments.population_timeout,
                "apply_wait_seconds": self.arguments.apply_wait,
                "drain_timeout_seconds": self.arguments.drain_timeout,
                "observe_seconds": self.arguments.observe_seconds,
                "windows": self.windows,
            },
            "capture": self.capture_facts,
            "calls_total": len(calls),
            "calls": calls,
        }

    def write(self):
        plan = self.plan
        with open(self.arguments.out, "w") as handle:
            # Compatto, non indentato: e' un artefatto verificato per hash e
            # letto da un programma, non un file da leggere a occhio. Indentato
            # peserebbe tre volte tanto nel repository.
            json.dump(plan, handle, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(open(self.arguments.out, "rb").read()).hexdigest()
        base = os.path.basename(self.arguments.out)
        with open(self.arguments.out + ".sha256", "w") as handle:
            handle.write(f"{digest}  {base}\n")
        per_window = {window["phase"]: sum(1 for call in plan["calls"]
                                           if call["phase"] == window["phase"])
                      for window in self.windows}
        print(f"piano L120: {plan['calls_total']} richieste, seed {plan['seed']}, "
              f"{plan['users']} utenti")
        for window in self.windows:
            print(f"  {window['phase']:15} {per_window[window['phase']]:6d} richieste "
                  f"a {window['rate']:.0f}/s per {window['seconds']:.0f}s")
        print(f"  cattura: {plan['capture']['rows']} righe, "
              f"{plan['capture']['login_calls']} chiamate di login, "
              f"sha256 {plan['capture']['sha256'][:16]}...")
        print(f"  file: {self.arguments.out}")
        print(f"  sha256: {digest}")
        return digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=20260831)
    parser.add_argument("--users", type=int, default=48)
    parser.add_argument("--lookups", type=int, default=32,
                        help="quanti username diversi il filtro pesca a rotazione")
    parser.add_argument("--level-rate", type=float, default=120.0)
    parser.add_argument("--warmup-rate", type=float, default=40.0)
    parser.add_argument("--warmup-seconds", type=float, default=30.0)
    parser.add_argument("--stabilize-seconds", type=float, default=30.0)
    parser.add_argument("--measure-seconds", type=float, default=120.0)
    parser.add_argument("--baseline-seconds", type=float, default=60.0)
    parser.add_argument("--login-period", type=float, default=3.0)
    parser.add_argument("--population-timeout", type=float, default=900.0)
    parser.add_argument("--apply-wait", type=float, default=80.0)
    parser.add_argument("--drain-timeout", type=float, default=120.0)
    parser.add_argument("--observe-seconds", type=float, default=120.0)
    arguments = parser.parse_args(argv)
    L120Plan(arguments).write()
    return 0


if __name__ == "__main__":
    sys.exit(main())
