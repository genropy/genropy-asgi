"""The twin proxy: the owner browses through it, and BOTH stacks answer.

There is no recorded session here. The owner works in a browser against this
proxy, and every request he makes is performed twice — first on the legacy
stack, then on the bridge. The LEGACY answer is the one the browser receives, so
a fault on the bridge never blocks his work; the bridge is the shadow, and what
it answers is only ever compared, never shown.

**One process owns the whole cycle.** This reverses the rule the earlier phases
worked under, where the database copy and the two launchers were steps run by
hand. With a live proxy the owner drives from a browser in real time, so the
orchestration cannot be a sequence of shell commands: the copy, the two stacks
and the comparison have to be one thing that starts and stops together.

  copy the db -> empty the legacy register -> start its daemon -> start
  gunicorn -> start the bridge -> serve, compare, stop

The legacy stack is TWO processes, and that is why its own register daemon is on
that line: `SiteRegisterClient` reads the daemon's Pyro address out of
`site/sitedaemon.xml` when the site is built, so gunicorn started first would
hold an address nobody answers. The daemon also SAVES its register on stop and
restores it on start, so the two saved pickles are deleted before it goes up —
every comparative run begins from an empty register, and a surviving connection
would keep the browser logged in and leave the login out of the session.

**The two instances.** The command line names the instance the LEGACY serves;
the bridge serves that name plus `_asgi` (`sandbox` -> `sandbox_asgi`, owner
2026-08-25). The `_asgi` twin is configuration only — the same `root.py` and
`siteconfig.xml`, an `instanceconfig.xml` whose `db` node names the COPY, and no
`sitedaemon.xml`. This proxy does not create it: it reads both
`instanceconfig.xml` files for the two database names, and refuses with the
commands to run when the twin is not there.

**The copy is made before either stack starts**, and that order is not a
preference: `createdb -T` needs its template free of connections, and the
template is the very database the legacy stack is about to open.

**Sequence, not parallelism.** The two legs of one request go out one after the
other, never together: run in parallel they contend for CPU and for the
database, and both timings are dirtied. Sequential warms the second, which is
its own bias — either way the timings here are indicative, and the speed verdict
belongs to macro-phase 3, with collection off.

**What is dispatched, and what is compared, are two different questions.**
Everything the browser sends is dispatched to both stacks, statics included: the
bridge must see exactly the traffic the legacy sees, or its state could diverge
for a reason we introduced. Only the comparison is selective — a static carries
the same pair of register calls every time and says nothing, so it is dispatched
and not compared. The consequence is on record: the serving of static assets is
never compared between the two stacks.

**The join between the two archives** is the one `replica.py` already uses, and
it is reached in two steps because the exchange id is minted inside the stack
that serves the request, not by whoever sends it:

- the legacy leg carries `X-Bench-Twin-Request`, this proxy's own ordinal, which
  the HTTP recorder writes into the line like any other request header; the
  proxy then reads back the legacy exchange id belonging to that ordinal;
- the bridge leg carries `X-Bench-Replica-Of` with that legacy exchange id,
  which is exactly what `TraceReader.get_exchange_replaying` and
  `StructuralDiff` already read. Neither module is modified.

**Identifiers.** The bridge mints its own page ids, so the same `IdentityMap` the
replica uses rewrites the tokens of the legacy request into the tokens the bridge
minted, in the query string and in the body alike. Cookies are never forwarded to
the bridge: the browser's jar belongs to the legacy stack, and this proxy keeps a
jar of its own for the shadow. Every OTHER request header is forwarded as it
came — the user agent among them, because the site writes it into the connection
register item, and a missing one would read as a divergence of the stack.

**The two rules inherited from Phase 7, both binding.** The exchanges before the
first RPC are not compared, each stack finishing its lazy build there and the
bridge doing it in the template whose lines are dropped by construction. And the
`+28%` is not a pending divergence: it was measured with an instrument that could
not attribute a call, so anything of it that reappears here is a new measurement
with attribution, not a continuation of the old figure.

**The stop leaves everything standing.** At the first divergence nothing
declares, the proxy prints the report, writes it beside the archives, and stops
dispatching to the bridge — it does NOT exit, and neither stack is torn down. The
whole point of comparing live is that the two stacks are still answering while
the divergence is investigated; a proxy that died at the stop would leave the
archives as the only evidence. The legacy keeps serving, so the browser stays
usable.

Run, from the repository root:

  GENRO_GNRFOLDER=$PWD/temp/gnr \\
      PYTHONPATH=<pinned genropy>/gnrpy \\
      GNR_DAEMON_PROVIDER=genropy-asgi \\
      PGGSSENCMODE=disable python benchmarks/compare/twin_proxy.py \\
      test_invoice_pg_legacy --run "the invoice session"

Then browse http://127.0.0.1:8097. The environment is the bridge's own, because
this process imports genropy to read the instance configuration and asks
`genropy_parity_check.py` whether the two stacks carry the same source; the
legacy child is started WITHOUT `PYTHONPATH`, so it keeps importing the frozen
copy inside `temp/legacy_venv` as it always has. `GNR_DAEMON_PROVIDER` is the
bridge's own condition and the run refuses without it; the legacy child does not
receive it, because on that stack genropy must keep its own daemon client.
"""

import argparse
import http.client
import http.server
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.dirname(BENCH_DIR)
BENCH_ROOT = os.path.dirname(BENCH)
sys.path.insert(0, BENCH)

from gnr.app.gnrdeploy import PathResolver           # noqa: E402
from gnr.app.pathresolver import EntityNotFoundException   # noqa: E402
from gnr.core.gnrbag import Bag                      # noqa: E402

from genropy_parity_check import GenropyParity       # noqa: E402
from http_recorder import STATIC_CONTENT_TYPES       # noqa: E402
from replica import (EXCHANGE_POLL_SECONDS, EXCHANGE_WAIT_SECONDS,  # noqa: E402
                     REPLICA_HEADER, IdentityMap, ReplicaClient, TraceReader)
from run_archive import (ARCHIVE_DIR_ENV, DEFAULT_ARCHIVE_DIR,  # noqa: E402
                         RUN_NAME_ENV)
from structural_diff import DeclaredRules, StructuralDiff   # noqa: E402

# This proxy's own ordinal on the legacy leg. The exchange id is minted inside
# the stack, so the sender needs a mark of its own to find the line it caused.
TWIN_HEADER = "X-Bench-Twin-Request"

# The suffix that names the bridge's twin of an instance (owner, 2026-08-25).
BRIDGE_SUFFIX = "_asgi"

# The bridge's daemon override engages only when genropy is told which provider
# to use (genropy #1070). Unset, `gnr.web.daemon.siteregister_client` stays
# genropy's own, the bench's recording client cannot even be built on top of it —
# it binds commands the classic client reaches through `__getattr__` — and the
# bridge template dies importing its engine factory. So the run refuses instead:
# this is a condition of the run, declared by whoever starts it.
DAEMON_PROVIDER_ENV = "GNR_DAEMON_PROVIDER"
DAEMON_PROVIDER = "genropy-asgi"

LEGACY_PYTHON = os.path.join(BENCH_ROOT, "temp", "legacy_venv", "bin", "python")
LEGACY_DAEMON = os.path.join(BENCH_ROOT, "temp", "legacy_venv", "bin", "gnrdaemon")
SERVE_LEGACY = os.path.join(BENCH_DIR, "serve_legacy.py")
SERVE_BRIDGE = os.path.join(BENCH_DIR, "serve_bridge.py")
GUNICORN_RECORDERS = os.path.join(BENCH_DIR, "gunicorn_recorders.conf.py")

# What each launcher prints when its archive exists, and when it is serving.
ARCHIVE_LINE = re.compile(r"recording run \S+ into (\S+)")
LEGACY_READY = "Listening at:"
BRIDGE_READY = "Application startup complete"
STARTUP_WAIT_SECONDS = 180.0
STARTUP_POLL_SECONDS = 0.2

# The sitedaemon says nothing when it is up: it writes this descriptor in the
# site folder, carrying the Pyro address and its own pid, and `SiteRegisterClient`
# reads `register_uri` out of it when the site is built. So the descriptor naming
# THIS process is the readiness signal, and the pid is what tells a fresh one from
# the file an earlier daemon left behind. It sits in the SITE folder, beside the
# two register pickles.
DAEMON_DESCRIPTOR = "sitedaemon.xml"

# The register the daemon saves on stop and restores on start. Every comparative
# run begins from an EMPTY register — on the bridge a restart wipes it, so a
# legacy run carrying yesterday's connections is not comparable. And a surviving
# connection keeps the browser logged in, which would leave the login out of the
# session altogether.
REGISTER_PICKLES = ("siteregister_data.pik", "siteregister_data_loaded.pik")

# Headers that belong to one hop of the connection and are never relayed, plus
# the two the proxy writes itself when it answers the browser.
HOP_BY_HOP = frozenset(["connection", "keep-alive", "proxy-authenticate",
                        "proxy-authorization", "te", "trailers",
                        "transfer-encoding", "upgrade"])
REPLY_OWNED = frozenset(["content-length", "date", "server"])

# The exchange whose request carried a given header: one query on the archive,
# never a walk over every line it holds. An hour of browsing writes thousands of
# lines, and a walk per request would grow with the session.
EXCHANGE_BY_HEADER = (
    "SELECT line FROM record WHERE kind = 'http' "
    "AND json_extract(line, '$.req_headers.\"{header}\"') = ? "
    "ORDER BY id DESC LIMIT 1")


class TwinInstances:
    """The two instances of a twin run, and the two databases behind them."""

    def __init__(self, legacy_name):
        self.legacy_name = legacy_name
        self.resolver = PathResolver()

    @property
    def bridge_name(self):    # wf:phase-8:new
        """The instance the bridge serves: the legacy one plus the suffix."""
        return f"{self.legacy_name}{BRIDGE_SUFFIX}"

    def get_instance_path(self, name):    # wf:phase-8:new
        return self.resolver.instance_name_to_path(name)

    def get_database(self, name):    # wf:phase-8:new
        """The db node of an instance, read from its own instanceconfig.xml."""
        path = os.path.join(self.get_instance_path(name), "instanceconfig.xml")
        return dict(Bag(path).getAttr("db"))

    @property
    def legacy_database(self):    # wf:phase-8:new
        return self.get_database(self.legacy_name)

    @property
    def bridge_database(self):    # wf:phase-8:new
        return self.get_database(self.bridge_name)

    @property
    def ready(self):    # wf:phase-8:new
        """Is the bridge's twin instance there, and does it name another db?

        The resolver raises when the instance does not exist, which is an answer
        to this question and not a failure of the run: the refusal below names
        the commands that create it.
        """
        try:
            path = self.get_instance_path(self.bridge_name)
        except EntityNotFoundException:
            return False
        if not os.path.exists(os.path.join(path, "instanceconfig.xml")):
            return False
        return self.bridge_database.get("dbname") != self.legacy_database.get("dbname")

    @property
    def report(self):    # wf:phase-8:new
        """What is missing and how to make it: a refusal names its own remedy."""
        source = self.get_instance_path(self.legacy_name)
        target = os.path.join(os.path.dirname(source), self.bridge_name)
        return (
            f"the bridge serves {self.bridge_name}, and it is not usable.\n"
            f"It must exist, and its db node must name a database of its own —\n"
            f"configuration only: no sitedaemon.xml, no site/data, no _static.\n\n"
            f"  mkdir -p {target}/site\n"
            f"  sed 's/dbname=\"{self.legacy_database.get('dbname')}\"/"
            f"dbname=\"{self.bridge_name}\"/' \\\n"
            f"      {source}/instanceconfig.xml > {target}/instanceconfig.xml\n"
            f"  cp {source}/root.py {target}/root.py\n"
            f"  cp {source}/site/siteconfig.xml {target}/site/siteconfig.xml")


class DatabaseCopy:
    """The bridge's database, dropped and made again from the legacy's own."""

    def __init__(self, source, target):
        self.source = source
        self.target = target

    @property
    def connection_options(self):    # wf:phase-8:new
        """Host and port as the instance declares them, and nothing invented."""
        options = []
        for flag, key in (("-h", "host"), ("-p", "port"), ("-U", "user")):
            value = self.source.get(key)
            if value:
                options += [flag, str(value)]
        return options

    @property
    def commands(self):    # wf:phase-8:new
        """The two commands, in the order they have to run."""
        return [["dropdb", *self.connection_options, "--if-exists",
                 self.target["dbname"]],
                ["createdb", *self.connection_options, "-T",
                 self.source["dbname"], self.target["dbname"]]]

    def make_copy(self):    # wf:phase-8:new
        """Drop the copy and make it again; a failure stops the run, loudly."""
        for command in self.commands:
            print(f"  {' '.join(command)}")
            subprocess.run(command, check=True)


class StackProcess:
    """One launched stack: its process, its output, and the archive it minted."""

    def __init__(self, label, command, environment,
                 ready_token=None, mints_archive=True):
        self.label = label
        self.command = command
        self.environment = environment
        self.ready_token = ready_token
        self.mints_archive = mints_archive
        self.process = None
        self.archive_path = None
        self.minted = threading.Event()
        self.serving = threading.Event()

    def launch_process(self):    # wf:phase-8:new
        """Start the child and read its output for as long as it lives."""
        self.process = subprocess.Popen(
            self.command, env=self.environment, cwd=BENCH_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        threading.Thread(target=self.read_output, daemon=True).start()

    def read_output(self):    # wf:phase-8:new
        """Relay every line, and watch for the archive and for readiness."""
        for line in self.process.stdout:
            print(f"[{self.label}] {line.rstrip()}", flush=True)
            found = ARCHIVE_LINE.search(line)
            if found:
                self.archive_path = found.group(1)
                self.minted.set()
            if self.ready_token and self.ready_token in line:
                self.serving.set()

    @property
    def is_serving(self):    # wf:phase-8:new
        """Is this stack up? Read from the LOG, never from a request to the site.

        A readiness probe is an exchange, and the recorder writes it into the
        archive as a line no session asked for.
        """
        return self.serving.is_set() and (self.minted.is_set() or not self.mints_archive)

    def wait_serving(self, timeout=STARTUP_WAIT_SECONDS):    # wf:phase-8:new
        """Wait until it serves — or until it dies, which is an answer too."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_serving:
                return
            if self.process.poll() is not None:
                raise SystemExit(f"{self.label} died before it was serving "
                                 f"(exit {self.process.returncode}); its output is above")
            time.sleep(STARTUP_POLL_SECONDS)
        raise SystemExit(f"{self.label} did not come up within {timeout:.0f}s")

    def stop(self):    # wf:phase-8:new
        """Ask the child to go, then insist; the pool goes with the launcher."""
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()


class SiteDaemonProcess(StackProcess):
    """The legacy stack's register server: another process, on a Pyro socket.

    The legacy stack is TWO processes, and gunicorn is the second of them:
    `SiteRegisterClient` reads the Pyro address out of the descriptor when the
    site is built, so a worker started before the daemon holds an address that
    answers nobody. That is why this one goes up first and is waited for.
    """

    def __init__(self, label, command, environment, site_path):
        super().__init__(label, command, environment, mints_archive=False)
        self.site_path = site_path

    @property
    def descriptor_path(self):    # wf:phase-8:new
        return os.path.join(self.site_path, DAEMON_DESCRIPTOR)

    def clear_register(self):    # wf:phase-8:new
        """Delete the saved register, so the run starts from an empty one."""
        for name in REGISTER_PICKLES:
            path = os.path.join(self.site_path, name)
            if os.path.exists(path):
                print(f"  removing {path}")
                os.remove(path)

    @property
    def is_serving(self):    # wf:phase-8:new
        """Does the descriptor name THIS process? Then the register is answering."""
        if not os.path.exists(self.descriptor_path):
            return False
        return Bag(self.descriptor_path).getAttr("params").get("pid") == self.process.pid


class ShadowLeg:
    """One request waiting for its shadow leg, and the answer that it is done."""

    def __init__(self, method, target, headers, body, twin, status, reply_headers):
        self.method = method
        self.target = target
        self.headers = headers
        self.body = body
        self.twin = twin
        self.status = status
        self.reply_headers = reply_headers
        self.done = threading.Event()


class ShadowClient(ReplicaClient):
    """The bridge-side client: the browser's own headers, this side's cookies."""

    def send_shadow(self, method, target, headers, body):    # wf:phase-8:new
        """One shadow request: the headers as they came, the jar as it is here.

        The connection is kept alive across requests and the server may close it
        while the owner is reading a page — uvicorn does after a few idle seconds.
        That is ordinary HTTP, so a dead connection is reopened and the request
        sent once more; a second failure is the bridge's, and it travels up.
        """
        outgoing = {key: value for key, value in headers.items()
                    if key.lower() not in HOP_BY_HOP and key.lower() != "cookie"}
        outgoing.update(self._headers())
        try:
            return self.send_once(method, target, outgoing, body)
        except (http.client.RemoteDisconnected, http.client.CannotSendRequest,
                http.client.BadStatusLine, ConnectionResetError, BrokenPipeError):
            self.conn.close()
            return self.send_once(method, target, outgoing, body)

    def send_once(self, method, target, headers, body):    # wf:phase-8:new
        """One attempt on the current connection; http.client reopens a closed one."""
        self.conn.request(method, target, body=body, headers=headers)
        response = self.conn.getresponse()
        answer = response.read()
        self._store_cookies(response)
        return response.status, answer


class TwinProxy:
    """The proxy the owner browses through, and the comparison it runs behind him."""

    def __init__(self, instances, legacy, bridge, port,
                 legacy_port, bridge_port, run_name, rules=None):
        self.instances = instances
        self.legacy = legacy
        self.bridge = bridge
        self.port = port
        self.legacy_port = legacy_port
        self.bridge_port = bridge_port
        self.run_name = run_name
        self.rules = rules or DeclaredRules()
        self.reference = None
        self.shadow = None
        self.diff = None
        self.client = None
        self.identity = IdentityMap()
        self.ordinal_lock = threading.Lock()
        self.legs = queue.Queue()
        self.opened = threading.Event()
        self.ordinal = 0
        self.compared = 0
        self.dispatched = 0
        # the cold start, decided as the session goes instead of by rescanning:
        # once the first RPC has been served, nothing after it is cold any more.
        self.rpc_served = False
        self.divergence = None
        self.server = None

    def open_comparison(self):    # wf:phase-8:new
        """Both stacks are up: start the shadow thread and wait for it to be ready."""
        threading.Thread(target=self.run_shadow, daemon=True).start()
        self.opened.wait()
        print(f"\ndeclared run: {self.run_name}")
        print(f"browse http://127.0.0.1:{self.port} — the legacy answers you, "
              f"the bridge shadows every request")

    def run_shadow(self):    # wf:phase-8:new
        """The one thread that talks to the bridge and reads the two archives.

        A thread of its own, and not a lock, for a reason each half of the work
        gives on its own: SQLite refuses a connection used from a thread other
        than the one that opened it, and serving is threaded; and the two legs of
        a request have to stay sequential, which a queue makes structural instead
        of something every future caller has to remember.
        """
        self.reference = TraceReader(self.legacy.archive_path)
        self.shadow = TraceReader(self.bridge.archive_path)
        self.diff = StructuralDiff(self.reference, self.shadow, self.rules)
        self.client = ShadowClient("127.0.0.1", self.bridge_port)
        print(self.diff.header)
        self.opened.set()
        while True:
            leg = self.legs.get()
            try:
                self.follow(leg)
            except Exception as failure:
                self.stop_shadowing(f"the shadow leg of {leg.method} "
                                    f"{leg.target} failed: {failure!r}")
            finally:
                leg.done.set()

    @property
    def report_path(self):    # wf:phase-8:new
        """Where the divergence report is written, beside the two archives."""
        directory = os.environ.get(ARCHIVE_DIR_ENV) or DEFAULT_ARCHIVE_DIR
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", self.run_name).strip("-")
        return os.path.join(directory, f"{name}-divergence.txt")

    def get_next_twin(self):    # wf:phase-8:new
        """The mark this proxy puts on the legacy leg of the next request."""
        with self.ordinal_lock:
            self.ordinal += 1
            return f"twin-{self.ordinal:05d}"

    def send_legacy(self, method, target, headers, body, twin):    # wf:phase-8:new
        """The leg the browser waits on: one connection, one request, one reply."""
        outgoing = {key: value for key, value in headers.items()
                    if key.lower() not in HOP_BY_HOP}
        outgoing[TWIN_HEADER] = twin
        connection = http.client.HTTPConnection("127.0.0.1", self.legacy_port,
                                                timeout=300)
        try:
            connection.request(method, target, body=body, headers=outgoing)
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def dispatch(self, method, target, headers, body):    # wf:phase-8:new
        """One request on both stacks, in sequence; the LEGACY reply is returned."""
        twin = self.get_next_twin()
        status, reply_headers, reply_body = self.send_legacy(
            method, target, headers, body, twin)
        self.dispatched += 1
        if self.divergence is None:
            # The shadow leg runs on its own thread and this one waits for it:
            # in sequence, never together. Whatever happens over there is a
            # divergence to report, never a failure the browser is told about —
            # the owner's work does not stop because the twin did.
            leg = ShadowLeg(method, target, headers, body, twin,
                            status, reply_headers)
            self.legs.put(leg)
            leg.done.wait()
        return status, reply_headers, reply_body

    def follow(self, leg):    # wf:phase-8:new
        """The shadow leg and its comparison; a stop here never touches the browser."""
        reference = self.get_marked_exchange(self.reference, TWIN_HEADER, leg.twin)
        if reference is None:
            self.stop_shadowing(f"the legacy archive carries no exchange marked "
                                f"{leg.twin}: the comparison has lost its reference")
            return
        self.client.replaying = reference["exchange_id"]
        shadow_status, shadow_body = self.client.send_shadow(
            leg.method, self.identity.get_adapted(leg.target), leg.headers,
            self.get_adapted_body(leg.body))
        self.identity.learn_page_id(reference.get("resp_body"),
                                    shadow_body.decode("utf-8", "replace"))
        label = self.get_label(reference, leg.method, leg.target)
        if self.is_static(leg.target, leg.reply_headers):
            print(f"  {label}  {leg.status}/{shadow_status}  static, not compared")
            return
        self.compare(reference, label, leg.status, shadow_status)

    def get_adapted_body(self, body):    # wf:phase-8:new
        """The body with the legacy's identifiers rewritten into the bridge's.

        Decoded as latin-1 and encoded back: the tokens are ASCII, and every
        other byte of the request survives the round trip untouched.
        """
        if not body:
            return body
        return self.identity.get_adapted(body.decode("latin-1")).encode("latin-1")

    def is_static(self, target, reply_headers):    # wf:phase-8:new
        """The recorder's own rule, read off the legacy reply: the content type decides."""
        if target.split("?")[0].endswith("favicon.ico"):
            return True
        for key, value in reply_headers:
            if key.lower() == "content-type":
                return any(token in value.lower() for token in STATIC_CONTENT_TYPES)
        return False

    def get_marked_exchange(self, reader, header, value):    # wf:phase-8:new
        """The exchange whose request carried that header, once it is written.

        The HTTP recorder writes its line in the generator's `finally`, after the
        last chunk has left, so the sender can hold the whole reply a moment
        before the line exists — hence the wait.
        """
        query = EXCHANGE_BY_HEADER.format(header=header)
        deadline = time.time() + EXCHANGE_WAIT_SECONDS
        while time.time() < deadline:
            row = reader.connection.execute(query, (value,)).fetchone()
            if row is not None:
                return json.loads(row[0])
            time.sleep(EXCHANGE_POLL_SECONDS)
        return None

    def get_label(self, reference, method, target):    # wf:phase-8:new
        """The exchange as one readable name: the call, or the URL that carried it."""
        rpc = reference.get("rpc_method")
        return f"{method} {target.split('?')[0]}{f' {rpc}' if rpc else ''}"

    def is_cold_start(self, reference):    # wf:phase-8:new
        """Is this exchange still part of the cold start?

        Everything before the first RPC is: each stack finishes building lazily
        there, and the bridge does it in the template whose register lines are
        dropped by construction. The first RPC itself is compared, and from it on
        the session is never cold again — so the question is asked of this
        exchange and of what came before, never of the whole archive.
        """
        if reference.get("rpc_method"):
            self.rpc_served = True
            return False
        return not self.rpc_served

    def compare(self, reference, label, status, shadow_status):    # wf:phase-8:new
        """Statuses first, then the register calls; the first stop ends the shadow."""
        if self.is_cold_start(reference):
            print(f"  {label}  {status}/{shadow_status}  before the first RPC, "
                  f"not compared")
            return
        shadow = self.get_marked_exchange(self.shadow, REPLICA_HEADER,
                                          reference["exchange_id"])
        if shadow is None:
            self.stop_shadowing(f"{label}: the bridge archive carries no exchange "
                                f"stamped with {reference['exchange_id']}")
            return
        reference_lines = len(self.reference.get_register_lines(reference["exchange_id"]))
        shadow_lines = len(self.shadow.get_register_lines(shadow["exchange_id"]))
        timings = (f"{self.get_timing(reference)}/{self.get_timing(shadow)} ms")
        print(f"  {label}  {status}/{shadow_status}  "
              f"reg {reference_lines}/{shadow_lines}  {timings}")
        self.compared += 1
        if shadow_status != status:
            self.stop_shadowing(f"{label}: the legacy answered {status}, "
                                f"the bridge {shadow_status}")
            return
        divergence = self.diff.get_divergence(reference, shadow, self.compared)
        if divergence is not None:
            self.stop_shadowing(divergence.report)

    def get_timing(self, record):    # wf:phase-8:new
        """The response time the recorder measured inside the stack that served it."""
        duration = record.get("duration_ms")
        return "-" if duration is None else f"{duration:.0f}"

    def stop_shadowing(self, report):    # wf:phase-8:new
        """Print the divergence, write it down, and stop dispatching to the bridge.

        Nothing is torn down. Both stacks keep answering, which is what makes the
        divergence investigable at all, and the legacy keeps serving the browser.
        """
        self.divergence = report
        text = (f"DIVERGENCE — run {self.run_name}\n"
                f"legacy archive: {self.legacy.archive_path}\n"
                f"bridge archive: {self.bridge.archive_path}\n"
                f"{self.dispatched} request(s) dispatched, {self.compared} compared\n\n"
                f"{report}\n")
        with open(self.report_path, "w") as report_file:
            report_file.write(text)
        print(f"\n{text}\nshadow off: the bridge is no longer dispatched to. "
              f"Both stacks are still up.\nreport written to {self.report_path}",
              flush=True)

    def serve(self):    # wf:phase-8:new
        """Listen until something stops us; the finally is the only teardown."""
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port),
                                                      TwinHandler)
        self.server.proxy = self
        self.server.daemon_threads = True
        try:
            self.server.serve_forever()
        finally:
            self.server.server_close()


class TwinHandler(http.server.BaseHTTPRequestHandler):
    """One browser request: to the legacy, to the bridge, then back to the browser."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        """Silent: the proxy prints one line per exchange, with the comparison on it."""

    def do_GET(self):
        self.relay("GET")

    def do_POST(self):
        self.relay("POST")

    @property
    def request_body(self):    # wf:phase-8:new
        """The body as the browser sent it; a chunked request is refused, not guessed."""
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            raise RuntimeError("the twin proxy does not relay chunked requests")
        length = self.headers.get("Content-Length")
        return self.rfile.read(int(length)) if length else b""

    def relay(self, method):    # wf:phase-8:new
        """The whole exchange, from this side of the wire."""
        body = self.request_body
        status, headers, answer = self.server.proxy.dispatch(
            method, self.path, dict(self.headers.items()), body)
        self.send_response(status)
        for key, value in headers:
            if key.lower() in HOP_BY_HOP or key.lower() in REPLY_OWNED:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)


class TwinRun:
    """The whole cycle: the copy, the two stacks, the proxy, and the teardown."""

    def __init__(self, arguments):
        self.arguments = arguments
        self.instances = TwinInstances(arguments.instance)
        self.daemon = None
        self.legacy = None
        self.bridge = None
        self.proxy = None

    @property
    def parity(self):    # wf:phase-8:new
        return GenropyParity()

    def get_environment(self, bridge):    # wf:phase-8:new
        """The child's environment: the run name always, the bridge's two only there.

        The legacy child gets neither the pin on its import path — it imports the
        frozen copy of the same source, which is what makes it the legacy stack —
        nor the daemon provider, which on that stack must stay genropy's own.
        """
        environment = dict(os.environ, PGGSSENCMODE="disable")
        environment[RUN_NAME_ENV] = self.arguments.run
        if not bridge:
            environment.pop("PYTHONPATH", None)
            environment.pop(DAEMON_PROVIDER_ENV, None)
        return environment

    @property
    def daemon_command(self):    # wf:phase-8:new
        """With a sitename it runs the site register server and stays up.

        Without one it starts the multi-site daemon, which spawns its children
        with multiprocessing and dies on macOS.
        """
        return [LEGACY_DAEMON, self.instances.legacy_name]

    @property
    def legacy_command(self):    # wf:phase-8:new
        return [LEGACY_PYTHON, SERVE_LEGACY, self.instances.legacy_name,
                "-b", f"127.0.0.1:{self.arguments.legacy_port}",
                "-w", str(self.arguments.workers), "-k", "gthread",
                "--threads", "16", "-c", GUNICORN_RECORDERS]

    @property
    def bridge_command(self):    # wf:phase-8:new
        return [sys.executable, SERVE_BRIDGE, self.instances.bridge_name,
                "-p", str(self.arguments.bridge_port), "--nodebug"]

    def check_ground(self):    # wf:phase-8:new
        """Refuse before anything is started, and name the remedy every time."""
        parity = self.parity
        if not parity.aligned:
            raise SystemExit(parity.report)
        if not self.instances.ready:
            raise SystemExit(self.instances.report)
        for path in (LEGACY_PYTHON, LEGACY_DAEMON):
            if not os.path.exists(path):
                raise SystemExit(f"the legacy venv is not complete: no {path}")
        if os.environ.get(DAEMON_PROVIDER_ENV) != DAEMON_PROVIDER:
            raise SystemExit(
                f"{DAEMON_PROVIDER_ENV} is not {DAEMON_PROVIDER!r}: the bridge "
                f"would build the site on genropy's own register client, and its "
                f"recording factory would not import. Remedy:\n"
                f"  export {DAEMON_PROVIDER_ENV}={DAEMON_PROVIDER}")

    def start_stacks(self):    # wf:phase-8:new
        """The whole order, and none of it is a preference.

        The COPY first, because `createdb -T` needs its template free of
        connections and the template is the database the legacy stack is about to
        open. Then the legacy's own register DAEMON, because the site reads its
        address when it is built. Then gunicorn, then the bridge.
        """
        print(f"copying {self.instances.legacy_database['dbname']} into "
              f"{self.instances.bridge_database['dbname']}")
        DatabaseCopy(self.instances.legacy_database,
                     self.instances.bridge_database).make_copy()
        legacy_environment = self.get_environment(bridge=False)
        self.daemon = SiteDaemonProcess(
            "daemon", self.daemon_command, legacy_environment,
            os.path.join(self.instances.get_instance_path(
                self.instances.legacy_name), "site"))
        self.legacy = StackProcess("legacy", self.legacy_command,
                                   legacy_environment, LEGACY_READY)
        self.bridge = StackProcess("bridge", self.bridge_command,
                                   self.get_environment(bridge=True), BRIDGE_READY)
        self.daemon.clear_register()
        for stack in (self.daemon, self.legacy, self.bridge):
            stack.launch_process()
            stack.wait_serving()

    def stop_stacks(self):    # wf:phase-8:new
        """Down in the reverse order they came up: the daemon outlives its site."""
        for stack in (self.bridge, self.legacy, self.daemon):
            if stack is not None:
                stack.stop()

    def serve(self):    # wf:phase-8:new
        """Ground, stacks, proxy — and the teardown that runs whatever happens."""
        # The output is read while the run is going, from a pipe: block buffering
        # would hold the comparison lines back until the process ended.
        sys.stdout.reconfigure(line_buffering=True)
        self.check_ground()
        try:
            self.start_stacks()
            self.proxy = TwinProxy(self.instances, self.legacy, self.bridge,
                                   self.arguments.port, self.arguments.legacy_port,
                                   self.arguments.bridge_port, self.arguments.run)
            self.proxy.open_comparison()
            signal.signal(signal.SIGTERM, self.on_signal)
            self.proxy.serve()
        except KeyboardInterrupt:
            print("\nstopping")
        finally:
            self.stop_stacks()

    def on_signal(self, number, frame):    # wf:phase-8:new
        """A TERM closes the listener; the finally above does the rest."""
        if self.proxy is not None and self.proxy.server is not None:
            threading.Thread(target=self.proxy.server.shutdown, daemon=True).start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("instance", help="the instance the LEGACY stack serves; "
                                         "the bridge serves it plus _asgi")
    parser.add_argument("--run", required=True,
                        help="the name you give this run; both archives carry it")
    parser.add_argument("-w", "--workers", type=int, default=1,
                        help="gunicorn workers on the legacy stack")
    parser.add_argument("--port", type=int, default=8097,
                        help="where you browse")
    parser.add_argument("--legacy-port", type=int, default=8099)
    parser.add_argument("--bridge-port", type=int, default=8098)
    TwinRun(parser.parse_args()).serve()
