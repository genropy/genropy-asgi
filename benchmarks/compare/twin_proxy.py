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

**One shadow per browser.** The bridge-side connection, cookie jar and page-id
map are per-browser things, so the proxy keeps one of each per browser: two
windows logged in as two users are two connections on the legacy stack, and a
single jar would make them one on the bridge. Which browser a request came from
is said by the proxy's OWN cookie, `bench_twin`, minted on first sight and
stripped from everything sent onward. The site's session cookie cannot serve:
its value changes at login, because the connection rotates.

**Identifiers.** The bridge mints its own page ids, so the same `IdentityMap` the
replica uses rewrites the tokens of the legacy request into the tokens the bridge
minted, in the query string and in the body alike. Cookies are never forwarded to
the bridge: the browser's jar belongs to the legacy stack, and this proxy keeps a
jar of its own for the shadow. Every OTHER request header is forwarded as it
came — the user agent among them, because the site writes it into the connection
register item, and a missing one would read as a divergence of the stack.

**The bridge's placement ceiling.** `--max-users-per-worker` reaches the recipe
through `GNR_ASGI_WORKER_MAX_USERS`, which `spa/config.py` passes to the group as
`worker_max_users`. Left out, the core's default governs and one worker takes
everybody: two browsers land in the same process and the cross-worker paths —
the register population, the stores, the datachanges between users — are never
exercised. With `1` each user gets a worker of his own. The birth happens inside
the placement since genro-asgi 2682ad7, so the second user waits the moment a
fork costs instead of being answered 503.

**Debug, one flag for two stacks.** Genropy keeps two things apart that the
bridge used to weld together: `site.debug` — the SQL time counters, `pageModule`
in the page's bootstrap — and the werkzeug debugger, an error page that evaluates
Python in the process. On the legacy the first comes from the merged siteconfig
and the second only from `serveprod --debug`; on the bridge they now have a
switch each, `debug` and `debugger` (2026-08-26). So this proxy runs both stacks
with debug ON and no debugger, which is the pair that makes the SQL counters
carry real numbers on both sides, and `--fulldebug` adds the debugger to both.
The legacy cannot be talked out of its configured debug from a command line, so
the bridge follows it rather than the other way round.

**The two rules inherited from Phase 7, both binding.** The exchanges before the
first RPC are not compared, each stack finishing its lazy build there and the
bridge doing it in the template whose lines are dropped by construction. And the
`+28%` is not a pending divergence: it was measured with an instrument that could
not attribute a call, so anything of it that reappears here is a new measurement
with attribution, not a continuation of the old figure.

**One call, one verdict, and nothing stops.** Every request carries a mark of its
own — `twin-00042` — which reaches BOTH archives, so the unit of comparison is the
single call: it either agrees on the two stacks or it diverges, on its own merits,
whatever the calls around it did. A divergence is therefore a finding about that
call and no reason to arrest the run: `twin-00043` is asked the same question with
no assumption that one follows from the other (owner, 2026-08-26, replacing the
first-divergence arrest the replica works under — that rule fits a linear replay of
one recorded session, not a live session of several independent users, where the
divergence of one must not end the session of the others).

Each divergence is printed, and written to a numbered file of its own beside the
archives, naming the call and the browser. Both stacks stay up throughout, which
is what makes a divergence investigable while it is fresh; the closing summary
lists every call that diverged.

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
import uuid
import signal
import subprocess
import sys
import threading
import time

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.dirname(BENCH_DIR)
BENCH_ROOT = os.path.dirname(BENCH)
sys.path.insert(0, BENCH)

from gnr.app.pathresolver import (EntityNotFoundException,  # noqa: E402
                                  PathResolver)
from gnr.core.gnrbag import Bag                      # noqa: E402

from genropy_parity_check import GenropyParity       # noqa: E402
from http_recorder import STATIC_CONTENT_TYPES       # noqa: E402
from replica import (EXCHANGE_POLL_SECONDS, EXCHANGE_WAIT_SECONDS,  # noqa: E402
                     REPLICA_HEADER, IdentityMap, ReplicaClient, TraceReader)
from run_archive import (ARCHIVE_DIR_ENV, DEFAULT_ARCHIVE_DIR,  # noqa: E402
                         RUN_NAME_ENV)
from structural_diff import (DeclaredRules, ReferenceRace,  # noqa: E402
                             ReplyShape, ServiceWarmup, StaleConnection,
                             StructuralDiff)

# This proxy's own ordinal on the legacy leg. The exchange id is minted inside
# the stack, so the sender needs a mark of its own to find the line it caused.
TWIN_HEADER = "X-Bench-Twin-Request"

# The suffix that names the bridge's twin of an instance (owner, 2026-08-25).
BRIDGE_SUFFIX = "_asgi"

# The proxy's own cookie, and the only thing that says which browser a request
# came from. The site's session cookie cannot do it: its value CHANGES at login,
# because the connection rotates — measured 2026-08-26, where the page load after
# the login read as a second browser. This one is minted once per cookie jar and
# never changes, which is also exactly the granularity that matters: two users
# need two jars, since two windows of one profile share the site's cookie and the
# legacy stack cannot tell them apart either. It is stripped from everything sent
# onward, so neither site ever sees a cookie the bench invented.
TWIN_COOKIE = "bench_twin"

# The two weights a divergence carries (owner, 2026-08-26). What the browser
# receives is what the bridge must reproduce, so a difference in the REPLY — its
# status or its body, piggybacked datachanges included — is an ERROR. The register
# calls are how the two stacks got there, and a difference in them is a WARNING:
# real, worth a report, and not a failure of the emulation the browser sees.
ERROR = "ERROR"
WARNING = "WARNING"

# The bridge's daemon override engages only when genropy is told which provider
# to use (genropy #1070). Unset, `gnr.web.daemon.siteregister_client` stays
# genropy's own, the bench's recording client cannot even be built on top of it —
# it binds commands the classic client reaches through `__getattr__` — and the
# bridge template dies importing its engine factory. So the run refuses instead:
# this is a condition of the run, declared by whoever starts it.
DAEMON_PROVIDER_ENV = "GNR_DAEMON_PROVIDER"
DAEMON_PROVIDER = "genropy-asgi"

# How many users one worker of the bridge may hold. The recipe reads it here and
# passes it to the group; unset, the core's own default governs and one worker
# takes everybody, so the pool never grows and the cross-worker paths never run.
WORKER_MAX_USERS_ENV = "GNR_ASGI_WORKER_MAX_USERS"

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
        """The db an instance will actually open, read as the site reads it.

        Genropy's own merge, not the instance's file alone: the `db` node can
        come from the default of the gnr folder — which is where a deployment
        usually keeps the connection and its password — and a copy made on the
        name found in the wrong place would copy the wrong database.
        """
        return dict(self.resolver.get_instanceconfig(name).getAttr("db"))

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


class BrowserShadow:
    """The bridge-side twin of ONE browser, and the thread that keeps it.

    Everything a browser has of its own lives here: the connection to the bridge,
    the cookie jar, the page ids, the two archive readers, the comparison, and the
    thread that runs them. Two users must share NOTHING of the measuring
    apparatus, or a difference between them could be the bench's and not the
    stacks' — and testing table subscriptions and datachanges is exactly testing
    what one user's action does to another (owner, 2026-08-26).

    The thread is not only isolation. SQLite refuses a connection used from a
    thread other than the one that opened it, so the readers must be built here;
    and the two legs of one request stay sequential because this browser's queue
    is served one leg at a time, while OTHER browsers run in parallel — which is
    what browsers actually do, and what delivery between users needs in order to
    be observed at all.
    """

    def __init__(self, proxy, identity):
        self.proxy = proxy
        self.identity = identity
        self.client = ShadowClient("127.0.0.1", proxy.bridge_port)
        self.identity_map = IdentityMap()
        self.legs = queue.Queue()
        self.reference = None
        self.shadow = None
        self.diff = None
        threading.Thread(target=self.serve_legs, daemon=True).start()

    def serve_legs(self):    # wf:phase-8:new
        """Build this browser's own view of the two runs, then follow it."""
        self.reference = TraceReader(self.proxy.legacy.archive_path)
        self.shadow = TraceReader(self.proxy.bridge.archive_path)
        self.diff = StructuralDiff(self.reference, self.shadow, self.proxy.rules)
        while True:
            leg = self.legs.get()
            try:
                self.proxy.follow(self, leg)
            except Exception as failure:
                self.proxy.record_divergence(
                    leg, ERROR, f"the shadow leg of {leg.method} {leg.target} "
                                f"failed: {failure!r}")
            finally:
                leg.done.set()

    def follow_leg(self, leg):    # wf:phase-8:new
        """Hand this browser's thread one leg, and wait for it to be done."""
        self.legs.put(leg)
        leg.done.wait()


class ShadowLeg:
    """One request waiting for its shadow leg, and the answer that it is done."""

    def __init__(self, method, target, headers, body, twin, status, reply_headers,
                 reply_body=b"", identity=""):
        self.identity = identity
        self.reply_body = reply_body
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
        self.rules = rules or DeclaredRules(
            [ReferenceRace(), StaleConnection(), ServiceWarmup()])
        self.shadows = {}
        self.shadows_lock = threading.Lock()
        self.ordinal_lock = threading.Lock()
        self.verdict_lock = threading.Lock()
        self.ordinal = 0
        self.dispatched = 0
        self.verdicts = []
        self.divergences = 0
        # the cold start, decided as the session goes instead of by rescanning:
        # once the first RPC has been served, nothing after it is cold any more.
        # Global on purpose: it is a fact about the two PROCESSES finishing their
        # lazy build, not about any one browser.
        self.rpc_served = False
        self.server = None

    def open_comparison(self):    # wf:phase-8:new
        """Both stacks are up: say which two runs are being compared, then serve."""
        print(StructuralDiff(TraceReader(self.legacy.archive_path),
                             TraceReader(self.bridge.archive_path),
                             self.rules).header)
        print(f"\ndeclared run: {self.run_name}")
        print(f"browse http://127.0.0.1:{self.port} — the legacy answers you, "
              f"the bridge shadows every request")
        print("every request carries a mark of its own; a divergence names it "
              "and the run carries on")
        print("ERROR = the reply the browser would have seen; "
              "WARNING = the register calls behind it")

    def get_shadow(self, identity):    # wf:phase-8:new
        """The shadow of that browser, made — with its thread — on first sight."""
        with self.shadows_lock:
            shadow = self.shadows.get(identity)
            if shadow is None:
                shadow = BrowserShadow(self, identity)
                self.shadows[identity] = shadow
                print(f"  a browser appears — shadow #{len(self.shadows)}")
            return shadow

    def get_identity(self, headers):    # wf:phase-8:new
        """The browser's mark, minted here the first time and never changing.

        Returns the mark and whether it was minted now, because a mark nobody has
        yet has to travel back in a `Set-Cookie` or the next request mints another.
        """
        for morsel in (headers.get("Cookie") or "").split(";"):
            name, _, value = morsel.strip().partition("=")
            if name == TWIN_COOKIE:
                return value, False
        return uuid.uuid4().hex[:16], True

    def get_browser_headers(self, headers):    # wf:phase-8:new
        """The browser's headers with the bench's own cookie taken back out.

        Neither site ever sees it: the proxy's mark is the proxy's business, and a
        cookie the bench invented has no place in a recorded exchange.
        """
        cookies = [morsel.strip() for morsel in (headers.get("Cookie") or "").split(";")
                   if morsel.strip() and not morsel.strip().startswith(f"{TWIN_COOKIE}=")]
        cleaned = {key: value for key, value in headers.items()
                   if key.lower() != "cookie"}
        if cookies:
            cleaned["Cookie"] = "; ".join(cookies)
        return cleaned

    @property
    def report_dir(self):    # wf:phase-8:new
        return os.environ.get(ARCHIVE_DIR_ENV) or DEFAULT_ARCHIVE_DIR

    @property
    def run_slug(self):    # wf:phase-8:new
        """The declared name as a filename: what the reports are called."""
        return re.sub(r"[^A-Za-z0-9_-]+", "-", self.run_name).strip("-")

    def get_report_path(self, number, severity):    # wf:phase-8:new
        """One file per divergence, numbered and weighed: a finding of its own."""
        return os.path.join(self.report_dir,
                            f"{self.run_slug}-{number:02d}-{severity.lower()}.txt")

    def get_next_twin(self):    # wf:phase-8:new
        """The mark this proxy puts on the legacy leg of the next request.

        It is the id of ONE call, and the unit the whole comparison is written in:
        the same mark reaches both archives, so `twin-00042` either agrees on the
        two stacks or diverges, on its own, whatever the calls around it did.
        """
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
        identity, minted = self.get_identity(headers)
        headers = self.get_browser_headers(headers)
        twin = self.get_next_twin()
        status, reply_headers, reply_body = self.send_legacy(
            method, target, headers, body, twin)
        if minted:
            reply_headers = list(reply_headers) + [
                ("Set-Cookie", f"{TWIN_COOKIE}={identity}; Path=/; SameSite=Lax")]
        with self.verdict_lock:
            self.dispatched += 1
        # The shadow leg runs on this browser's own thread and this one waits for
        # it: the two legs of one request in sequence, browsers in parallel.
        # Whatever happens over there is a divergence to report, never a failure
        # the browser is told about.
        self.get_shadow(identity).follow_leg(
            ShadowLeg(method, target, headers, body, twin,
                      status, reply_headers, reply_body, identity))
        return status, reply_headers, reply_body

    def follow(self, shadow, leg):    # wf:phase-8:new
        """The shadow leg and its comparison, on this browser's own thread.

        A STATIC is settled first, and before any archive is read. It is dispatched
        like everything else — the bridge must see the traffic the legacy sees — but
        it is not compared, so it needs no reference line, and asking for one would
        be worse than useless: the recorder writes a static as a stub carrying its
        exchange id alone, with no request headers to find it by. Measured on the
        first browser session, 2026-08-25: every static waited out the archive
        timeout, one after another, and the browser stalled.
        """
        if self.is_static(leg.target, leg.reply_headers):
            shadow.client.replaying = None
            shadow_status, _body = self.send_shadow(shadow, leg)
            self.say(leg, f"{leg.method} {leg.target.split('?')[0]}",
                     f"{leg.status}/{shadow_status}  static, not compared")
            return
        reference = self.get_marked_exchange(shadow.reference, TWIN_HEADER, leg.twin)
        if reference is None:
            self.record_divergence(
                leg, WARNING,
                f"the legacy archive carries no exchange marked {leg.twin}: "
                f"this call has no reference for its register calls")
            shadow.client.replaying = None
            shadow_status, shadow_body = self.send_shadow(shadow, leg)
            self.compare_reply(leg, f"{leg.method} {leg.target.split('?')[0]}",
                               shadow_status, shadow_body)
            return
        shadow.client.replaying = reference["exchange_id"]
        shadow_status, shadow_body = self.send_shadow(shadow, leg)
        shadow.identity_map.learn_page_id(reference.get("resp_body"),
                                          shadow_body.decode("utf-8", "replace"))
        label = self.get_label(reference, leg.method, leg.target)
        self.compare_reply(leg, label, shadow_status, shadow_body)
        self.compare_register(shadow, leg, reference, label, shadow_status)

    def compare_reply(self, leg, label, shadow_status, shadow_body):    # wf:phase-8:new
        """What the BROWSER would have seen on each stack — the ERROR half.

        The status and the whole body, the second masked of the identifiers each
        stack mints and of the clock. Nothing else is allowed to differ: the reply
        is what the bridge exists to reproduce, and everything the server pushes
        to the client rides inside it.

        It runs on every exchange, cold start included: the register rule that
        excuses those exchanges is about how each stack builds itself, and says
        nothing about what it answered.
        """
        if shadow_status != leg.status:
            self.record_divergence(
                leg, ERROR, f"{label}: the legacy answered {leg.status}, "
                            f"the bridge {shadow_status}")
            return
        difference = ReplyShape(leg.reply_body.decode("utf-8", "replace")).get_difference(
            ReplyShape(shadow_body.decode("utf-8", "replace")))
        if difference is not None:
            self.record_divergence(leg, ERROR, f"{label}: {difference}")

    def send_shadow(self, shadow, leg):    # wf:phase-8:new
        """The request the browser just made, sent to the bridge in its turn."""
        return shadow.client.send_shadow(
            leg.method, shadow.identity_map.get_adapted(leg.target), leg.headers,
            self.get_adapted_body(shadow, leg.body))

    def get_adapted_body(self, shadow, body):    # wf:phase-8:new
        """The body with the legacy's identifiers rewritten into the bridge's.

        Decoded as latin-1 and encoded back: the tokens are ASCII, and every
        other byte of the request survives the round trip untouched.
        """
        if not body:
            return body
        return shadow.identity_map.get_adapted(
            body.decode("latin-1")).encode("latin-1")

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

    def compare_register(self, shadow, leg, reference, label, shadow_status):    # wf:phase-8:new
        """How each stack reached that reply — the WARNING half.

        One call, one verdict, and nothing stops: a call that diverges is a
        finding about THAT call, and `twin-00043` is asked the same question
        `twin-00042` was with no assumption that one follows from the other
        (owner, 2026-08-26).
        """
        if self.is_cold_start(reference):
            self.say(leg, label, f"{leg.status}/{shadow_status}  "
                                 f"before the first RPC, not compared")
            return
        replayed = self.get_marked_exchange(shadow.shadow, REPLICA_HEADER,
                                            reference["exchange_id"])
        if replayed is None:
            self.record_divergence(
                leg, WARNING, f"{label}: the bridge archive carries no exchange "
                              f"stamped with {reference['exchange_id']}")
            return
        reference_lines = len(shadow.reference.get_register_lines(
            reference["exchange_id"]))
        shadow_lines = len(shadow.shadow.get_register_lines(replayed["exchange_id"]))
        measure = (f"{leg.status}/{shadow_status}  "
                   f"reg {reference_lines}/{shadow_lines}  "
                   f"{self.get_timing(reference)}/{self.get_timing(replayed)} ms")
        self.say(leg, label, measure)
        if shadow_status != leg.status:
            # the reply half already reported it; two stacks that answered
            # differently did not reach the same place by different roads.
            return
        divergence = shadow.diff.get_divergence(reference, replayed, self.ordinal)
        if divergence is not None:
            self.record_divergence(leg, WARNING, divergence.report)
        else:
            self.record_verdict(leg, label, measure, "agree")

    def say(self, leg, label, measure):    # wf:phase-8:new
        """One line per call, opening on the mark the whole comparison is written in."""
        print(f"  {leg.twin}  {label}  {measure}", flush=True)

    def record_verdict(self, leg, label, measure, verdict):    # wf:phase-8:new
        """What this call turned out to be, kept for the closing table."""
        with self.verdict_lock:
            self.verdicts.append((leg.twin, label, measure, verdict))

    def record_divergence(self, leg, severity, report):    # wf:phase-8:new
        """Write this divergence down, name the call and its weight, and carry on.

        Nothing stops and nothing is torn down. The unit of comparison is the
        single call, so a divergence is a finding about that call and says nothing
        about the next one; and with several users the divergence of one must not
        end the session of the others.
        """
        with self.verdict_lock:
            self.divergences += 1
            number = self.divergences
            self.verdicts.append((leg.twin, leg.target, "", severity))
        text = (f"{severity} #{number} — run {self.run_name}\n"
                f"call: {leg.twin}  {leg.method} {leg.target}\n"
                f"browser: shadow of {leg.identity}\n"
                f"legacy archive: {self.legacy.archive_path}\n"
                f"bridge archive: {self.bridge.archive_path}\n"
                f"{self.dispatched} request(s) dispatched so far\n\n"
                f"{report}\n")
        path = self.get_report_path(number, severity)
        with open(path, "w") as report_file:
            report_file.write(text)
        print(f"\n{text}\nwritten to {path}\nthe run carries on.\n", flush=True)

    def get_timing(self, record):    # wf:phase-8:new
        """The response time the recorder measured inside the stack that served it."""
        duration = record.get("duration_ms")
        return "-" if duration is None else f"{duration:.0f}"

    @property
    def summary(self):    # wf:phase-8:new
        """What the session came to: every call judged, and the ones that diverged."""
        errors = [row for row in self.verdicts if row[3] == ERROR]
        warnings = [row for row in self.verdicts if row[3] == WARNING]
        lines = [f"run {self.run_name}: {self.dispatched} request(s) through "
                 f"{len(self.shadows)} browser(s), "
                 f"{len(errors)} ERROR, {len(warnings)} WARNING",
                 "  ERROR = what the browser would have seen: the status or the "
                 "reply body, identifiers and clock masked",
                 "  WARNING = how the two stacks got there: the register calls"]
        for twin, label, _measure, severity in errors + warnings:
            lines.append(f"  {severity:<7} {twin}  {label}")
        lines.append(f"legacy archive: {self.legacy.archive_path}")
        lines.append(f"bridge archive: {self.bridge.archive_path}")
        return "\n".join(lines)

    def serve(self):    # wf:phase-8:new
        """Listen until something stops us; the finally is the only teardown."""
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port),
                                                      TwinHandler)
        self.server.proxy = self
        self.server.daemon_threads = True
        try:
            self.server.serve_forever()
        finally:
            print(f"\n{self.summary}", flush=True)
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
        if bridge and self.arguments.max_users_per_worker:
            environment[WORKER_MAX_USERS_ENV] = str(self.arguments.max_users_per_worker)
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
        """Gunicorn's own command line, plus the debug the run declares.

        WITHOUT `--debug` the site reads `wsgi?debug` out of its merged
        siteconfig, which the bench's own configuration sets to True: the SQL
        time counters carry real numbers and no werkzeug debugger is wrapped
        around anything. WITH it, `serveprod` also wraps the site in that
        debugger — the same pair `--fulldebug` gives the bridge, which is why one
        flag governs both stacks.
        """
        command = [LEGACY_PYTHON, SERVE_LEGACY, self.instances.legacy_name,
                   "-b", f"127.0.0.1:{self.arguments.legacy_port}",
                   "-w", str(self.arguments.workers), "-k", "gthread",
                   "--threads", "16", "-c", GUNICORN_RECORDERS]
        return command + ["--debug"] if self.arguments.fulldebug else command

    @property
    def bridge_command(self):    # wf:phase-8:new
        """The bridge's, with NO `--nodebug`: unset means debug, as the recipe reads it.

        That is what puts the two stacks on the same footing — the legacy takes
        its debug from the configuration and cannot be talked out of it from a
        command line, so the bridge follows rather than the other way round.
        """
        command = [sys.executable, SERVE_BRIDGE, self.instances.bridge_name,
                   "-p", str(self.arguments.bridge_port)]
        return command + ["--fulldebug"] if self.arguments.fulldebug else command

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
    parser.add_argument("--fulldebug", action="store_true",
                        help="add the werkzeug debugger to both stacks; without "
                             "it both run with debug and no debugger, which is "
                             "what makes their SQL counters comparable")
    parser.add_argument("--max-users-per-worker", type=int,
                        help="the bridge's placement ceiling: with 1 each user "
                             "lands on a worker of his own, which is what "
                             "exercises the cross-worker paths")
    parser.add_argument("--port", type=int, default=8097,
                        help="where you browse")
    parser.add_argument("--legacy-port", type=int, default=8099)
    parser.add_argument("--bridge-port", type=int, default=8098)
    TwinRun(parser.parse_args()).serve()
