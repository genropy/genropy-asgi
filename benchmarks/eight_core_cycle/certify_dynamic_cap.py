#!/usr/bin/env python3
# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Is there a per-worker user cap in effect? Two sources, one verdict.

The dynamic profile is only valid if NOTHING caps the users a worker may hold: the
pool has to fill a worker by occupancy and decide its own size. Proving that takes
two readings, and the earlier version of this check got the first one wrong.

THE ENVIRONMENT, and why its mere NAME proves nothing. The lab's base Compose
declares ``GNR_ASGI_WORKER_MAX_USERS`` for every bridge profile with an EMPTY
default, so the name is present in the container whatever the profile does — and
``dynamic_recipe.py`` never reads it. Grepping for the name therefore rejects a
correct configuration, which is exactly what happened on 2026-08-31 at 16:15:53.
What matters is the VALUE:

- absent          -> accepted
- present, empty  -> accepted
- present, valued -> REJECTED, and ``0`` is a value like any other

THE LIVE SETPOINT, which is the authoritative one. ``worker_max_users`` read back
from ``/_orchestration/status`` must be ``null``: the core stores its infinity and
JSON cannot carry it, so a null means "no cap". A number there is a cap in force no
matter how clean the environment looks.

Both must agree. An empty environment with a numeric setpoint is a cap, and is
refused.

The rest of the production policy is certified in the same pass, because it is read
from the same document: 50/30 for the CPU growth and its hysteresis, 80 for the
occupancy, 0 for the reception reserve, 60 seconds of retirement quiet — and the
absence of the fixed run's experimental ``worker_min_life_seconds=3600``.

    certify_dynamic_cap.py --env-value "" --status-url http://127.0.0.1:8098
    certify_dynamic_cap.py --env-value "" --status-file /tmp/status.json
"""

import argparse
import json
import sys
import urllib.request

# La policy di produzione, letta viva. Sono gli stessi valori che il driver
# ricontrolla in `certify_settings`: qui si verificano PRIMA che il driver parta.
EXPECTED_POLICY = {
    "cpu_grow_percent": 50.0,
    "cpu_grow_rearm_percent": 30.0,
    "occupancy_max_percent": 80.0,
    "reception_reserved_percent": 0.0,
    "cpu_retirement_quiet_seconds": 60.0,
    "user_idle_freeze_minutes": None,
}
EXPERIMENTAL_MIN_LIFE = 3600.0


class CapCertificate:
    """The verdict on the cap, and on the policy around it."""

    def __init__(self, env_value, settings):
        self.env_value = env_value
        self.settings = settings

    @property
    def env_declares_cap(self):
        """A cap in the environment: a value that is not empty. ``0`` counts."""
        return bool((self.env_value or "").strip())

    @property
    def problems(self):
        """Everything that makes the dynamic profile invalid. Empty means valid."""
        found = []
        if self.env_declares_cap:
            found.append(f"GNR_ASGI_WORKER_MAX_USERS vale "
                         f"{self.env_value.strip()!r}: un cap dichiarato nell'ambiente")
        live = self.settings.get("worker_max_users", "assente")
        if live is not None:
            found.append(f"il setpoint vivo worker_max_users vale {live!r} invece di null: "
                         f"un cap in vigore")
        for key, value in EXPECTED_POLICY.items():
            if self.settings.get(key, "assente") != value:
                found.append(f"{key} vale {self.settings.get(key, 'assente')!r} "
                             f"invece di {value!r}")
        if self.settings.get("worker_min_life_seconds") == EXPERIMENTAL_MIN_LIFE:
            found.append(f"worker_min_life_seconds vale {EXPERIMENTAL_MIN_LIFE}: e' il "
                         f"controllo sperimentale della prova fissa, non la policy reale")
        return found

    @property
    def record(self):
        return {
            "env_value": self.env_value,
            "env_declares_cap": self.env_declares_cap,
            "live_worker_max_users": self.settings.get("worker_max_users", "assente"),
            "live_worker_max_number": self.settings.get("worker_max_number", "assente"),
            "live_settings": self.settings,
            "problemi": self.problems,
        }


def read_settings(arguments):
    """The live setpoints, from the running bridge or from a file."""
    if arguments.status_file:
        payload = json.load(open(arguments.status_file))
    else:
        with urllib.request.urlopen(arguments.status_url.rstrip("/")
                                    + "/_orchestration/status", timeout=15) as answer:
            payload = json.load(answer)
    return payload["effective_settings"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-value", default="",
                        help="il valore di GNR_ASGI_WORKER_MAX_USERS nel container: "
                             "vuoto o assente sono entrambi accettati")
    parser.add_argument("--status-url", default=None,
                        help="la base del bridge, per leggere /_orchestration/status")
    parser.add_argument("--status-file", default=None,
                        help="un documento di status gia' letto, per i test")
    parser.add_argument("--out", default=None, help="dove scrivere il certificato")
    arguments = parser.parse_args(argv)
    if not (arguments.status_url or arguments.status_file):
        raise SystemExit("serve --status-url oppure --status-file")
    certificate = CapCertificate(arguments.env_value, read_settings(arguments))
    record = certificate.record
    if arguments.out:
        with open(arguments.out, "w") as handle:
            json.dump(record, handle, indent=2)
    print(f"  cap nell'ambiente: {record['env_declares_cap']} "
          f"(valore {record['env_value']!r})")
    print(f"  worker_max_users vivo: {record['live_worker_max_users']!r}")
    print(f"  worker_max_number vivo: {record['live_worker_max_number']!r} (tetto, non obiettivo)")
    for problem in record["problemi"]:
        print(f"  STOP: {problem}")
    return 1 if record["problemi"] else 0


if __name__ == "__main__":
    sys.exit(main())
