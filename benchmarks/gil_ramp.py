"""Experiment 1 — 'who dies first' under thread load: in-process vs daemon register.

Isolated test of the GIL thesis: N worker threads hammer the register with get_item
for a fixed duration; we measure total register calls/sec achieved at each N.

- in-process: get_item runs in RAM under critical_section (consumes only worker GIL).
- daemon: get_item goes through GnrDaemonClient -> msgpack pack/unpack (worker GIL) +
  loopback TCP to the single-threaded asyncio daemon (which itself serializes).

Thesis: in-process throughput keeps rising with threads until it saturates one core's
GIL on ~0.6us calls; daemon plateaus much earlier and lower, because each call holds
~21x more worker GIL (msgpack) AND serializes on the single-thread daemon.

Run: python3 gil_ramp.py   (daemon must be live on :40405)
"""
import threading
import time
from unittest.mock import MagicMock

from genro_daemon.siteregister import GnrSiteRegister
from genro_daemon.client import GnrDaemonClient

DURATION = 3.0
THREADS = [1, 2, 4, 8, 12, 16]


def ramp(label, call_factory):
    print(f"\n=== {label} ===")
    print(f"{'threads':>7} {'calls/sec':>12} {'vs 1thr':>8}")
    base = None
    for n in THREADS:
        counts = [0] * n
        stop = threading.Event()

        def worker(idx, call):
            c = 0
            while not stop.is_set():
                call()
                c += 1
            counts[idx] = c

        calls = [call_factory() for _ in range(n)]
        threads = [threading.Thread(target=worker, args=(i, calls[i]))
                   for i in range(n)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        time.sleep(DURATION)
        stop.set()
        for t in threads:
            t.join()
        wall = time.perf_counter() - t0
        cps = sum(counts) / wall
        if base is None:
            base = cps
        print(f"{n:>7} {cps:>12,.0f} {cps/base:>7.2f}x")
    return base


# ---- IN-PROCESS ----
sr = GnrSiteRegister(MagicMock(), sitename="gil_inproc", thread_safe=True)
sr.setConfiguration()
sr.page_register.create("p1", connection_id="c1", user="u1")


def inproc_call():
    def call():
        with sr.critical_section():
            sr.get_item("p1", register_name="page")
    return call


# ---- DAEMON ----
# one client per thread (mirrors per-worker-thread reality; shared pool otherwise)
def daemon_call():
    cli = GnrDaemonClient("gnr://localhost:40405", sitename="gil_daemon_x")
    try:
        cli.addSiteRegister("gil_daemon_x")
    except Exception:
        pass

    def call():
        cli.get_item("p1", register_name="page")
    return call


ramp("IN-PROCESS (RAM)", inproc_call)
ramp("DAEMON (TCP+msgpack, single-thread server)", daemon_call)
