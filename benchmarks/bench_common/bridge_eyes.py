# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Reading the bridge without touching it: the census, the status, the freezer.

Every read here is on a path the pool's demux diverts BEFORE the hosted site,
which is why looking costs nothing and changes nothing:

- ``GET /_server/inspector/census`` never traverses the site, mints no cookie and
  opens no connection;
- ``GET /_orchestration/status`` reads the live setpoints and calls no worker;
- ``POST /_orchestration/apply`` changes the policy and touches no user register.

The one thing that is NOT innocent is ``GET /`` without a cookie: the site coins
a guest, and the guest occupies a slot. No method here ever asks for it.

THE FOUR POPULATION COUNTS. The census offers ``frozen`` as a key and nothing
else: ``authenticated``, ``placed`` and ``unplaced`` are derived. The derivation
has one trap that would silently double-count, and it is closed in ``population``
below: a frozen user is ALSO unplaced, because freezing sets its placement to
None while leaving it in the map. ``unplaced`` here therefore means "not placed
and not frozen" — a user waiting for a worker.

TWO MEASURES THE CORE DOES NOT OFFER, and which this file takes instead:

- the THAW LATENCY. Nothing in the core times a thaw, and a successful freeze
  writes no journal line at all — only a failed one does. The thaw is synchronous
  inside the first request of the returning user, so its cost is the wall clock
  of that request, measured by the driver.
- the SIZE OF THE FROZEN DATA. ``FreezeHandler`` exposes the names of the user
  folders and the free space of the whole filesystem, never the room the freezer
  takes. It is measured here with one ``du`` on the deposit's own directory.
"""

import json
import subprocess
import urllib.request

# Il prefisso con cui il sito battezza un ospite anonimo. E' il criterio che usa
# il codice stesso per decidere il destino di un utente.
GUEST_PREFIX = "guest_"


class CensusUnavailable(RuntimeError):
    """The census did not answer: there is no population reading."""


class BridgeEyes:
    """The bridge's own numbers. Reads, plus one apply. Never load.

    Kept apart from any load engine on purpose: an observation must not be able
    to become part of the traffic being measured.
    """

    def __init__(self, base, census_url=None, journal_path=None, container=None,
                 frozen_users_path=None):
        self.base = base
        self.census_url = census_url or (base + "/_server/inspector/census")
        self.journal_path = journal_path
        self.container = container
        self.frozen_users_path = frozen_users_path

    # ------------------------------------------------------------------ letture
    def read_census(self):
        """The site's census, or None when it did not answer."""
        try:
            with urllib.request.urlopen(self.census_url, timeout=6) as answer:
                return json.load(answer)["site"]
        except Exception:                                        # noqa: BLE001
            return None

    def require_census(self):
        census = self.read_census()
        if census is None:
            raise CensusUnavailable(f"census non leggibile: {self.census_url}")
        return census

    def live_settings(self):
        """effective_settings from /_orchestration/status.

        ``user_idle_freeze_minutes`` comes back as ``null`` when the freeze is
        OFF: the core stores ``math.inf`` and JSON cannot carry it. A null is
        therefore "never freeze", not "freeze at zero".
        """
        with urllib.request.urlopen(self.base + "/_orchestration/status", timeout=15) as answer:
            return json.load(answer)

    def apply_settings(self, settings):
        """Hot application of setpoints. Returns the outcome record."""
        request = urllib.request.Request(
            self.base + "/_orchestration/apply", data=json.dumps(settings).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as answer:
            return json.load(answer)

    def read_journal_events(self):
        """How many times each decision and each reason appears in the journal.

        A SUCCESSFUL freeze appears nowhere: the core logs the order only when
        the worker refuses it. Counting frozen users from this file would count
        only the failures.
        """
        counts = {}
        if not self.journal_path:
            return counts
        try:
            for line in open(self.journal_path):
                if not line.strip():
                    continue
                record = json.loads(line)
                for key in ("decision", "reason"):
                    value = record.get(key)
                    if value:
                        counts[value] = counts.get(value, 0) + 1
        except Exception:                                        # noqa: BLE001
            return counts
        return counts

    # ------------------------------------------------------------------ topologia
    @property
    def topology(self):
        """The living workers and the user->worker map, both sorted."""
        group = self.require_census()["groups"]["pool"]
        return {"workers": sorted(group["living_workers"]),
                "map": dict(group["user_worker_map"])}

    @property
    def worker_pids(self):
        """pid -> worker name. A worker that did not answer has no pid."""
        census = self.read_census() or {}
        out = {}
        for name, entry in (census.get("workers") or {}).items():
            if isinstance(entry, dict) and entry.get("pid") is not None:
                out[str(entry["pid"])] = name
        return out

    @property
    def worker_pid_map(self):
        """worker name -> pid, the way a report wants to read it."""
        return {name: int(pid) for pid, name in self.worker_pids.items()}

    # ------------------------------------------------------------------ popolazione
    def population(self, census=None):
        """The four counts, and the shape behind them.

        ``unplaced`` excludes the frozen: freezing leaves the user in the map with
        a placement of None, so counting "placement is None" would count every
        frozen user a second time.
        """
        census = census or self.read_census()
        if census is None:
            return {}
        group = census["groups"]["pool"]
        user_map = census["user_map"]
        placement = group["user_worker_map"]
        authenticated = [user for user in user_map if not user.startswith(GUEST_PREFIX)]
        guests = [user for user in user_map if user.startswith(GUEST_PREFIX)]
        frozen = [user for user in authenticated if user_map[user].get("frozen")]
        placed = [user for user in authenticated if placement.get(user)]
        frozen_set = set(frozen)
        unplaced = [user for user in authenticated
                    if not placement.get(user) and user not in frozen_set]
        per_worker = {}
        for user in placed:
            worker = placement[user]
            per_worker[worker] = per_worker.get(worker, 0) + 1
        return {
            "authenticated": len(authenticated),
            "placed": len(placed),
            "frozen": len(frozen),
            "unplaced": len(unplaced),
            "guest": len(guests),
            "connections": len(census.get("connection_user_map", {})),
            "pages": len(census.get("page_connection_map", {})),
            "workers": sorted(group["living_workers"]),
            "worker_count": len(group["living_workers"]),
            "per_worker": per_worker,
            "frozen_users": sorted(frozen),
            "memory_occupied_percent": group.get("memory_occupied_percent"),
            "memory_accounting": group.get("memory_accounting"),
        }

    def user_clocks(self, census=None):
        """Per user, the two clocks the freeze judge actually reads.

        ``last_user_ts`` and ``last_rpc_ts`` from each worker's own register —
        the same numbers the group handler compares, so the driver can check how
        idle a user is without making it less idle.
        """
        census = census or self.read_census()
        if census is None:
            return {}
        out = {}
        for worker, entry in (census.get("workers") or {}).items():
            if not isinstance(entry, dict):
                continue
            for user, item in (entry.get("user_register") or {}).items():
                out[user] = {"worker": worker, "state": item.get("state"),
                             "last_user_ts": item.get("last_user_ts"),
                             "last_rpc_ts": item.get("last_rpc_ts"),
                             "last_refresh_ts": item.get("last_refresh_ts")}
        return out

    # ------------------------------------------------------------------ freezer
    def frozen_deposit(self):
        """The room the freezer takes, and how many folders it holds.

        Measured with one ``du -sb`` plus one count inside the container, because
        the core exposes neither. Returns empty when the container or the path
        were not declared: a missing measure is reported as missing, never as a
        zero.
        """
        if not (self.container and self.frozen_users_path):
            return {"available": False, "reason": "container o path non dichiarati"}
        script = (f'if [ -d "{self.frozen_users_path}" ]; then '
                  f'du -sb "{self.frozen_users_path}" | cut -f1; '
                  f'find "{self.frozen_users_path}" -mindepth 1 -maxdepth 1 -type d | wc -l; '
                  f'find "{self.frozen_users_path}" -name "*.pickle" | wc -l; '
                  f'else echo ASSENTE; fi')
        try:
            done = subprocess.run(["docker", "exec", self.container, "sh", "-c", script],
                                  capture_output=True, text=True, timeout=30)
        except Exception as failure:                             # noqa: BLE001
            return {"available": False, "reason": repr(failure)[:120]}
        if done.returncode != 0:
            return {"available": False, "reason": done.stderr.strip()[:120]}
        lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
        if not lines or lines[0] == "ASSENTE":
            return {"available": True, "exists": False, "bytes": 0,
                    "user_folders": 0, "pickles": 0}
        return {"available": True, "exists": True, "bytes": int(lines[0]),
                "user_folders": int(lines[1]), "pickles": int(lines[2]),
                "path": self.frozen_users_path}
