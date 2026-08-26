"""The structural comparison: two archived runs, exchange by exchange, and the
first difference that nothing declares.

The reference run and the replica run are joined by the `X-Bench-Replica-Of`
request header the replica stamps on every call it sends (`replica.py`): the
replica run's own archive says which reference exchange each of its exchanges
reproduces, so nothing outside the two archives has to be kept in step.

**Two lines are the same call when the call AND the caller agree.** The
alignment of the two sequences is keyed on both, because `store:getItem` is made
from all over the site: keyed on the call alone, the alignment pairs a general
preference read with a user preference read and reports their arguments as a
difference, while the real difference — an insertion elsewhere — goes unnamed
(measured 2026-08-26).

**Equal means equal by STRUCTURE.** Two exchanges agree when their register
lines carry the same sequence of calls and the same SHAPE of arguments and
answers. The shape is what survives a second run of the same session; the value
often is not, and a comparison that reads values reports the clock and the
random identifiers as differences of the stack:

- a 22-character identifier — page id, connection id, register item id — is
  masked, wherever it sits inside a longer string;
- a timestamp, a date and a `datetime.datetime(...)` repr are masked;
- an answer that is a Bag goes in as the PATHS of its nodes, with the values
  dropped and the attribute NAMES kept;
- an answer that is a dict repr goes in as the NAMES of its keys, values
  dropped: a register item that grows or loses a key is a difference, a
  register item whose `start_ts` moved is not;
- everything else is compared as its masked text, numbers included — a count
  that changes is a difference, and one of the cheapest to read.

The CALL is the surface plus the verb, and `client` and `passthrough` are one
surface here: they say how the recorder reached the method inside the register
client, not what the site asked, and the site cannot tell them apart. `store`
stays its own surface.

Measured on the pair of 2026-08-23/2026-08-25 (the browser session and its own
replay): 636 lines of the exchanges whose call sequence already agreed, 29
differing on the raw answer, 12 on the shape — and those 12 are real, four of
them a register item that carries three keys on one side and not on the other.

**What does not stop the run.** A difference recognised by a DECLARED rule is
reported as known and the replay carries on. The table is a mechanism with one
entry today, the reference race Phase 2 measured, and a rule is written only
from a signature somebody observed: the known bridge divergences of
`temp/problemi_genro_asgi_dal_ponte_2026-08-22.md` (S1, S2, S3, S5) are facts
between workers, invisible on a legacy-vs-legacy run, so their rules are added
when the first bridge cycle shows them and not before (foreman, 2026-08-25).

**Where the report is read.** Not here. `replica.py` asks this module after every
exchange it replays and stops at the first divergence nothing declares, printing
the report below — the run stops while the two stacks are still standing, which
is the whole point of a replica rather than an offline diff of two finished
traces (owner, 2026-08-23).
"""

import difflib
import json
import re
import xml.etree.ElementTree as ET

# The identifiers both stacks mint fresh at every run: 22 characters of
# base64url, the shape genropy gives a page id, a connection id and a register
# item id. Masked wherever it sits, including inside a longer string —
# `guest_<id>` is a user name the register builds around one.
MINTED_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{22}(?![A-Za-z0-9_-])")

# The exchange ids of the bench itself, 16 hex characters, and anything else
# written the same way.
HEX_IDENTIFIER = re.compile(r"\b[0-9a-f]{16,32}\b")

ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
DATETIME_REPR = re.compile(r"datetime\.datetime\([^)]*\)")

# A quoted name followed by a colon: how a key reads inside a dict repr.
DICT_KEY = re.compile(r"'([A-Za-z_][A-Za-z0-9_]*)':")

# The same, followed by None. A key carrying no value and a key that is not there
# are semantically identical (owner, 2026-08-25), so the shape drops both: a
# register item the daemon seeded to None and one the core simply does not carry
# say the same thing about the state, and reporting them as a difference sends the
# replica after a difference nobody can act on.
DICT_NULL_KEY = re.compile(r"'([A-Za-z_][A-Za-z0-9_]*)':\s*None\b")

# The two surfaces the recorder distinguishes INSIDE the register client: `client`
# when the class declares the method, `passthrough` when its `__getattr__` reaches
# it. That is how the recorder got there, not what the site asked or received, and
# the site cannot tell them apart — the legacy client hands most of its surface to
# `__getattr__`, the bridge declares every command explicitly by design. So the
# comparison reads them as one call, under the name of the declared one (foreman,
# 2026-08-25), so a declared rule matching on the call writes `client` and never
# has to know which stack declared the verb. `store` stays distinct: it is another
# object's surface, the live Bag's, not another way into this one.
# How much of a reply travels into the report around the first difference: enough
# to recognise the place, not so much that the report becomes the body.
REPLY_WINDOW = 240

CLIENT_SURFACE = "client"
REGISTER_CLIENT_SURFACES = ("client", "passthrough")

# What the site answers a call arriving on a connection a login already replaced.
# Copied verbatim from `gnr/web/gnrwebpage.py:307`, typo and all: it is a literal
# the site writes, not a sentence, and correcting it here would match nothing.
CONNECTION_ROTATED = "The connection is not longer valid"


class ReplyShape:
    """One reply body as it survives a second run: masked, and comparable whole.

    What the BROWSER receives is what the bridge has to reproduce, so the reply is
    compared entire and not only by its status (owner, 2026-08-26). Two families
    of difference are legitimate and go first: the identifiers each stack mints
    for itself — page ids, connection ids, register item ids — and the clock.
    What is left is a difference the browser would see, piggybacked datachanges
    and client data included, since those ride inside the same reply.
    """

    def __init__(self, body):
        self.body = body or ""

    @property
    def masked(self):
        """The body with minted identifiers, timestamps and dates masked."""
        text = DATETIME_REPR.sub("<datetime>", self.body)
        text = ISO_TIMESTAMP.sub("<ts>", text)
        text = ISO_DATE.sub("<date>", text)
        text = HEX_IDENTIFIER.sub("<hex>", text)
        return MINTED_IDENTIFIER.sub("<id>", text)

    def get_difference(self, other):
        """Where the two replies first part company, or None when they do not.

        A window around the first differing character rather than a line diff: a
        Bag reply is one long line, and a line diff of it says only that the line
        differs.
        """
        mine, theirs = self.masked, other.masked
        if mine == theirs:
            return None
        offset = 0
        for offset, (left, right) in enumerate(zip(mine, theirs)):
            if left != right:
                break
        else:
            offset = min(len(mine), len(theirs))
        start = max(0, offset - REPLY_WINDOW // 2)
        return (f"the replies differ at character {offset} of "
                f"{len(mine)}/{len(theirs)}\n"
                f"  legacy: ...{mine[start:offset + REPLY_WINDOW]}...\n"
                f"  bridge: ...{theirs[start:offset + REPLY_WINDOW]}...")


class LineShape:
    """The comparable shape of one register line: what survives a second run."""

    def __init__(self, record):
        self.record = record

    @property
    def call(self):
        """The surface and the verb: what the site asked the register to do."""
        surface = self.record.get("surface")
        return (CLIENT_SURFACE if surface in REGISTER_CLIENT_SURFACES else surface,
                self.record.get("verb"))

    @property
    def alignment_key(self):
        """What makes two lines the SAME call for the purpose of lining up.

        The call alone is not enough. `store:getItem` is made from all over the
        site, so two lines wearing it are matched by the alignment even when one
        reads a general preference and the other a user's — and the report then
        says "the arguments differ" about two calls that were never the same
        call. Measured on 2026-08-26: four insertions elsewhere in the sequence
        slid the alignment, and the divergence it reported named preferences
        while the cause was a service freshness check.

        The caller closes it. It is the site code that made the call, cut by
        dotted module name so the same file reads identically on the frozen copy
        the legacy runs and on the checkout the bridge runs (Phase 1) — which is
        exactly what a key has to be: equal when the two stacks did the same
        thing, different when they did not.
        """
        return (*self.call, self.caller)

    @property
    def arguments(self):
        """The shape of the positional and keyword arguments of the call."""
        return (json.dumps([self.get_shaped(value)
                            for value in (self.record.get("args") or [])],
                           sort_keys=True, default=repr),
                json.dumps({key: self.get_shaped(value)
                            for key, value in (self.record.get("kwargs") or {}).items()},
                           sort_keys=True, default=repr))

    @property
    def answer(self):
        """The shape of what the register answered."""
        return json.dumps(self.get_shaped(self.record.get("result")),
                          sort_keys=True, default=repr)

    @property
    def caller(self):
        """The site code that made the call, or a plain absence."""
        return self.record.get("site_caller") or "(no site_caller in this run)"

    @property
    def text(self):
        """The line as the report prints it: the call, its arguments, its answer."""
        arguments = ", ".join(str(self.get_shaped(value))
                              for value in (self.record.get("args") or []))
        keywords = ", ".join(f"{key}={self.get_shaped(value)}"
                             for key, value in (self.record.get("kwargs") or {}).items())
        signature = ", ".join(part for part in (arguments, keywords) if part)
        surface, verb = self.call
        return f"{surface}:{verb}({signature}) -> {self.get_shaped(self.record.get('result'))}"

    def get_shaped(self, value):
        """The value with everything a second run would legitimately change removed."""
        if not isinstance(value, str):
            return value
        if value.startswith("<?xml") and "GenRoBag" in value:
            paths = self.get_bag_paths(value)
            if paths is not None:
                return {"bag": paths}
        if value.startswith("{") and value.endswith("}"):
            keys = set(DICT_KEY.findall(value)) - set(DICT_NULL_KEY.findall(value))
            return {"dict": sorted(keys)}
        return self.get_masked(value)

    def get_masked(self, text):
        """The text with minted identifiers, timestamps and dates masked."""
        return ReplyShape(text).masked

    def get_bag_paths(self, xml):
        """The node paths of a Bag, values dropped, attribute names kept."""
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return None
        paths = []
        self.collect_bag_paths(root, "", paths)
        return sorted(paths)

    def collect_bag_paths(self, node, prefix, paths):
        for child in node:
            path = f"{prefix}.{child.tag}" if prefix else child.tag
            paths.append(f"{path}[{','.join(sorted(child.attrib))}]")
            self.collect_bag_paths(child, path, paths)


class Divergence:
    """One difference between the two runs, at one position of one exchange."""

    def __init__(self, exchange, position, kind, reference, replica, ordinal):
        self.exchange = exchange
        self.position = position
        self.kind = kind
        self.reference = reference
        self.replica = replica
        self.ordinal = ordinal
        self.known = None

    @property
    def where(self):
        """The exchange this happened in, as a reader recognises it."""
        return (f"[{self.position}] {self.exchange.get('method')} "
                f"{self.exchange.get('path')} "
                f"{self.exchange.get('rpc_method') or ''}".rstrip())

    @property
    def report(self):
        """The whole difference, readable without opening the code."""
        lines = [f"{'known' if self.known else 'DIVERGENCE'}: {self.kind} "
                 f"at register call {self.ordinal} of {self.where}"]
        if self.known:
            lines.append(f"  declared rule: {self.known}")
        lines.append(f"  reference: {self.reference.text if self.reference else '(no call)'}")
        lines.append(f"  replica:   {self.replica.text if self.replica else '(no call)'}")
        lines.append(f"  reference caller: {self.reference.caller if self.reference else '-'}")
        lines.append(f"  replica caller:   {self.replica.caller if self.replica else '-'}")
        return "\n".join(lines)


class DeclaredRule:
    """One difference the bench recognises instead of stopping on it.

    A rule answers one of the two questions, and `None` to the other: a recorded
    HTTP status the replay cannot reproduce, or a register divergence that is a
    known fact of the stack under comparison. Nothing is recognised implicitly —
    a difference with no rule stops the run.
    """

    name = "declared rule"

    def get_status_reason(self, trace, record):
        """Why the recorded status of this exchange cannot be replayed, or None."""
        return None

    def get_divergence_reason(self, divergence):
        """Why this register divergence is a known fact, or None."""
        return None


class ReferenceRace(DeclaredRule):
    """The status a browser produced by overlapping two calls, which a replay cannot.

    Two conditions, and both are read from the trace itself: the recorded reply
    says the connection had already been rotated, and the exchange was running
    while an earlier one was still in flight on the same cookie. A reply of the
    first kind alone proves nothing — a stale tab produces one too, and that one
    IS reproducible. Measured on the reference session of 2026-08-23: the two
    `login_doLogin` calls overlap by 22.8 ms, the first rotates the connection,
    the second answers 400, and a replay sending them one after the other gets
    the 200 the site owes a legitimate call.
    """

    name = "reference-race"

    def get_status_reason(self, trace, record):
        if CONNECTION_ROTATED not in (record.get("resp_body") or ""):
            return None
        overlapped = trace.get_overlapped_exchange(record)
        if overlapped is None:
            return None
        return (f"the connection was rotated by "
                f"{overlapped.get('rpc_method') or overlapped.get('path')}, "
                f"still in flight on the same cookie")


class StaleConnection(DeclaredRule):
    """The browser came back with a connection the register no longer knows.

    A comparative run starts from an EMPTY register, and a browser that was on
    the site before still holds the cookie of the run before: the site refuses
    the call, and the twin — which keeps a jar of its own and inherited nothing —
    answers the call normally. The two stacks are not disagreeing, they are being
    asked different questions, and comparing the register calls of a refusal with
    those of a served call says nothing about either.

    The signature is the same literal `ReferenceRace` reads, and the two rules are
    told apart by what is NOT there: a race has an earlier exchange still in
    flight on the same cookie, which rotated the connection a moment ago. This one
    has none — nothing rotated it, it simply outlived the register.

    Measured on the owner's first session through the twin proxy, 2026-08-25: the
    third exchange, `getRemoteTranslation`, answered 400 on the legacy and 200 on
    the bridge, with the cookie of the previous run in the request.
    """

    name = "stale-connection"

    def get_status_reason(self, trace, record):
        if CONNECTION_ROTATED not in (record.get("resp_body") or ""):
            return None
        if trace.get_overlapped_exchange(record) is not None:
            return None
        return ("the browser carried a connection from before this run, which "
                "started from an empty register")


class ServiceWarmup(DeclaredRule):
    """A service the freshly born worker had not instantiated yet.

    The bridge gives every user a worker of his own when the ceiling says so, and
    a worker born a moment ago has instantiated no service: the first request it
    serves resolves them, and each resolution reads the register. The legacy makes
    the very same calls — once, at the startup of its one long-lived process,
    outside any exchange a comparison looks at. So the two stacks do the same work
    at two different moments, and only one of the two moments is inside a compared
    exchange.

    The worker already settles what it can at birth (`genropy_worker.py`,
    `resources_dirs` and the local storages), which is where a resolution belongs.
    What stays is the tail nobody can pre-warm without opening remote volumes in a
    just-forked process, and the owner accepted it as a settling difference on
    2026-08-26: it sits on the register surface only — the reply the browser
    receives is compared apart, and agrees.

    NARROW ON PURPOSE. It recognises one thing: a call the REPLICA made and the
    reference did not, whose caller chain passes through the service resolution.
    A different order, a different argument, an extra call from anywhere else, and
    anything at all on the reference side, all stay divergences.
    """

    name = "service-warmup"

    # The two frames that say "this call is a service being resolved", read from
    # `gnr/lib/services/__init__.py` where `getService` instantiates on demand.
    SERVICE_RESOLUTION = ("lib/services/__init__.py", "getService")

    def get_divergence_reason(self, divergence):
        """The replica is resolving a service and the reference is not, or None.

        Both shapes the diff produces are covered by the same question. Where the
        extra call has no counterpart the alignment reports it as an insertion;
        where the call happens to wear the same surface and verb as the reference's
        — `store:getItem` on both, one reading a preference and the other a
        service definition — the alignment pairs them and reports the arguments.
        What tells the family apart is the CALLER, not the shape of the pairing.

        And when BOTH sides are resolving a service and still differ, that is a
        real difference: two warm-ups that do not agree are not a warm-up.
        """
        if divergence.replica is None or not self.is_resolution(divergence.replica):
            return None
        if divergence.reference is not None and self.is_resolution(divergence.reference):
            return None
        surface, verb = divergence.replica.call
        return (f"the worker was still instantiating a service — "
                f"{surface}:{verb}({divergence.replica.arguments}) under "
                f"{divergence.replica.caller.split(' <- ')[0]}")

    def is_resolution(self, shape):
        """Does this line come from a service being resolved on demand?"""
        return all(frame in shape.caller for frame in self.SERVICE_RESOLUTION)


class DeclaredRules:
    """The table: every difference the bench recognises, and nothing else.

    It is born with the one rule every driver needs. `StaleConnection` is NOT in
    it: the same reply means different things to the two drivers, and only the
    driver knows which. Replaying a recorded session, a reference 400 from a stale
    tab IS reproducible and must be reported; browsing live through the twin
    proxy, the browser's leftover connection is the proxy's own situation and the
    bridge cannot share it. So the proxy declares that rule in its own table —
    where ORDER MATTERS, because both rules read the same literal and
    `ReferenceRace` is the narrower: it must be asked first, or a race would be
    reported as a stale connection.

    The known bridge divergences
    (S1, S2, S3, S5) enter as the first cycle against the bridge shows each of
    them, with the owner's sign-off — a rule written from a document would
    declare a signature nobody observed.
    """

    def __init__(self, rules=None):
        self.rules = list(rules) if rules is not None else [ReferenceRace()]

    @property
    def names(self):
        return [rule.name for rule in self.rules]

    def get_status_reason(self, trace, record):
        """The rule and the reason recognising this recorded status, or None."""
        for rule in self.rules:
            reason = rule.get_status_reason(trace, record)
            if reason:
                return f"{rule.name}: {reason}"
        return None

    def get_divergence_reason(self, divergence):
        """The rule and the reason recognising this divergence, or None."""
        for rule in self.rules:
            reason = rule.get_divergence_reason(divergence)
            if reason:
                return f"{rule.name}: {reason}"
        return None


class StructuralDiff:
    """The comparison of a reference run with the replica run reproducing it."""

    def __init__(self, reference, replica, rules=None):
        self.reference = reference
        self.replica = replica
        self.rules = rules or DeclaredRules()
        self.known = []

    @property
    def header(self):
        """Which two runs are being compared, and under which declared conditions.

        Everything on these lines is read from the archives themselves, never from
        the command line: a report that names its own inputs can be read months
        later beside the two files it names.
        """
        lines = []
        for role, reader, conditions in (
                ("reference", self.reference, self.reference.conditions),
                ("replica", self.replica, self.replica.conditions)):
            database = conditions.get("database") or {}
            # the replica's own count is 0 here by construction: the header is
            # printed before the replay writes anything into the file the target
            # minted at startup.
            size = (f", {len(reader.records)} exchanges recorded"
                    if role == "reference" else ", recording from now")
            lines.append(
                f"{role}: {reader.path}\n"
                f"  stack {conditions.get('stack')}, "
                f"genropy {conditions.get('genropy_commit') or 'not declared'}\n"
                f"  instance {conditions.get('sitename') or 'not declared'}, "
                f"db {database.get('dbname') or 'not declared'}{size}")
        return "\n".join(lines)

    def get_divergence(self, reference_exchange, replica_exchange, position):
        """The first difference between the two exchanges, or None.

        A difference a declared rule recognises is recorded as known and does not
        come back: the caller stops only on what nothing declares.
        """
        for divergence in self.get_differences(reference_exchange,
                                               replica_exchange, position):
            divergence.known = self.rules.get_divergence_reason(divergence)
            if not divergence.known:
                return divergence
            self.known.append(divergence)
        return None

    def get_differences(self, reference_exchange, replica_exchange, position):
        """Every difference between the two exchanges, in the order they happened."""
        left = [LineShape(record) for record
                in self.reference.get_register_lines(reference_exchange["exchange_id"])]
        right = [LineShape(record) for record
                 in self.replica.get_register_lines(replica_exchange["exchange_id"])]
        differences = []
        matcher = difflib.SequenceMatcher(
            a=[shape.alignment_key for shape in left],
            b=[shape.alignment_key for shape in right],
            autojunk=False)
        for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
            if tag == "equal":
                differences.extend(self.get_value_differences(
                    left, right, left_start, left_end, right_start,
                    reference_exchange, position))
            else:
                differences.append(self.get_call_difference(
                    tag, left, right, left_start, left_end, right_start, right_end,
                    reference_exchange, position))
        differences.sort(key=lambda difference: difference.ordinal)
        return differences

    def get_value_differences(self, left, right, left_start, left_end,
                              right_start, exchange, position):
        """Where two calls that agree carry arguments or answers that do not."""
        differences = []
        for step in range(left_end - left_start):
            one, other = left[left_start + step], right[right_start + step]
            if one.arguments != other.arguments:
                kind = "arguments"
            elif one.answer != other.answer:
                kind = "answer"
            else:
                continue
            differences.append(Divergence(exchange, position, kind, one, other,
                                          left_start + step + 1))
        return differences

    def get_call_difference(self, tag, left, right, left_start,
                            left_end, right_start, right_end, exchange, position):
        """Where the two runs made different calls, or a different number of them."""
        kind = {"insert": "extra call in the replica",
                "delete": "call missing in the replica"}.get(tag, "different call")
        return Divergence(exchange, position, kind,
                          left[left_start] if left_end > left_start else None,
                          right[right_start] if right_end > right_start else None,
                          left_start + 1)
