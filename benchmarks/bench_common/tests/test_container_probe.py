"""Direct checks of the legacy role map, with no laboratory and no Docker.

A fabricated process table stands in for the one ``docker exec`` reads inside the
container: the text is written in the exact format of ``READ_SCRIPT``, so the
code under test is the real ``parse`` — the role map, the naming, and the
per-role aggregates of ``RoleSample``.

The central thesis: THE MASTER IS THE GUNICORN PROCESS WITH NO GUNICORN PARENT.
Two topologies produce the same shape and both must classify:

- the lab's entrypoint ``exec``s Gunicorn, so the master IS pid 1 and its four
  workers carry ``ppid=1``;
- behind an init or a shell, pid 1 is a stranger and the master is its child.

Parentage towards init cannot tell them apart: in the first topology it names the
four workers and leaves the master unclassified. That is the defect measured on
the L120 run of 2026-08-31, where ``worker_count`` read 0 with four workers alive.

    python3 test_container_probe.py
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                os.pardir, os.pardir)))

from bench_common.container_probe import (                                       # noqa: E402
    BridgeRoles, ContainerProbe, LegacyRoles, RoleSample)

failures = []

GUNICORN_CMD = ("python -m gnr.web.cli.gnrserveprod legacy_lab -b 0.0.0.0:8099 "
                "-w 4 -k gthread --threads 16")
DAEMON_CMD = "/usr/local/bin/python /usr/local/bin/gnrdaemon legacy_lab"
SERVICE_CMD = ("python -c from gnr.web.gnrwsgisite import GnrWsgiSite; "
               "site = GnrWsgiSite('legacy_lab'); print(repr(site.getPreference(")
FOREIGN_CMD = "postgres: logical replication launcher"
INIT_CMD = "/sbin/docker-init -- /lab/entrypoints/legacy.sh"


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got!r}"
          + ("" if ok else f"  atteso {want!r}"))
    if not ok:
        failures.append(label)


def process_line(pid, ppid, cmd, rss=100000, pss=50000, ticks=1000):
    """One row in the exact shape READ_SCRIPT emits."""
    return f"P|{pid}|{ppid}|{rss}|{pss},10,20,30,40|{ticks}|{cmd}"


def table(*lines):
    """A whole read: the processes, then the cgroup gauges READ_SCRIPT appends."""
    return "\n".join(list(lines) + [
        "C|402653184|427819008|2147483648",
        "E|low 0;high 0;max 0;oom 0;oom_kill 0;oom_group_kill 0;",
        "S|anon|300000000", "S|file|80000000", "S|kernel|9000000",
        "S|sock|100000", "S|shmem|0", "U|123456789"])


def roles_of(processes):
    return {pid: info["role"] for pid, info in processes.items()}


def count_role(processes, role):
    return sum(1 for info in processes.values() if info["role"] == role)


# The topology measured on Hetzner: the entrypoint exec'd Gunicorn, so the master
# is pid 1 with ppid 0, and the four workers hang off pid 1 beside the daemon.
EXEC_TOPOLOGY = table(
    process_line("1", "0", GUNICORN_CMD, ticks=500),
    process_line("7", "1", DAEMON_CMD, ticks=6000),
    process_line("11", "1", GUNICORN_CMD, ticks=8100),
    process_line("12", "1", GUNICORN_CMD, ticks=8200),
    process_line("13", "1", GUNICORN_CMD, ticks=8300),
    process_line("14", "1", GUNICORN_CMD, ticks=8400),
    process_line("7886", "0", SERVICE_CMD, ticks=300),
    process_line("9001", "999", FOREIGN_CMD, ticks=10))

# The other topology: pid 1 is an init, the master is its child, the workers are
# the master's children. Pid numbering is deliberately out of order.
INIT_TOPOLOGY = table(
    process_line("1", "0", INIT_CMD, ticks=5),
    process_line("9", "1", DAEMON_CMD, ticks=6000),
    process_line("15", "1", GUNICORN_CMD, ticks=500),
    process_line("31", "15", GUNICORN_CMD, ticks=8100),
    process_line("22", "15", GUNICORN_CMD, ticks=8200),
    process_line("40", "15", GUNICORN_CMD, ticks=8300),
    process_line("28", "15", GUNICORN_CMD, ticks=8400),
    process_line("7886", "1", SERVICE_CMD, ticks=300),
    process_line("9001", "999", FOREIGN_CMD, ticks=10))


print("== topologia exec: il master E' il pid 1 ==")
probe = ContainerProbe("nessun-container", LegacyRoles())
processes, cgroup, stat, cpu_usec = probe.parse(EXEC_TOPOLOGY)

check("il pid 1 e' il master", processes["1"]["role"], "gunicorn_master")
check("un solo master", count_role(processes, "gunicorn_master"), 1)
check("quattro worker", sum(1 for info in processes.values()
                            if info["role"].startswith("gunicorn_worker_")), 4)
check("i quattro figli sono numerati in ordine di pid",
      [processes[pid]["role"] for pid in ("11", "12", "13", "14")],
      ["gunicorn_worker_01", "gunicorn_worker_02",
       "gunicorn_worker_03", "gunicorn_worker_04"])
check("il daemon resta distinto", processes["7"]["role"], "daemon")
check("il processo di servizio non e' gunicorn", processes["7886"]["role"], "other")
check("il processo estraneo resta other", processes["9001"]["role"], "other")
check("nessun processo gunicorn finisce in other",
      [pid for pid, info in processes.items()
       if "gnrserveprod" in info["cmd"] and info["role"] == "other"], [])
check("il master non e' anche un worker",
      processes["1"]["role"].startswith("gunicorn_worker_"), False)
check("nessun ruolo assegnato due volte",
      len(roles_of(processes).values()) == len(set(roles_of(processes).values())), False)

named = [info["role"] for info in processes.values() if info["role"] != "other"]
check("i ruoli non-other sono tutti diversi", len(named) == len(set(named)), True)
check("il figlio del daemon non entra fra i worker",
      "7" in [pid for pid, info in processes.items()
              if info["role"].startswith("gunicorn_worker_")], False)


print("\n== topologia init: il master e' figlio dell'init ==")
processes_init, _, _, _ = ContainerProbe("nessun-container", LegacyRoles()).parse(INIT_TOPOLOGY)

check("il pid 1 non e' gunicorn e resta other", processes_init["1"]["role"], "other")
check("il master e' il figlio gunicorn dell'init", processes_init["15"]["role"],
      "gunicorn_master")
check("un solo master", count_role(processes_init, "gunicorn_master"), 1)
check("quattro worker", sum(1 for info in processes_init.values()
                           if info["role"].startswith("gunicorn_worker_")), 4)
check("numerati in ordine di pid, non di lettura",
      [processes_init[pid]["role"] for pid in ("22", "28", "31", "40")],
      ["gunicorn_worker_01", "gunicorn_worker_02",
       "gunicorn_worker_03", "gunicorn_worker_04"])
check("il daemon resta distinto", processes_init["9"]["role"], "daemon")
check("il processo di servizio figlio dell'init resta other",
      processes_init["7886"]["role"], "other")
check("il processo estraneo resta other", processes_init["9001"]["role"], "other")
check("nessun processo gunicorn in other",
      [pid for pid, info in processes_init.items()
       if "gnrserveprod" in info["cmd"] and info["role"] == "other"], [])
named_init = [info["role"] for info in processes_init.values() if info["role"] != "other"]
check("i ruoli non-other sono tutti diversi", len(named_init) == len(set(named_init)), True)


print("\n== la certificazione dichiara la forma attesa ==")
check("la forma exec e' certificata senza problemi, tolti i due estranei",
      ContainerProbe("nessun-container", LegacyRoles()).certify(
          {pid: info for pid, info in processes.items()
           if info["role"] != "other"}, 4), [])
check("la forma init e' certificata senza problemi, tolti i tre estranei",
      ContainerProbe("nessun-container", LegacyRoles()).certify(
          {pid: info for pid, info in processes_init.items()
           if info["role"] != "other"}, 4), [])
check("una forma con tre worker viene respinta",
      ContainerProbe("nessun-container", LegacyRoles()).certify(
          {pid: info for pid, info in processes.items()
           if info["role"] != "other" and pid != "14"}, 4),
      ["worker: 3 invece di 4 (['gunicorn_worker_01', 'gunicorn_worker_02', "
       "'gunicorn_worker_03'])"])


print("\n== gli aggregati sommano i quattro worker e non il master ==")
# Un secondo istante: solo i tick cambiano, di quantita' diverse per processo.
previous = {pid: info["ticks"] for pid, info in processes.items()}
later = table(
    process_line("1", "0", GUNICORN_CMD, pss=60000, ticks=500 + 100),
    process_line("7", "1", DAEMON_CMD, pss=74000, ticks=6000 + 600),
    process_line("11", "1", GUNICORN_CMD, pss=81000, ticks=8100 + 800),
    process_line("12", "1", GUNICORN_CMD, pss=82000, ticks=8200 + 850),
    process_line("13", "1", GUNICORN_CMD, pss=83000, ticks=8300 + 900),
    process_line("14", "1", GUNICORN_CMD, pss=84000, ticks=8400 + 950),
    process_line("7886", "0", SERVICE_CMD, pss=48000, ticks=300 + 5),
    process_line("9001", "999", FOREIGN_CMD, pss=1000, ticks=10 + 1))
roles = LegacyRoles()
second, cgroup2, stat2, cpu2 = ContainerProbe("nessun-container", roles).parse(later)
sample = RoleSample(second, cgroup2, stat2, cpu2, roles,
                    previous_ticks=previous, previous_cpu_usec=123456789 - 40000000,
                    elapsed=10.0)

cpu = sample.cpu_by_role()
pss = sample.pss_by_role()
columns = sample.columns()

# 100 tick sono un secondo di CPU: in 10 s di intervallo fanno il 10% di un core.
# I quattro worker crescono di 800, 850, 900 e 950 tick.
check("la CPU dei quattro worker e' la somma dei quattro processi",
      columns["cpu_workers_pct"], round(sum([80.0, 85.0, 90.0, 95.0]), 1))
check("il master ha la sua CPU, separata", cpu["gunicorn_master"], 10.0)
check("la CPU del master non entra negli aggregati worker",
      columns["cpu_workers_pct"] == round(columns["cpu_workers_pct"]
                                          + cpu["gunicorn_master"], 1), False)
check("il PSS dei quattro worker e' la somma dei quattro processi",
      columns["pss_workers_kb"], 81000 + 82000 + 83000 + 84000)
check("il PSS del master resta fuori", pss["gunicorn_master"], 60000)
check("il conteggio dei worker e' quattro", columns["worker_count"], 4)
check("i nomi dei worker sono in colonna", columns["worker_roles"],
      "gunicorn_worker_01|gunicorn_worker_02|gunicorn_worker_03|gunicorn_worker_04")
check("il PSS totale somma tutti gli otto processi", columns["pss_total_kb"],
      60000 + 74000 + 81000 + 82000 + 83000 + 84000 + 48000 + 1000)
check("il daemon non entra fra i worker", pss.get("daemon"), 74000)
check("il formato del CSV non cambia: le colonne sono quelle di prima",
      sorted(columns), sorted(["stack", "process_count", "processes", "cpu_total_pct",
                               "cpu_by_role", "cpu_workers_pct", "pss_total_kb",
                               "rss_total_kb", "pss_by_role", "pss_workers_kb",
                               "worker_count", "worker_roles", "cg_current", "cg_peak",
                               "cg_max", "cg_events", "st_anon", "st_file", "st_kernel",
                               "st_sock", "st_shmem"]))


print("\n== il caso senza gunicorn non inventa un master ==")
only_daemon, _, _, _ = ContainerProbe("nessun-container", LegacyRoles()).parse(
    table(process_line("1", "0", INIT_CMD),
          process_line("7", "1", DAEMON_CMD),
          process_line("9001", "999", FOREIGN_CMD)))
check("nessun master", count_role(only_daemon, "gunicorn_master"), 0)
check("nessun worker", sum(1 for info in only_daemon.values()
                           if info["role"].startswith("gunicorn_worker_")), 0)
check("il daemon resta riconosciuto", only_daemon["7"]["role"], "daemon")


print("\n== la classificazione bridge non cambia ==")
bridge = BridgeRoles(worker_pids={"31": "pool_0001", "32": "pool_0002"})
bridge_processes, _, _, _ = ContainerProbe("nessun-container", bridge).parse(table(
    process_line("1", "0", "python -m gnr.web.cli.gnrasgiserve bridge_lab"),
    process_line("14", "1", "python -m genro_asgi.spa.template_entry"),
    process_line("31", "14", "python -m genro_asgi.spa.template_entry"),
    process_line("32", "14", "python -m genro_asgi.spa.template_entry"),
    process_line("9001", "999", FOREIGN_CMD)))
check("il commander e' il processo gnrasgiserve", bridge_processes["1"]["role"], "commander")
check("il template e' il figlio dell'init", bridge_processes["14"]["role"], "template")
check("i worker del pool prendono il nome dal census",
      [bridge_processes["31"]["role"], bridge_processes["32"]["role"]],
      ["pool_0001", "pool_0002"])
check("l'estraneo resta other", bridge_processes["9001"]["role"], "other")


print("\n" + "=" * 50)
if failures:
    print(f"FALLITI {len(failures)}:")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("tutti i controlli passati")
