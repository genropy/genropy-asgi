# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""What a container is made of, read from the kernel: processes, roles, gauges.

One ``docker exec`` per sample walks ``/proc`` inside the container and reads the
cgroup controllers. The script is transcribed from the one proven in
``worker_sensitivity/worker_probe.py``: a single round trip, because a sample
taken with six round trips is six different instants.

The measure is the same for both stacks; only the NAMING of the processes
differs, and that is the whole job of a role map:

- the bridge has a commander, a template and its pool workers;
- the legacy has the register daemon, the Gunicorn master and its workers.

A role map never changes what is read, only what each process is called. Every
sample carries the full command line as well, so a role assigned wrongly is
visible in the data instead of hidden by it.

PROVISIONAL: the legacy role map distinguishes the Gunicorn master from its
workers by parentage, not by process title. Gunicorn rewrites its title only
when ``setproctitle`` is installed, and the lab image does not install it, so
master and workers share one command line. ``certify`` exists for that reason:
the runner declares the shape it expects and the run stops if the classification
does not produce it.
"""

import json
import subprocess

# Un solo giro: /proc processo per processo, poi i gauge del cgroup.
READ_SCRIPT = (
    r'for d in /proc/[0-9]*; do p=${d#/proc/}; [ -r $d/stat ] || continue; '
    r'ppid=$(awk "{print \$4}" $d/stat 2>/dev/null); '
    r'ut=$(awk "{print \$14+\$15}" $d/stat 2>/dev/null); '
    r'cmd=$(tr "\0" " " < $d/cmdline 2>/dev/null); '
    r'rss=$(awk "/^VmRSS/{print \$2}" $d/status 2>/dev/null); '
    r'sm=$(awk "/^Pss:/{a+=\$2} /^Private_Clean:/{b+=\$2} /^Private_Dirty:/{c+=\$2} '
    r'/^Shared_Clean:/{e+=\$2} /^Shared_Dirty:/{f+=\$2} END{printf \"%d,%d,%d,%d,%d\",a,b,c,e,f}" '
    r'$d/smaps_rollup 2>/dev/null); '
    r'echo "P|$p|$ppid|${rss:-0}|${sm:-0,0,0,0,0}|${ut:-0}|$cmd"; done; '
    r'echo "C|$(cat /sys/fs/cgroup/memory.current)|$(cat /sys/fs/cgroup/memory.peak)|'
    r'$(cat /sys/fs/cgroup/memory.max)"; '
    r'echo "E|$(tr "\n" ";" < /sys/fs/cgroup/memory.events)"; '
    r'awk "/^(anon|file|kernel|sock|shmem) /{printf \"S|%s|%s\n\",\$1,\$2}" /sys/fs/cgroup/memory.stat; '
    r'awk "/usage_usec/{printf \"U|%s\n\",\$2}" /sys/fs/cgroup/cpu.stat')


class BridgeRoles:
    """commander, template, and one role per pool worker.

    The pool worker names come from the census, which knows each worker's pid:
    the process tree alone cannot say which child is ``pool_0003``.
    """

    name = "bridge"
    expected_singletons = ("commander", "template")

    def __init__(self, worker_pids=None):
        self.worker_pids = dict(worker_pids or {})

    def role_of(self, pid, ppid, cmd):
        named = self.worker_pids.get(pid)
        if named:
            return named
        if "gnrasgiserve" in cmd:
            return "commander"
        if "template_entry" in cmd:
            return "template" if ppid == "1" else "worker_unknown"
        return "other"

    def aggregate_names(self, roles):
        """Which roles are summed into the 'workers' column of a sample."""
        return sorted(name for name in roles if name.startswith("pool_"))


GUNICORN_SIGNATURE = "gnrserveprod"
DAEMON_SIGNATURE = "gnrdaemon"


class LegacyRoles:
    """daemon, gunicorn master, gunicorn workers.

    The Gunicorn master is the ``gnrserveprod`` process that has no
    ``gnrserveprod`` parent; every ``gnrserveprod`` child of it is a worker,
    numbered by pid order so the same process keeps the same name across samples
    of one run.

    Two topologies produce that shape, and the rule covers both. The lab's
    entrypoint ``exec``s Gunicorn, so the master IS pid 1 and its workers carry
    ``ppid=1``; behind an init or a shell the master is a child of pid 1 instead.
    Parentage towards init therefore cannot name the master: the earlier rule
    ``gnrserveprod`` with ``ppid == "1"`` matched the four workers of the first
    topology and left the master in ``other``.
    """

    name = "legacy"
    expected_singletons = ("daemon", "gunicorn_master")

    def __init__(self):
        self.assigned = {}

    def prepare(self, raw):
        """Decide the names once per sample, from the whole process table."""
        self.assigned = {}
        gunicorn = {pid: ppid for pid, ppid, cmd in raw if GUNICORN_SIGNATURE in cmd}
        for pid, ppid, cmd in raw:
            if DAEMON_SIGNATURE in cmd:
                self.assigned[pid] = "daemon"
        master = self.get_master_pid(gunicorn)
        if master is None:
            return
        self.assigned[master] = "gunicorn_master"
        children = sorted(int(pid) for pid, ppid in gunicorn.items()
                          if ppid == master and pid != master)
        for number, pid in enumerate(children, start=1):
            self.assigned[str(pid)] = f"gunicorn_worker_{number:02d}"

    def get_master_pid(self, gunicorn):
        """The Gunicorn process with no Gunicorn parent, pid 1 included."""
        if "1" in gunicorn:
            return "1"
        rootmost = sorted(int(pid) for pid, ppid in gunicorn.items() if ppid not in gunicorn)
        return str(rootmost[0]) if rootmost else None

    def role_of(self, pid, ppid, cmd):
        return self.assigned.get(pid, "other")

    def aggregate_names(self, roles):
        return sorted(name for name in roles if name.startswith("gunicorn_worker_"))


class ContainerProbe:
    """One container, sampled from the kernel, with roles the caller chooses."""

    def __init__(self, container, roles):
        self.container = container
        self.roles = roles

    def read(self):
        """One sample: processes with their roles, cgroup gauges, cpu usage.

        Returns ``(processes, cgroup, memory_stat, cpu_usec)``. On a failed
        ``docker exec`` it returns empty structures and ``None``: the caller
        decides whether a missing sample invalidates the run, because during a
        deliberate container recreation a gap is expected.
        """
        try:
            done = subprocess.run(["docker", "exec", self.container, "sh", "-c", READ_SCRIPT],
                                  capture_output=True, text=True, timeout=20)
        except Exception:                                        # noqa: BLE001
            return {}, {}, {}, None
        if done.returncode != 0:
            return {}, {}, {}, None
        return self.parse(done.stdout)

    def parse(self, out):
        """The text of one read, turned into structures. Pure: testable offline."""
        raw, cgroup, stat, cpu_usec = [], {}, {}, None
        rows = []
        for line in out.splitlines():
            parts = line.split("|")
            if parts[0] == "P" and len(parts) >= 7:
                cmd = "|".join(parts[6:]).strip()
                if not cmd or cmd.startswith("sh -c for d in"):
                    continue
                rows.append(parts)
                raw.append((parts[1], parts[2], cmd))
            elif parts[0] == "C":
                cgroup.update(current=parts[1], peak=parts[2], max=parts[3])
            elif parts[0] == "E":
                cgroup["events"] = parts[1].strip(";")
            elif parts[0] == "S":
                stat[parts[1]] = parts[2]
            elif parts[0] == "U":
                cpu_usec = int(parts[1])
        if hasattr(self.roles, "prepare"):
            self.roles.prepare(raw)
        processes = {}
        for parts in rows:
            cmd = "|".join(parts[6:]).strip()
            pss, pclean, pdirty, sclean, sdirty = (int(v) for v in parts[4].split(","))
            pid, ppid = parts[1], parts[2]
            processes[pid] = {
                "pid": pid, "ppid": ppid, "rss_kb": int(parts[3] or 0), "pss_kb": pss,
                "private_clean_kb": pclean, "private_dirty_kb": pdirty,
                "shared_clean_kb": sclean, "shared_dirty_kb": sdirty,
                "ticks": int(parts[5] or 0), "cmd": cmd,
                "role": self.roles.role_of(pid, ppid, cmd),
            }
        return processes, cgroup, stat, cpu_usec

    def certify(self, processes, expected_workers):
        """The shape the runner declared, or the list of what is wrong.

        An empty list means the classification produced exactly one process for
        each expected singleton role and ``expected_workers`` worker processes.
        """
        problems = []
        roles = [info["role"] for info in processes.values()]
        for singleton in self.roles.expected_singletons:
            found = roles.count(singleton)
            if found != 1:
                problems.append(f"{singleton}: {found} processi invece di 1")
        workers = self.roles.aggregate_names(set(roles))
        if len(workers) != expected_workers:
            problems.append(f"worker: {len(workers)} invece di {expected_workers} ({workers})")
        unknown = [info["cmd"][:60] for info in processes.values()
                   if info["role"] in ("other", "worker_unknown")]
        if unknown:
            problems.append(f"processi non classificati: {unknown}")
        return problems


class RoleSample:
    """The per-role numbers of one instant, ready for a CSV row.

    CPU is a difference between two instants, so this object needs the previous
    tick counts: it is built once per sample and asked for its columns.
    """

    def __init__(self, processes, cgroup, memory_stat, cpu_usec, roles,
                 previous_ticks=None, previous_cpu_usec=None, elapsed=None):
        self.processes = processes
        self.cgroup = cgroup
        self.memory_stat = memory_stat
        self.cpu_usec = cpu_usec
        self.roles = roles
        self.previous_ticks = previous_ticks or {}
        self.previous_cpu_usec = previous_cpu_usec
        self.elapsed = elapsed

    @property
    def ticks(self):
        """This instant's tick counts, to become the next sample's baseline."""
        return {pid: info["ticks"] for pid, info in self.processes.items()}

    @property
    def cpu_total_percent(self):
        """The container's CPU over the interval, from the cgroup, or None."""
        if self.cpu_usec is None or self.previous_cpu_usec is None or not self.elapsed:
            return None
        return round((self.cpu_usec - self.previous_cpu_usec) / 10000.0 / self.elapsed, 2)

    def cpu_by_role(self):
        """Percent of one core per role over the interval, summed per role."""
        if not self.elapsed:
            return {}
        out = {}
        for pid, info in self.processes.items():
            before = self.previous_ticks.get(pid)
            if before is None:
                continue
            percent = (info["ticks"] - before) / 100.0 / self.elapsed * 100.0
            out[info["role"]] = round(out.get(info["role"], 0.0) + percent, 1)
        return out

    def pss_by_role(self):
        """PSS in kilobytes per role, summed per role."""
        out = {}
        for info in self.processes.values():
            out[info["role"]] = out.get(info["role"], 0) + info["pss_kb"]
        return out

    def rss_by_role(self):
        out = {}
        for info in self.processes.values():
            out[info["role"]] = out.get(info["role"], 0) + info["rss_kb"]
        return out

    @property
    def worker_roles(self):
        return self.roles.aggregate_names({info["role"] for info in self.processes.values()})

    def columns(self):
        """The stack-agnostic part of a sample row."""
        pss, rss, cpu = self.pss_by_role(), self.rss_by_role(), self.cpu_by_role()
        workers = self.worker_roles
        return {
            "stack": self.roles.name,
            "process_count": len(self.processes),
            "processes": json.dumps(list(self.processes.values())),
            "cpu_total_pct": self.cpu_total_percent if self.cpu_total_percent is not None else "",
            "cpu_by_role": json.dumps(cpu),
            "cpu_workers_pct": round(sum(cpu.get(name, 0.0) for name in workers), 1),
            "pss_total_kb": sum(pss.values()),
            "rss_total_kb": sum(rss.values()),
            "pss_by_role": json.dumps(pss),
            "pss_workers_kb": sum(pss.get(name, 0) for name in workers),
            "worker_count": len(workers),
            "worker_roles": "|".join(workers),
            "cg_current": self.cgroup.get("current", ""),
            "cg_peak": self.cgroup.get("peak", ""),
            "cg_max": self.cgroup.get("max", ""),
            "cg_events": self.cgroup.get("events", ""),
            "st_anon": self.memory_stat.get("anon", ""),
            "st_file": self.memory_stat.get("file", ""),
            "st_kernel": self.memory_stat.get("kernel", ""),
            "st_sock": self.memory_stat.get("sock", ""),
            "st_shmem": self.memory_stat.get("shmem", ""),
        }


COLUMNS = ["ts", "epoch", "run", "stack", "phase", "rate_offered", "scheduled", "done",
           "errors_http", "errors_app", "errors_transport", "reqs_per_s",
           "p50_ms", "p95_ms", "p99_ms", "late_p50_s", "late_max_s", "pending",
           "process_count", "processes", "cpu_total_pct", "cpu_by_role", "cpu_workers_pct",
           "pss_total_kb", "rss_total_kb", "pss_by_role", "pss_workers_kb",
           "worker_count", "worker_roles",
           "cg_current", "cg_peak", "cg_max", "cg_events",
           "st_anon", "st_file", "st_kernel", "st_sock", "st_shmem",
           "users_authenticated", "users_placed", "users_frozen", "users_unplaced",
           "users_guest", "connections", "pages", "users_per_worker"]
