# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Materialise the population plan both stacks will replay, identically.

Two thousand authenticated users, eighty of them working, and the rest silent.
Everything that is a DRAW is written into this file before any run: who enters
when, how long each pause lasts, and — the part the previous campaign left to a
seeded call at run time — exactly WHICH users leave the working set and which
enter it, at which instant. Legacy and bridge then replay the same file, and the
two rotations are the same rotation.

The phases the plan describes, in order:

1. ``populate``    — the users enter, one a second
2. ``rest``        — silence longer than the freeze window
3. ``baseline_80`` — the initial working set measured on its own
4. one STEP per population: ``thaw_N`` (ten users come back, one a second),
   ``settle_N`` (twenty seconds of quiet), ``measure_N`` (sixty seconds measured)
5. ``logout``      — everybody leaves
6. ``observe``     — silence, to watch the memory come back

THE RAMP IS MONOTONIC. A woken user stays awake: the population climbs 80, 90,
100, … and never comes back down. That is what makes each step a capacity reading
rather than a rotation — the previous protocol swapped users in and out and kept
the working set the same size, which measures churn, not capacity.

THE STEPS ARE DRAWN ONCE. Which ten users each group holds, and in which order
they come back, is written here: legacy and bridge then wake the same identities
in the same order, and a difference between the two legs cannot be a difference of
draw.

WHY THE REST IS LONGER THAN THE FREEZE WINDOW, and by that much: the bridge
checks user activity every twelve beats of five seconds — sixty seconds — and
the photograph it judges is up to five seconds old. A freeze declared at five
minutes therefore happens somewhere between five minutes and about six minutes
and five seconds after the last call. The default rest is seven minutes, which
clears that band with margin. Those cadences are module constants of the core,
not configuration: the uncertainty cannot be reduced, only cleared.

THE SILENT USERS SEND NOTHING. Not a request, not a ping. This is a property of
the driver, and it is what makes the freeze measurable: ``churn_driver``'s
``LoggedUser`` has no pinger of its own, unlike ``session_bench``'s emulated
user, and the bridge lets a client push its own activity clock forward through
``/_ping`` — so a driver that pinged would be deciding its own freeze.

    python3 make_population_trace.py --out traces/population_full.json \\
        --users 2000 --working-set 80 --seed 20260831
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
SCENARIO_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS = os.path.join(SCENARIO_DIR, "accounts", "load_users.txt")
SESSION_CAPTURE = os.path.join(BENCHMARKS_DIR, "session_capture.jsonl")


class PopulationPlan:
    """The whole run: who enters when, who works, who swaps with whom."""

    def __init__(self, arguments):
        self.arguments = arguments
        for path in (arguments.accounts, SESSION_CAPTURE):
            if not os.path.isfile(path):
                raise SystemExit(f"dipendenza assente: {path}")
        self.accounts = [line.strip() for line in open(arguments.accounts) if line.strip()]
        self.check_accounts()
        if arguments.users > len(self.accounts):
            raise SystemExit(f"{arguments.users} utenti chiesti, "
                             f"{len(self.accounts)} account in {arguments.accounts}")
        if arguments.working_set > arguments.users:
            raise SystemExit(f"working set {arguments.working_set} "
                             f"maggiore della popolazione {arguments.users}")
        self.rng = random.Random(arguments.seed)

    def check_accounts(self):
        """The account file is a fact of the run: it is checked, not trusted."""
        problems = []
        if len(self.accounts) != len(set(self.accounts)):
            duplicates = len(self.accounts) - len(set(self.accounts))
            problems.append(f"{duplicates} username duplicati")
        for number, name in enumerate(self.accounts, start=1):
            if not name.isascii() or " " in name or ":" in name:
                problems.append(f"riga {number}: formato inatteso {name!r}")
                break
        suspicious = [name for name in self.accounts
                      if any(word in name.lower()
                             for word in ("password", "passwd", "secret", "token", "key="))]
        if suspicious:
            problems.append(f"righe sospette di contenere un segreto: {suspicious[:3]}")
        if problems:
            raise SystemExit("account non validi in %s:\n  %s"
                             % (self.arguments.accounts, "\n  ".join(problems)))

    @property
    def accounts_facts(self):
        return {
            "path": os.path.relpath(self.arguments.accounts, SCENARIO_DIR),
            "sha256": hashlib.sha256(open(self.arguments.accounts, "rb").read()).hexdigest(),
            "rows": len(self.accounts),
            "unique": len(set(self.accounts)),
            "first": self.accounts[:3],
            "last": self.accounts[-3:],
        }

    @property
    def entries(self):
        """One row per user: the account, and when it enters.

        The ramp is not a draw: one arrival every ``--arrival-gap`` seconds is a
        fact of the protocol. The pauses are draws, and they are written here.
        """
        return [
            {
                "user": number + 1,
                "label": f"user_{number + 1}",
                "username": self.accounts[number],
                "entry_offset_s": round(number * self.arguments.arrival_gap, 3),
                "think_times": [round(self.rng.uniform(self.arguments.min_think,
                                                       self.arguments.max_think), 2)
                                for _ in range(self.arguments.bursts)],
            }
            for number in range(self.arguments.users)
        ]

    @property
    def working_set(self):
        """The users active from the baseline on, in the order they are woken."""
        labels = [f"user_{number + 1}" for number in range(self.arguments.users)]
        return self.rng.sample(labels, self.arguments.working_set)

    def ramp(self, working_set):
        """The steps: which ten users wake at each one, and when.

        Drawn once and written out, so the two legs wake the same identities in
        the same order. The population climbs by ``--group-size`` per step and
        never falls: a woken user stays awake for every later step.

        The last step is the one that would exceed ``--max-active``, or the one
        that runs out of idle users — whichever comes first. Nothing here decides
        where the run actually stops: the guards do that, at run time, and the
        plan only says how far the steps are allowed to go.
        """
        idle = [f"user_{number + 1}" for number in range(self.arguments.users)
                if f"user_{number + 1}" not in set(working_set)]
        self.rng.shuffle(idle)
        size = self.arguments.group_size
        steps, active = [], len(working_set)
        while active + size <= self.arguments.max_active and len(idle) >= size:
            group = [idle.pop(0) for _ in range(size)]
            active += size
            steps.append({
                "step": len(steps) + 1,
                "target_active": active,
                "wake": group,
                "wake_gap_s": self.arguments.wake_gap,
                "thaw_seconds": round(size * self.arguments.wake_gap, 3),
                "settle_seconds": self.arguments.settle_seconds,
                "measure_seconds": self.arguments.measure_seconds,
            })
        return steps

    @property
    def plan(self):
        entries = self.entries
        working_set = self.working_set
        steps = self.ramp(working_set)
        return {
            "kind": "population_ramp_plan",
            "seed": self.arguments.seed,
            "users": self.arguments.users,
            "working_set_size": self.arguments.working_set,
            "max_active": self.arguments.max_active,
            "group_size": self.arguments.group_size,
            "steps_declared": len(steps),
            "protocol": {
                "arrival_gap_s": self.arguments.arrival_gap,
                "batch": self.arguments.batch,
                "batch_settle_s": self.arguments.batch_settle,
                "populate_timeout_s": self.arguments.populate_timeout,
                "freeze_minutes": self.arguments.freeze_minutes,
                "rest_seconds": self.arguments.rest_seconds,
                "freeze_deadline_s": self.arguments.freeze_deadline,
                "baseline_seconds": self.arguments.baseline_seconds,
                "wake_gap_s": self.arguments.wake_gap,
                "settle_seconds": self.arguments.settle_seconds,
                "measure_seconds": self.arguments.measure_seconds,
                "hold_after_stop_seconds": self.arguments.hold_after_stop,
                "logout_gap_s": self.arguments.logout_gap,
                "observe_seconds": self.arguments.observe_seconds,
                "login_attempts_max": self.arguments.login_attempts_max,
                "login_retry_seconds": self.arguments.login_retry_seconds,
                "admission": {
                    "p95_limit_ms": self.arguments.admission_p95_ms,
                    "consecutive_buckets": self.arguments.admission_consecutive,
                    "minimum_samples_ratio": self.arguments.admission_min_ratio,
                },
                "generator": {
                    "started_ratio_min": self.arguments.generator_started_min,
                    "lateness_drift_limit_s": self.arguments.generator_drift_limit,
                    "server_fast_p95_ms": self.arguments.generator_server_fast_ms,
                },
                "freeze_granularity_note": (
                    "il vertice giudica l'inattivita' ogni 12 battiti da 5s = 60s, "
                    "e la foto del worker e' vecchia fino a 5s: un freeze a N minuti "
                    "avviene fra N e N+1:05 dal silenzio"),
            },
            "accounts": self.accounts_facts,
            "capture": {
                "path": os.path.relpath(SESSION_CAPTURE, BENCHMARKS_DIR),
                "sha256": hashlib.sha256(open(SESSION_CAPTURE, "rb").read()).hexdigest(),
            },
            "entries": entries,
            "working_set": working_set,
            "steps": steps,
        }

    def write(self):
        plan = self.plan
        with open(self.arguments.out, "w") as handle:
            # Compatto, non indentato: e' un artefatto verificato per hash e
            # letto da un programma, non un file da leggere a occhio. Indentato
            # peserebbe tre volte tanto nel repository.
            json.dump(plan, handle, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(open(self.arguments.out, "rb").read()).hexdigest()
        with open(self.arguments.out + ".sha256", "w") as handle:
            handle.write(f"{digest}  {os.path.basename(self.arguments.out)}\n")
        pauses = [pause for entry in plan["entries"] for pause in entry["think_times"]]
        rest = plan["protocol"]["rest_seconds"]
        freeze = plan["protocol"]["freeze_minutes"] * 60
        print(f"piano popolazione: {plan['users']} utenti, working set "
              f"{plan['working_set_size']}, seed {plan['seed']}")
        print(f"  ingressi: uno ogni {self.arguments.arrival_gap}s, "
              f"ultimo a {plan['entries'][-1]['entry_offset_s']:.0f}s "
              f"({plan['entries'][-1]['entry_offset_s'] / 60:.1f} min)")
        print(f"  pause: {min(pauses):.1f}..{max(pauses):.1f}s, "
              f"media {sum(pauses) / len(pauses):.1f}s, "
              f"{self.arguments.bursts} per utente")
        print(f"  freeze a {freeze:.0f}s, primo riposo {rest:.0f}s "
              f"(margine {rest - freeze - 65:.0f}s oltre la banda di incertezza)")
        steps = plan["steps"]
        print(f"  rampa: {len(steps)} gradini da {plan['group_size']} utenti, "
              f"da {plan['working_set_size']} a "
              f"{steps[-1]['target_active'] if steps else plan['working_set_size']} attivi "
              f"(tetto {plan['max_active']})")
        if steps:
            durata = sum(s["thaw_seconds"] + s["settle_seconds"] + s["measure_seconds"]
                         for s in steps)
            print(f"  ogni gradino: {steps[0]['thaw_seconds']:.0f}s di risveglio + "
                  f"{steps[0]['settle_seconds']:.0f}s di assestamento + "
                  f"{steps[0]['measure_seconds']:.0f}s misurati")
            print(f"  durata della sola rampa: {durata / 60:.0f} minuti")
            print(f"  primi tre risvegli del gradino 1: {steps[0]['wake'][:3]}")
        print(f"  account: {plan['accounts']['rows']} righe, "
              f"{plan['accounts']['unique']} distinte, sha256 "
              f"{plan['accounts']['sha256'][:16]}...")
        print(f"  file: {self.arguments.out}")
        print(f"  sha256: {digest}")
        return digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--accounts", default=ACCOUNTS)
    parser.add_argument("--seed", type=int, default=20260831)
    parser.add_argument("--users", type=int, default=2000)
    parser.add_argument("--working-set", type=int, default=80)
    parser.add_argument("--arrival-gap", type=float, default=1.0)
    parser.add_argument("--batch", type=int, default=100)
    parser.add_argument("--batch-settle", type=float, default=20.0)
    parser.add_argument("--populate-timeout", type=float, default=5400.0)
    parser.add_argument("--freeze-minutes", type=float, default=5.0)
    parser.add_argument("--rest-seconds", type=float, default=420.0)
    parser.add_argument("--freeze-deadline", type=float, default=600.0,
                        help="oltre questo, la stabilizzazione del freeze e' fallita")
    parser.add_argument("--baseline-seconds", type=float, default=120.0)
    parser.add_argument("--group-size", type=int, default=10)
    parser.add_argument("--max-active", type=int, default=500,
                        help="tetto di sicurezza: la rampa non lo supera")
    parser.add_argument("--wake-gap", type=float, default=1.0,
                        help="un utente al secondo dentro un gradino")
    parser.add_argument("--settle-seconds", type=float, default=20.0)
    parser.add_argument("--measure-seconds", type=float, default=60.0)
    parser.add_argument("--hold-after-stop", type=float, default=30.0,
                        help="quanto si tiene la popolazione dopo un ADMISSION_STOP")
    parser.add_argument("--logout-gap-s", dest="logout_gap", type=float, default=0.2)
    parser.add_argument("--observe-seconds", type=float, default=300.0)
    parser.add_argument("--login-attempts-max", type=int, default=5)
    parser.add_argument("--login-retry-seconds", type=float, default=5.0)
    parser.add_argument("--admission-p95-ms", type=float, default=1500.0)
    parser.add_argument("--admission-consecutive", type=int, default=15)
    parser.add_argument("--admission-min-ratio", type=float, default=0.25,
                        help="frazione degli utenti attivi che deve comparire in un "
                             "bucket da un secondo perche' porti un verdetto")
    parser.add_argument("--generator-started-min", type=float, default=0.80,
                        help="PAVIMENTO largo, non una soglia: il rapporto delle "
                             "avviate ha un errore di bordo di qualche punto, e un "
                             "valore vicino a 1 ci cadrebbe dentro. Serve solo a "
                             "cogliere residenti morti, non un driver in ritardo — "
                             "quello lo coglie la lateness")
    parser.add_argument("--generator-drift-limit", type=float, default=5.0)
    parser.add_argument("--generator-server-fast-ms", type=float, default=500.0)
    parser.add_argument("--min-think", type=float, default=10.0)
    parser.add_argument("--max-think", type=float, default=120.0)
    parser.add_argument("--bursts", type=int, default=200,
                        help="pause estratte per utente, una per raffica")
    arguments = parser.parse_args(argv)
    if arguments.rest_seconds <= arguments.freeze_minutes * 60 + 65:
        raise SystemExit(
            f"il riposo di {arguments.rest_seconds:.0f}s non supera la banda di "
            f"incertezza del freeze ({arguments.freeze_minutes * 60 + 65:.0f}s): "
            f"la prova non potrebbe distinguere un congelato da un utente ancora attivo")
    PopulationPlan(arguments).write()
    return 0


if __name__ == "__main__":
    sys.exit(main())
