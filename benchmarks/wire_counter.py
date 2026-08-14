"""Count process->daemon wire calls per RPC lifecycle (DAEMON mode).

Hooks GnrDaemonClient._invoke_method — the single point where the worker
serializes a call and sends it to the daemon over TCP. Every register touch,
whatever path generated it, crosses here. Flushes the per-rpc tally at each
_rpcDispatcher start. Output: /tmp/wire_counter.log
  RPC <method> wire=<N> breakdown={get_item:3, set_datachange:2, ...}
"""

import threading
import json
import time

LOG = "/tmp/wire_counter.log"
_tl = threading.local()


def _state():
    if not hasattr(_tl, "count"):
        _tl.count = 0
        _tl.by = {}
        _tl.current = None
    return _tl


def _flush(nxt):
    s = _state()
    if s.current is not None:
        with open(LOG, "a") as f:
            f.write(f"RPC {s.current} wire={s.count} "
                    f"breakdown={json.dumps(s.by, sort_keys=True)}\n")
    s.count = 0
    s.by = {}
    s.current = nxt


def install():
    from genro_daemon.client import GnrDaemonClient
    orig = GnrDaemonClient._invoke_method

    def counting(self, method, *args, **kw):
        s = _state()
        s.count += 1
        s.by[method] = s.by.get(method, 0) + 1
        return orig(self, method, *args, **kw)

    GnrDaemonClient._invoke_method = counting

    from gnr.web.gnrwebpage import GnrWebPage
    od = GnrWebPage._rpcDispatcher

    def disp(self, *a, **kw):
        _flush(kw.get("method"))
        return od(self, *a, **kw)

    GnrWebPage._rpcDispatcher = disp

    with open(LOG, "a") as f:
        f.write(f"# wire_counter installed at {time.time()}\n")


install()
