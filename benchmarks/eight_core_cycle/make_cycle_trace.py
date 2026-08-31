# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Materialise the eight-core cycle both stacks will replay, identically.

Sister of ``l120_comparison/make_trace.py``, and it differs in ONE decision, the
one that decides what the run measures:

THE PACE IS PER USER, NOT GLOBAL. The L120 plan declared a level rate and spread
the requests of that level across the users; when a user went quiet the others
absorbed its share, so the offered rate never moved. Here every active user owns
one request per second of its own, and the plan simply carries no row for a user
that is paused. The offered rate is therefore the count of active users, and the
pause of fifty users lowers it from 120/s to 70/s because fifty users' worth of
rows are absent — not because a rate was rewritten.

The consequence is that the plan, not the driver, holds the shape of the cycle.
A row exists exactly when its user is meant to call.

WHAT THE SEED DRAWS, and it is little on purpose:

- WHICH fifty users pause, and in which order they come back;
- which username each request looks up.

Everything else is arithmetic: user k logs in at second k-1 of the ramp and
calls one second later, every second, until its phase ends. Two users never
share an instant, because each carries a sub-second offset of ``(k-1)/users``:
that is also what one request per second per user really looks like on the wire,
rather than 120 requests landing together on the second.

THE PHASES ARE ALL MANDATORY. The plan declares six call-bearing phases and the
driver plays every one of them: a run that could not play them all is not a
comparison, so it fails instead of reporting a subset. The one thing that may
shrink a phase is the admission guard, which stops the growth of the population
and leaves the phases in place at the population reached.

``full_warmup`` sits between the ramp and the first measure, at full population,
OUTSIDE every measured window: it exists so that the first construction of the
site — paid once by each service process — is not inside a measure. It replaced
``full_stabilize``, which was the same sixty seconds of full traffic under a name
that did not say what the seconds were for.

    python3 make_cycle_trace.py --out traces/cycle_plan.json --seed 20260831
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


class CyclePlan:
    """The whole cycle: its protocol, its paused set, and every request."""

    def __init__(self, arguments):
        self.arguments = arguments
        missing = [path for path in (SESSION_CAPTURE, USERNAMES_ALL) if not os.path.isfile(path)]
        if missing:
            raise SystemExit(f"dipendenze assenti in {BENCHMARKS_DIR}: {missing}")
        self.accounts = [line.strip() for line in open(USERNAMES_ALL) if line.strip()]
        if arguments.users > len(self.accounts):
            raise SystemExit(f"{arguments.users} utenti chiesti, "
                             f"{len(self.accounts)} account in {USERNAMES_ALL}")
        if arguments.paused >= arguments.users:
            raise SystemExit(f"{arguments.paused} utenti in pausa su {arguments.users}: "
                             f"la pausa deve lasciare qualcuno attivo")
        self.capture = [json.loads(line) for line in open(SESSION_CAPTURE) if line.strip()]
        self.rng = random.Random(arguments.seed)
        self.paused_order = self.draw_paused()

    # ------------------------------------------------------------------ estrazioni
    def draw_paused(self):
        """Which users pause, in the order they will come back. Drawn once."""
        users = [f"user_{index + 1}" for index in range(self.arguments.users)]
        chosen = self.rng.sample(users, self.arguments.paused)
        self.rng.shuffle(chosen)
        return chosen

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

    # ------------------------------------------------------------------ istanti
    def offset_of(self, index):
        """The sub-second slot of user ``index`` (0-based), so no two collide."""
        return index / float(self.arguments.users)

    def user_calls(self, index, first_call_at, until):
        """The instants of one user: one per second from ``first_call_at``."""
        out = []
        step = 0
        while True:
            when = first_call_at + step + self.offset_of(index)
            if when >= until:
                return out
            out.append(when)
            step += 1

    # ------------------------------------------------------------------ fasi
    @property
    def phases(self):
        """The six call-bearing phases, with the nominal peak rate of each."""
        users = self.arguments.users
        active = users - self.arguments.paused
        return [
            {"phase": "login_ramp", "rate": float(users),
             "seconds": float(users) * self.arguments.login_period,
             "role": "gli utenti entrano uno ogni periodo di login e chiamano subito dopo"},
            # Il riscaldamento: tutti attivi, FUORI dalle finestre di misura. Serve
            # a togliere dalla misura il costo del primo caricamento del sito, che
            # ogni processo di servizio paga una volta sola. Le sue richieste, i suoi
            # errori, la sua CPU e la sua memoria restano registrati.
            {"phase": "full_warmup", "rate": float(users),
             "seconds": self.arguments.warmup_seconds,
             "role": "tutti attivi, fuori dalla misura: scalda ogni processo"},
            {"phase": "full_measure_1", "rate": float(users),
             "seconds": self.arguments.measure_seconds,
             "role": "la prima misura alla popolazione piena"},
            {"phase": "pause_50", "rate": float(active),
             "seconds": self.arguments.pause_seconds,
             "role": f"{self.arguments.paused} utenti smettono di chiamare, restano residenti"},
            # Un periodo in piu' dei rientri: il primo che rientra a t=0 chiama a
            # t=1, quindi il ritmo pieno arriva un periodo dopo l'ultimo rientro.
            # Senza quel periodo la finestra si chiuderebbe a 119/s e i 120/s
            # cadrebbero esattamente sul confine con la misura successiva.
            {"phase": "return_ramp", "rate": float(users),
             "seconds": (float(self.arguments.paused) + 1.0) * self.arguments.return_period,
             "role": "i fermi rientrano uno ogni periodo: il ritmo sale da 70 a 120"},
            {"phase": "full_measure_2", "rate": float(users),
             "seconds": self.arguments.measure_seconds,
             "role": "la seconda misura, dopo il rientro"},
        ]

    def rows_of_login_ramp(self, window):
        """User k enters at second (k-1)*period and calls one second later."""
        rows = []
        for index in range(self.arguments.users):
            entry = index * self.arguments.login_period
            for when in self.user_calls(index, entry + 1.0, window["seconds"]):
                rows.append((when, index))
        return rows

    def rows_of_all_active(self, window):
        """Every user calling once a second for the whole window."""
        rows = []
        for index in range(self.arguments.users):
            for when in self.user_calls(index, 0.0, window["seconds"]):
                rows.append((when, index))
        return rows

    def rows_of_pause(self, window):
        """Only the users that are not in the paused set."""
        paused = set(self.paused_order)
        rows = []
        for index in range(self.arguments.users):
            if f"user_{index + 1}" in paused:
                continue
            for when in self.user_calls(index, 0.0, window["seconds"]):
                rows.append((when, index))
        return rows

    def rows_of_return_ramp(self, window):
        """The never-paused keep calling; the paused resume one per period."""
        paused_at = {name: position * self.arguments.return_period
                     for position, name in enumerate(self.paused_order)}
        rows = []
        for index in range(self.arguments.users):
            name = f"user_{index + 1}"
            first = 0.0 if name not in paused_at else paused_at[name] + 1.0
            for when in self.user_calls(index, first, window["seconds"]):
                rows.append((when, index))
        return rows

    @property
    def calls(self):
        """One row per request: who, when, which lookup. Sorted by instant.

        The rows of a phase are sorted by instant because the player offers them
        in order and sleeps until each one is due. The sort is total: within a
        phase no two rows share an instant, by construction of the offsets.
        """
        builders = {
            "login_ramp": self.rows_of_login_ramp,
            "full_warmup": self.rows_of_all_active,
            "full_measure_1": self.rows_of_all_active,
            "pause_50": self.rows_of_pause,
            "return_ramp": self.rows_of_return_ramp,
            "full_measure_2": self.rows_of_all_active,
        }
        lookups = self.arguments.lookups
        rows = []
        sequence = 0
        for window in self.phases:
            instants = sorted(builders[window["phase"]](window))
            for when, index in instants:
                rows.append({
                    "phase": window["phase"],
                    "rate": window["rate"],
                    "t_rel": round(when, 6),
                    "user": f"user_{index + 1}",
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
            "kind": "eight_core_cycle_plan",
            "seed": self.arguments.seed,
            "users": self.arguments.users,
            "paused": self.arguments.paused,
            "paused_order": self.paused_order,
            "lookups": self.arguments.lookups,
            "protocol": {
                "baseline_seconds": self.arguments.baseline_seconds,
                "login_period_seconds": self.arguments.login_period,
                "login_attempts_max": self.arguments.login_attempts_max,
                "login_retry_seconds": self.arguments.login_retry_seconds,
                "return_period_seconds": self.arguments.return_period,
                "logout_period_seconds": self.arguments.logout_period,
                "population_timeout_seconds": self.arguments.population_timeout,
                "settle_seconds": self.arguments.settle_seconds,
                "drain_timeout_seconds": self.arguments.drain_timeout,
                "observe_seconds": self.arguments.observe_seconds,
                "admission": {
                    "p95_limit_ms": self.arguments.admission_p95_ms,
                    "consecutive_buckets": self.arguments.admission_consecutive,
                    "minimum_samples": self.arguments.admission_min_samples,
                },
                "phases": self.phases,
            },
            "capture": self.capture_facts,
            "calls_total": len(calls),
            "calls": calls,
        }

    def write(self):
        plan = self.plan
        with open(self.arguments.out, "w") as handle:
            # Compatto, non indentato: e' un artefatto verificato per hash e
            # letto da un programma. Indentato peserebbe tre volte tanto.
            json.dump(plan, handle, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(open(self.arguments.out, "rb").read()).hexdigest()
        base = os.path.basename(self.arguments.out)
        with open(self.arguments.out + ".sha256", "w") as handle:
            handle.write(f"{digest}  {base}\n")
        per_phase = {}
        for call in plan["calls"]:
            per_phase[call["phase"]] = per_phase.get(call["phase"], 0) + 1
        print(f"piano cycle: {plan['calls_total']} richieste, seed {plan['seed']}, "
              f"{plan['users']} utenti, {plan['paused']} in pausa")
        for window in self.phases:
            count = per_phase.get(window["phase"], 0)
            print(f"  {window['phase']:16} {count:7d} richieste in "
                  f"{window['seconds']:.0f}s  picco {window['rate']:.0f}/s "
                  f"media {count / max(window['seconds'], 1e-9):.1f}/s")
        print(f"  in pausa: {', '.join(plan['paused_order'][:5])}"
              f"{' ...' if len(plan['paused_order']) > 5 else ''}")
        print(f"  cattura: {plan['capture']['rows']} righe, "
              f"sha256 {plan['capture']['sha256'][:16]}...")
        print(f"  file: {self.arguments.out}")
        print(f"  sha256: {digest}")
        return digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=20260831)
    parser.add_argument("--users", type=int, default=120)
    parser.add_argument("--paused", type=int, default=50)
    parser.add_argument("--lookups", type=int, default=32,
                        help="quanti username diversi il filtro pesca a rotazione")
    parser.add_argument("--login-period", type=float, default=1.0)
    parser.add_argument("--return-period", type=float, default=1.0)
    parser.add_argument("--logout-period", type=float, default=1.0)
    parser.add_argument("--warmup-seconds", type=float, default=60.0,
                        help="il riscaldamento a popolazione piena, fuori dalla misura")
    parser.add_argument("--login-attempts-max", type=int, default=3,
                        help="quanti tentativi per ogni login, connessione nuova ogni volta")
    parser.add_argument("--login-retry-seconds", type=float, default=2.0)
    parser.add_argument("--measure-seconds", type=float, default=120.0)
    parser.add_argument("--pause-seconds", type=float, default=60.0)
    parser.add_argument("--baseline-seconds", type=float, default=60.0)
    parser.add_argument("--settle-seconds", type=float, default=20.0,
                        help="la quiete fra la fine dei login e la prima finestra")
    parser.add_argument("--population-timeout", type=float, default=900.0)
    parser.add_argument("--drain-timeout", type=float, default=120.0)
    parser.add_argument("--observe-seconds", type=float, default=120.0)
    parser.add_argument("--admission-p95-ms", type=float, default=1500.0)
    parser.add_argument("--admission-consecutive", type=int, default=15,
                        help="quanti secondi consecutivi oltre soglia chiudono la porta")
    parser.add_argument("--admission-min-samples", type=int, default=5,
                        help="sotto questo numero di chiamate il secondo non porta verdetto")
    arguments = parser.parse_args(argv)
    CyclePlan(arguments).write()
    return 0


if __name__ == "__main__":
    sys.exit(main())
