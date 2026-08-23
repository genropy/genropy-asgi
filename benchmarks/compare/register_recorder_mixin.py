"""Register recorder for the BRIDGE side of the comparison bench.

The legacy client hands most of its surface to a generic ``__getattr__``, so
there a wrapper OBJECT standing in front of it catches everything by
construction. The bridge's ``GenropyRegisterClient`` is the opposite: every
command it serves is an explicit method with its own body and there is no
funnel at all. So here the recorder is a MIXIN — the recording client is a real
SUBCLASS of the bridge's client, and each recorded verb is an override that
records the call and then delegates to the parent's own implementation.

Installed by the same assignment the legacy uses, from the recording worker:

    gnr.web.gnrwsgisite.SiteRegisterClient = RecordingRegisterClient

The record shape is the legacy one, unchanged: the machinery that builds a line
is inherited from ``register_recorder.RegisterRecorder`` rather than rewritten,
so the two stacks cannot drift in what a line carries. What differs is only how
the calls are caught, and the comparison reads lines, never mechanisms.

The price of a mixin is ``RECORDED_VERBS``: a list of names that can silently
fall behind the client it shadows, which the legacy wrapper never could.
``bridge_coverage_check.py`` is the tripwire — it compares this tuple against
the client's live surface and fails when they diverge.

**Only the outermost call is recorded.** A line is a call the SITE made, which
on legacy came for free: the wrapper is not in the client's internal call path,
so a command calling another command produced one line. A subclass IS in that
path, and the bridge's client calls six of its own public commands internally
(``get_item``, ``local_item``, ``pages``, ``set_datachange``,
``set_serverstore_changes``, ``subscribe_path``), while every ``ServerStore``
delegates its whole conversation back to the client that made it. Recorded
naively, the bridge's trace would carry lines the legacy one cannot have, and
macro-phase 2 would read a divergence produced by the instrument. So a call
that starts while another is already being recorded runs untouched.

**Two fields tell the truth about this stack instead of imitating the other.**
``surface`` is always ``client``: there is no ``__getattr__`` to pass through,
so the ``passthrough`` lines of the legacy trace have no counterpart here.
``wire_calls`` is always 1: the register lives inside the worker's own process
and no call costs a round trip. Both are real differences between the stacks,
not artefacts of measurement.
"""

import functools
import os
import threading

from gnr.web.daemon.siteregister_client import SiteRegisterClient
from register_recorder import RegisterRecorder
from run_archive import RUN_ENV, RunArchive

# Every public command the bridge's client declares, as of the client this
# bench was written against. Not a selection: the coverage check fails the
# moment the client's surface and this tuple stop being the same set.
RECORDED_VERBS = (
    "allowedUsers", "change_connection_user", "claim_cleanup", "connection",
    "connectionStore", "connections", "drop_connection", "drop_datachanges",
    "drop_page", "dump", "exists", "expire_connection", "expire_pages",
    "filter_subscribed_tables", "get_dbenv", "get_item", "globalStore",
    "handle_ping", "isInMaintenance", "load", "local_item", "lock_item",
    "new_connection", "new_page", "notifyDbEvents", "on_reloader_restart",
    "on_site_stop", "page", "pageStore", "pages", "pendingProcessCommands",
    "refresh", "reset_datachanges", "sendProcessCommand", "setInClientData",
    "setMaintenance", "setPendingContext", "setStoreSubscription", "set_datachange",
    "set_serverstore_changes", "subscribeTable", "subscribe_path",
    "subscribed_tables", "subscription_storechanges", "unlock_item",
    "updatePageProfilers", "user", "userStore", "users",
)


class BridgeCallRecorder(RegisterRecorder):  # wf:phase-5:new
    """The legacy recorder's machinery, on a client that is subclassed, not wrapped.

    It inherits everything that builds a line — the comparable values, the
    ordinal within the exchange, the exchange read from the request header, the
    store wrapping, the failure that never reaches the site — and replaces only
    what a wrapper gave for free: the re-entrancy guard, and the wire count that
    has no wire to count.

    It is built with the recording client itself, so ``self.client`` means the
    same thing it means on legacy: the object the site talks to.
    """

    def __init__(self, client, archive=None):  # wf:phase-5:new
        self.client = client
        self.archive = archive or RunArchive(os.environ[RUN_ENV])
        self.wire_count = threading.local()
        self.ordinals = {}
        self.ordinals_lock = threading.Lock()
        self.outermost = threading.local()

    def perform_recorded_call(self, target, verb, surface, fields, args, kwargs):
        """Record the call the site made; run the client's own inner calls untouched."""
        if getattr(self.outermost, "taken", False):
            return target(*args, **kwargs)
        self.outermost.taken = True
        try:
            return super().perform_recorded_call(target, verb, surface, fields,
                                                 args, kwargs)
        finally:
            self.outermost.taken = False

    def take_wire_count(self, previous):
        """One in-process call, always: this register is not on the other side of a wire."""
        state = super().take_wire_count(previous)
        state["wire_calls"] = 1
        return state


class RecordedVerb:  # wf:phase-5:new
    """One recording override, bound to the parent implementation it shadows.

    A descriptor rather than a closure so the override carries its own name and
    its own target where a reader can see them, and so the mixin installs the
    whole set with one loop instead of fifty repeated bodies.
    """

    def __init__(self, verb):  # wf:phase-5:new
        self.verb = verb
        self.parent_method = getattr(SiteRegisterClient, verb)

    def __get__(self, client, owner=None):  # wf:phase-5:new
        if client is None:
            return self
        return functools.partial(self.call, client)

    def call(self, client, *args, **kwargs):  # wf:phase-5:new
        target = functools.partial(self.parent_method, client)
        return client.recording.perform_recorded_call(target, self.verb, "client",
                                                      {}, args, kwargs)


class RegisterRecorderMixin:  # wf:phase-5:new
    """Gives its subclass a recording override for every verb the client declares."""

    def __init_subclass__(cls, **kwargs):  # wf:phase-5:new
        super().__init_subclass__(**kwargs)
        for verb in RECORDED_VERBS:
            setattr(cls, verb, RecordedVerb(verb))

    def __init__(self, site, archive=None):  # wf:phase-5:new
        """Built by the site as ``SiteRegisterClient(site)``; the archive is the run's.

        Without one it attaches to the run the bench recipe published in
        ``GNR_BENCH_RUN`` — the channel that survives the worker spawn, since
        nothing else crosses into a process the pool starts fresh.
        """
        super().__init__(site)
        self.recording = BridgeCallRecorder(self, archive)


class RecordingRegisterClient(RegisterRecorderMixin, SiteRegisterClient):  # wf:phase-5:new
    """The bridge's register client with every command recorded."""
