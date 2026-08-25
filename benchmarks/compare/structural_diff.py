"""The structural comparison: two archived runs, exchange by exchange, and the
first difference that nothing declares.

The reference run and the replica run are joined by the `X-Bench-Replica-Of`
request header the replica stamps on every call it sends (`replica.py`): the
replica run's own archive says which reference exchange each of its exchanges
reproduces, so nothing outside the two archives has to be kept in step.

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
CLIENT_SURFACE = "client"
REGISTER_CLIENT_SURFACES = ("client", "passthrough")

# What the site answers a call arriving on a connection a login already replaced.
# Copied verbatim from `gnr/web/gnrwebpage.py:307`, typo and all: it is a literal
# the site writes, not a sentence, and correcting it here would match nothing.
CONNECTION_ROTATED = "The connection is not longer valid"


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
        text = DATETIME_REPR.sub("<datetime>", text)
        text = ISO_TIMESTAMP.sub("<ts>", text)
        text = ISO_DATE.sub("<date>", text)
        text = HEX_IDENTIFIER.sub("<hex>", text)
        return MINTED_IDENTIFIER.sub("<id>", text)

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


class DeclaredRules:
    """The table: every difference the bench recognises, and nothing else.

    It is born with the one rule already measured. The known bridge divergences
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
        """Which two runs are being compared, and on which genropy."""
        return "\n".join(f"{role}: {reader.path}\n"
                         f"  stack {conditions.get('stack')}, "
                         f"genropy {conditions.get('genropy_commit') or 'not declared'}"
                         for role, reader, conditions in
                         (("reference", self.reference, self.reference.conditions),
                          ("replica", self.replica, self.replica.conditions)))

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
            a=[shape.call for shape in left], b=[shape.call for shape in right],
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
