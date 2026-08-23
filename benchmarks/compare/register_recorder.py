"""Register recorder for the legacy/bridge comparison bench.

A wrapper OBJECT standing in place of `SiteRegisterClient`: it builds the real
client, holds it, and catches every attribute through its own `__getattr__`, so
the methods declared on the legacy class are recorded together with the names
that class's own `__getattr__` passes through. Installed by one assignment, and
the same assignment serves on the bridge:

    gnr.web.gnrwsgisite.SiteRegisterClient = RegisterRecorder

The assignment must happen BEFORE the site is built: `gnrserveprod.main()`
constructs `GnrWsgiSite` before it reads the gunicorn `-c` file, and
`GnrWsgiSite.__init__` forces the register into existence, so no gunicorn hook
is early enough. `serve_legacy.py` is that install point.

One JSONL line per call in `temp/register_trace.jsonl`, joined to the HTTP trace
by the `exchange_id` the HTTP recorder injects as the `X-Bench-Exchange-Id`
request header and this recorder reads back through `site.currentRequest`. The
calls the master makes while building the site happen before any exchange
exists: the `exchange_id` key is then ABSENT from the record — never faked and
never inherited from whatever ran last on that thread.

What a line carries: the verb, the surface it was intercepted on (`client` for a
method declared on the legacy class, `passthrough` for a name its `__getattr__`
forwards, `store` for a call on a `ServerStore`), the arguments and the answer,
the wire attempts and the error class, the ordinal within its
exchange, the duration, thread and pid. Store lines carry as well the
`register_name` and `register_item_id` of the store the call happened on.

`wire_calls` counts the round trips one call cost, and it is counted on the Pyro
proxy rather than guessed: the legacy retry loop lives inside the closure
`SiteRegisterClient.__getattr__` builds, and its `except Exception` neither logs
nor re-raises, so from outside that funnel a fourfold failure is
indistinguishable from a legitimate `None`. Counting on the wire and attributing
to the call in flight keeps ONE line per call the site made — never one per round
trip.

The field is NOT called `attempts`, and the name was changed after it misled its
first reader: a store's Bag read shows `wire_calls: 2` because
`ServerStore.data` evaluates `self.register_item` twice, each evaluation a round
trip of its own — measured, not a retry. A retry shows as more round trips than
the call's shape costs, together with a `wire_error`.

Only non-routine attributes are handed back untouched, and the guard is
`inspect.isroutine`, not `callable`: `register.locked_exception` is a class, so
it is callable, and a wrapped class stops matching the `except` clause that uses
it — silently, in a path that only runs once something has already gone wrong.

The trace is opened per write: the wrapper is born in the master process, and a
handle inherited across the fork would let two processes interleave mid-line.

Values are written so that two runs can be compared: a Bag goes in as its XML,
because the default `repr` carries no content and a memory address that changes
at every run would read as a divergence; anything else goes in as its `repr`
with the address removed. Long values are truncated with their real length.

A failure inside the recorder is written as `recorder_error` and never reaches
the site.
"""

import functools
import inspect
import json
import os
import re
import threading
import time
from datetime import datetime

from gnr.core.gnrbag import Bag
from gnr.web.daemon.siteregister_client import ServerStore, SiteRegisterClient

EXCHANGE_HEADER = "X-Bench-Exchange-Id"

TRACE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "temp", "register_trace.jsonl")

VALUE_LENGTH_LIMIT = 2000

# `<Bag object at 0x10fcb0ce0>` says nothing about the answer and changes at
# every run: the address alone would read as a divergence. Bags go in as their
# XML, and any other address is dropped from the repr.
OBJECT_ADDRESS = re.compile(r" at 0x[0-9a-f]+")

# The store's register reads are properties, so they cannot be intercepted as
# calls: reading the attribute IS the call. They are recorded by name.
STORE_READ_PROPERTIES = ("data", "register_item", "datachanges", "subscribed_paths")


class TraceWriter:
    """Appends one JSONL line per recorded call, opening the file per write."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def append_record(self, record):
        line = json.dumps(record, ensure_ascii=False, default=repr)
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as trace:
                trace.write(line + "\n")


class WireCounter:
    """Stands in place of the Pyro proxy, counting attempts and wire errors.

    The legacy retry loop calls the proxy up to `MAX_RETRY_ATTEMPTS` times and
    swallows every exception. Counting here is what makes `attempts` and the
    error class true; no line of its own is written, so one call by the site
    stays one line in the trace.
    """

    def __init__(self, proxy, recorder):
        self.proxy = proxy
        self.recorder = recorder

    def __getattr__(self, name):
        attribute = getattr(self.proxy, name)
        if not callable(attribute):
            return attribute
        return self.get_counted_call(attribute)

    def get_counted_call(self, attribute):
        def counted(*args, **kwargs):
            self.recorder.record_wire_call()
            try:
                return attribute(*args, **kwargs)
            except Exception as exc:
                self.recorder.record_wire_error(exc)
                raise
        return counted


class StoreRecorder:
    """Stands in place of one `ServerStore`, recording the calls made on it.

    A store keeps the client it was built from, so an unwrapped store takes its
    whole conversation outside the recorder. The delegated call the store then
    makes on the real client is NOT recorded a second time: a line is a call the
    SITE made.
    """

    def __init__(self, store, recorder):
        self.store = store
        self.recorder = recorder

    @property
    def store_identity(self):
        return {"register_name": self.store.register_name,
                "register_item_id": self.store.register_item_id}

    def __enter__(self):
        self.recorder.perform_recorded_call(self.store.__enter__, "__enter__",
                                   "store", self.store_identity, (), {})
        return self

    def __exit__(self, exc_type, exc_value, tb):
        return self.recorder.perform_recorded_call(self.store.__exit__, "__exit__",
                                          "store", self.store_identity,
                                          (exc_type, exc_value, tb), {})

    def __getattr__(self, name):
        if name in STORE_READ_PROPERTIES:
            read = functools.partial(getattr, self.store, name)
            return self.recorder.perform_recorded_call(read, name, "store",
                                              self.store_identity, (), {})
        attribute = getattr(self.store, name)
        if not inspect.isroutine(attribute):
            return attribute
        return self.recorder.get_recorded_call(attribute, name, "store",
                                       self.store_identity)


class RegisterRecorder:
    """Stands in place of `SiteRegisterClient`, recording every call on it."""

    def __init__(self, site, trace_path=TRACE_PATH):
        self.client = SiteRegisterClient(site)
        self.trace = TraceWriter(trace_path)
        self.wire_count = threading.local()
        self.ordinals = {}
        self.ordinals_lock = threading.Lock()
        self.client.siteregister = WireCounter(self.client.siteregister, self)

    def __getattr__(self, name):
        attribute = getattr(self.client, name)
        if not inspect.isroutine(attribute):
            return attribute
        surface = "client" if hasattr(type(self.client), name) else "passthrough"
        return self.get_recorded_call(attribute, name, surface, {})

    def get_recorded_call(self, target, verb, surface, fields):
        def recorded(*args, **kwargs):
            return self.perform_recorded_call(target, verb, surface, fields,
                                     args, kwargs)
        return recorded

    def perform_recorded_call(self, target, verb, surface, fields, args, kwargs):
        previous = getattr(self.wire_count, "current", None)
        self.wire_count.current = {"wire_calls": 0, "wire_error": None}
        started = time.time()
        try:
            answer = target(*args, **kwargs)
        except Exception as exc:
            elapsed = time.time() - started
            self.write_record(verb, surface, fields, args, kwargs, None,
                              elapsed, exc, self.take_wire_count(previous))
            raise
        elapsed = time.time() - started
        self.write_record(verb, surface, fields, args, kwargs, answer,
                          elapsed, None, self.take_wire_count(previous))
        return self.get_recorded_answer(answer)

    def take_wire_count(self, previous):
        """The wire counters of the call just ended; the thread goes back."""
        state = self.wire_count.current
        self.wire_count.current = previous
        return state

    def get_recorded_answer(self, answer):
        if isinstance(answer, ServerStore):
            return StoreRecorder(answer, self)
        return answer

    def record_wire_call(self):
        state = getattr(self.wire_count, "current", None)
        if state is not None:
            state["wire_calls"] += 1

    def record_wire_error(self, exc):
        state = getattr(self.wire_count, "current", None)
        if state is not None:
            state["wire_error"] = f"{type(exc).__name__}: {exc}"

    @property
    def current_exchange_id(self):
        request = self.client.site.currentRequest
        if request is None:
            return None
        return request.headers.get(EXCHANGE_HEADER)

    def assign_ordinal(self, exchange_id):
        with self.ordinals_lock:
            ordinal = self.ordinals.get(exchange_id, 0) + 1
            self.ordinals[exchange_id] = ordinal
            return ordinal

    def get_comparable_value(self, value):
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, Bag):
            text = value.toXml()
        elif isinstance(value, str):
            text = value
        else:
            text = OBJECT_ADDRESS.sub("", repr(value))
        if len(text) <= VALUE_LENGTH_LIMIT:
            return text
        return f"{text[:VALUE_LENGTH_LIMIT]}...<{len(text)} chars>"

    def write_record(self, verb, surface, fields, args, kwargs, answer,
                     elapsed, exc, state):
        try:
            record = {"ts": datetime.now().isoformat(),
                      "pid": os.getpid(),
                      "thread": threading.get_ident(),
                      "surface": surface,
                      "verb": verb,
                      "args": [self.get_comparable_value(arg) for arg in args],
                      "kwargs": {key: self.get_comparable_value(value)
                                 for key, value in kwargs.items()},
                      "result": self.get_comparable_value(answer),
                      "wire_calls": state["wire_calls"],
                      "wire_error": state["wire_error"],
                      "error": f"{type(exc).__name__}: {exc}" if exc else None,
                      "duration_ms": round(elapsed * 1000, 3)}
            exchange_id = self.current_exchange_id
            if exchange_id is not None:
                record["exchange_id"] = exchange_id
            record["ordinal"] = self.assign_ordinal(exchange_id)
            record.update(fields)
            self.trace.append_record(record)
        except Exception as failure:
            self.append_error(verb, failure)

    def append_error(self, verb, exc):
        try:
            self.trace.append_record({"ts": datetime.now().isoformat(),
                               "pid": os.getpid(),
                               "thread": threading.get_ident(),
                               "verb": verb,
                               "recorder_error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
