"""Gunicorn config: instrument the in-process register's per-site critical_section.

post_fork wraps InProcessSiteRegisterClient._sr_call to count, per worker:
  - n_calls          : total register calls
  - t_wait_lock      : seconds spent waiting to ACQUIRE the per-site lock (contention)
  - t_hold_lock      : seconds spent holding it (work under the lock)
A background thread logs the deltas every 2s to /tmp/critsec_probe.log so we can
see, under load, how much time the register lock serialises across threads.

Used only for diagnosis (Esperimento 2). Not committed.
"""
import threading
import time

bind = "127.0.0.1:8099"
workers = 1
worker_class = "gthread"
threads = 8

_PROBE = {"n": 0, "wait": 0.0, "hold": 0.0, "t_req": 0.0, "n_req": 0}
_lock = threading.Lock()


def post_fork(server, worker):
    from genro_daemon import siteregister_client as src

    def probed(self, method_name, *args, **kwargs):
        cs = self.siteregister.critical_section()
        t0 = time.time()
        cs.__enter__()
        t1 = time.time()
        try:
            return getattr(self.siteregister, method_name)(*args, **kwargs)
        finally:
            cs.__exit__(None, None, None)
            t2 = time.time()
            with _lock:
                _PROBE["n"] += 1
                _PROBE["wait"] += (t1 - t0)
                _PROBE["hold"] += (t2 - t1)

    src.InProcessSiteRegisterClient._sr_call = probed

    def reporter():
        last = dict(_PROBE)
        while True:
            time.sleep(2)
            with _lock:
                cur = dict(_PROBE)
            dn = cur["n"] - last["n"]
            if dn:
                dw = (cur["wait"] - last["wait"]) * 1000
                dh = (cur["hold"] - last["hold"]) * 1000
                with open("/tmp/critsec_probe.log", "a") as f:
                    f.write(f"2s: calls={dn} wait_lock={dw:.0f}ms "
                            f"hold_lock={dh:.0f}ms "
                            f"wait/call={dw/dn:.3f}ms hold/call={dh/dn:.3f}ms\n")
            last = cur

    th = threading.Thread(target=reporter, daemon=True)
    th.start()
