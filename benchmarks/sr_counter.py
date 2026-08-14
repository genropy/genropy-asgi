"""Count register invocations per RPC lifecycle — no source changes.

Loaded into the gunicorn worker via PYTHONPATH + import (sitecustomize-style).
Monkey-patches:
  - SiteRegisterClient._sr_call / InProcessSiteRegisterClient._sr_call
    -> increment a thread-local counter, tallying by method_name
  - GnrWebPage._rpcDispatcher
    -> at the START of each pageCall, flush the previous count to a log line,
       then reset, so each line = register calls consumed by ONE rpc.

Output: one line per rpc to /tmp/sr_counter.log
  RPC <method> sr_calls=<N> breakdown={getItem:3, setInClientData:2, ...}
"""

import threading
import json
import time

LOG = "/tmp/sr_counter.log"
_tl = threading.local()


def _state():
    if not hasattr(_tl, "count"):
        _tl.count = 0
        _tl.by = {}
        _tl.current_rpc = None
    return _tl


def _flush(next_rpc):
    s = _state()
    if s.current_rpc is not None:
        with open(LOG, "a") as f:
            f.write(f"RPC {s.current_rpc} sr_calls={s.count} "
                    f"breakdown={json.dumps(s.by, sort_keys=True)}\n")
    s.count = 0
    s.by = {}
    s.current_rpc = next_rpc


def _tick(label):
    s = _state()
    s.count += 1
    s.by[label] = s.by.get(label, 0) + 1


def install():
    from genro_daemon.siteregister_client import SiteRegisterClient, ServerStore
    try:
        from genro_daemon.siteregister_client import InProcessSiteRegisterClient
    except Exception:
        InProcessSiteRegisterClient = None

    # 1) catch-all method calls via _sr_call
    def make_counting(orig):
        def counting(self, method_name, *args, **kwargs):
            _tick("sr:" + method_name)
            return orig(self, method_name, *args, **kwargs)
        return counting

    SiteRegisterClient._sr_call = make_counting(SiteRegisterClient._sr_call)
    if InProcessSiteRegisterClient is not None:
        InProcessSiteRegisterClient._sr_call = make_counting(
            InProcessSiteRegisterClient._sr_call)

    # 2) ServerStore operations (the real per-rpc serverstore traffic):
    #    getItem/setItem/dropItem/set_datachange/subscribe_path/__enter__/__exit__
    for meth in ("set_datachange", "drop_datachanges", "subscribe_path",
                 "reset_datachanges", "__enter__", "__exit__"):
        if hasattr(ServerStore, meth):
            orig = getattr(ServerStore, meth)

            def make(orig, meth):
                def wrapped(self, *a, **kw):
                    _tick("store:" + meth)
                    return orig(self, *a, **kw)
                return wrapped
            setattr(ServerStore, meth, make(orig, meth))

    # ServerStore proxies Bag ops (getItem/setItem/...) via __getattr__ -> data.<op>.
    # Wrap __getattr__ to count those Bag-delegated calls too.
    orig_ga = ServerStore.__getattr__

    def counting_ga(self, fname):
        res = orig_ga(self, fname)
        if callable(res):
            def counted(*a, **kw):
                _tick("store.bag:" + fname)
                return res(*a, **kw)
            return counted
        return res
    ServerStore.__getattr__ = counting_ga

    # 3) rpc lifecycle start -> flush previous count
    from gnr.web.gnrwebpage import GnrWebPage
    orig_disp = GnrWebPage._rpcDispatcher

    def disp(self, *a, **kw):
        _flush(kw.get("method"))
        return orig_disp(self, *a, **kw)

    GnrWebPage._rpcDispatcher = disp

    with open(LOG, "a") as f:
        f.write(f"# sr_counter installed at {time.time()} "
                f"(inproc={'yes' if InProcessSiteRegisterClient else 'no'})\n")


install()
