"""Orchestrate the threads x users grid for one register mode.

For each thread level: (re)start gunicorn (1 worker, gthread, --threads T),
wait until ready, warm up, then sweep the user counts running replay_a1.py
in --duration mode. Emit a compact GRID line per cell and a summary table.

The register MODE (inprocess|daemon) is whatever the site siteconfig declares;
this script does NOT switch it — run it once per mode after editing siteconfig
and let the caller restart. Usage:
  python3 run_grid.py --mode daemon
"""

import argparse
import re
import subprocess
import time
import urllib.request

SITE = "test_invoice_pg"
SITE_DIR = ("/Users/gporcari/Sviluppo/Genropy/genropy/projects/"
            "test_invoice/sites/test_invoice_pg")
BASE = "http://127.0.0.1:8099"
THREADS = [2, 4, 8, 16]
USERS = [4, 8, 16, 32]
DURATION = 10
WARMUP = 2.0
REPLAY = "/Users/gporcari/Sviluppo/genro_ng/meta-genro-modules/sub-projects/genropy-asgi/temp/benchmark/assets/replay_a1.py"


def port_pid():
    out = subprocess.run(["lsof", "-nP", "-iTCP:8099", "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True).stdout.split()
    return out[0] if out else None


def stop_server():
    pid = port_pid()
    if pid:
        subprocess.run(["kill", pid])
        for _ in range(20):
            if not port_pid():
                return
            time.sleep(0.3)


def start_server(threads):
    proc = subprocess.Popen(
        ["gnr", "web", "serveprod", SITE, "-b", "127.0.0.1:8099",
         "-w", "1", "-k", "gthread", "--threads", str(threads)],
        cwd=SITE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        try:
            urllib.request.urlopen(BASE + "/", timeout=2).read()
            return proc
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("server did not become ready")


def run_cell(users):
    out = subprocess.run(
        ["python3", REPLAY, "--base", BASE, "--users", str(users),
         "--duration", str(DURATION)],
        capture_output=True, text=True).stdout
    m = re.search(r"GRID .*", out)
    return m.group(0) if m else "GRID (no line) " + out[-200:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, help="label only: inprocess|daemon")
    args = ap.parse_args()

    rows = []
    for t in THREADS:
        stop_server()
        start_server(t)
        # warmup
        time.sleep(WARMUP)
        run_cell(USERS[0])  # discarded warmup cell
        for u in USERS:
            line = run_cell(u)
            mm = dict(re.findall(r"(\w+)=([\d.]+)", line))
            rps = mm.get("rps", "?")
            p90 = mm.get("p90", "?")
            non200 = mm.get("non200", "?")
            errs = mm.get("errs", "?")
            print(f"[{args.mode}] threads={t:>2} users={u:>2}  "
                  f"rps={rps:>7}  p90={p90:>5}ms  non200={non200} errs={errs}")
            rows.append((t, u, rps, p90, non200, errs))
    stop_server()

    print(f"\n=== GRID SUMMARY ({args.mode}) rps ===")
    print("threads\\users " + "  ".join(f"{u:>8}" for u in USERS))
    for t in THREADS:
        cells = {(tt, uu): rps for tt, uu, rps, *_ in rows}
        print(f"{t:>10}    " + "  ".join(
            f"{cells.get((t, u), '?'):>8}" for u in USERS))


if __name__ == "__main__":
    main()
