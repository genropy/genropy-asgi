# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Certifying the page-class cache from outside, without touching any source.

WHAT IS BLOCKING, and it is only what makes the two stacks comparable:

- ``configuration_enabled`` — the preference ``sys.experimental.page_class_cache``
  really is True. It is a row of ``adm.preference``, read by a service process in
  each container: the two stacks have their own database, so reading each one's is
  the only way a divergence would show. False, or not determinable, stops the run.
- ``genropy_revision`` — both legs mount the same GenroPy tree at the same
  revision. Read on the host from the mounted path.
- ``requests_carry_page_id`` — the load carries a ``page_id``. A request without
  one is never cached, so a run built on such requests could not exercise the
  cache at all.
- ``avoid_module_cache`` — the load does not carry ``_avoid_module_cache``, which
  would bypass the cache request by request.

The last two are facts of the driver's OWN request, checked on the form it builds:
no server is asked, and nothing is added to the measured path.

WHAT IS ONLY DIAGNOSTIC: the entries actually in the cache. On the bridge they can
be read from the worker. On the legacy the only door GenroPy ships is
``sys/page_class_cache``, gated ``superadmin,_DEV_``: if the bench account does not
already carry one of those tags the certificate records
``entries_status: unavailable`` with the reason, and THE RUN GOES ON. No tag is
granted, no account is created, no endpoint, middleware, hook or wrapper is added.

``entries_status`` exists precisely so that "not observable" is never confused
with ``entries = 0``. And no hit is ever simulated: GenroPy keeps no hit counter
to read, so a hit rate is not among the things this certificate can claim.

One more thing it does not claim: on the legacy a single request would speak for
one Gunicorn worker out of four, so even an available reading would not certify
the other three.
"""

import json
import subprocess
import urllib.error
import urllib.request

PREFERENCE_PATH = "experimental.page_class_cache"
PREFERENCE_PKG = "sys"
# L'espressione delle entry, valutata nel worker del bridge. Nello scope
# dell'eval del worker esiste un solo nome, `worker`.
EXPR_ENTRIES = "worker.gnr_site.resource_loader.page_class_cache_entries()"
# La pagina che GenroPy porta gia': l'unica porta per le entry del legacy.
LEGACY_PAGE = "/sys/page_class_cache"
LEGACY_RPC = "getCacheEntries"
# Il campo che il carico deve portare, e quello che non deve portare.
PAGE_ID_FIELD = "page_id"
BYPASS_FIELD = "_avoid_module_cache"


class BlockingGap(RuntimeError):
    """A blocking fact could not be established: the two stacks are not comparable."""


class PageClassCacheCertificate:
    """The certificate of one stack, taken outside the measured window."""

    def __init__(self, stack, base, container=None, instance=None,
                 genropy_tree=None, console_path="/_console"):
        self.stack = stack
        self.base = base
        self.container = container
        self.instance = instance
        self.genropy_tree = genropy_tree
        self.console_path = console_path

    # ------------------------------------------------------- parte bloccante
    def read_configuration(self):
        """The preference, from a service process inside this stack's container.

        The same row the live process reads. Returns (enabled, source, error):
        ``enabled`` is None when the reading did not happen, which is blocking.
        """
        source = f"processo di servizio in {self.container}: GnrWsgiSite.getPreference"
        if not (self.container and self.instance):
            return None, source, "container o instance non dichiarati"
        script = (
            "from gnr.web.gnrwsgisite import GnrWsgiSite; "
            f"site = GnrWsgiSite({self.instance!r}); "
            f"print(repr(site.getPreference({PREFERENCE_PATH!r}, "
            f"pkg={PREFERENCE_PKG!r})))")
        try:
            done = subprocess.run(["docker", "exec", self.container, "python", "-c", script],
                                  capture_output=True, text=True, timeout=300)
        except Exception as failure:                             # noqa: BLE001
            return None, source, repr(failure)[:200]
        if done.returncode != 0:
            return None, source, (done.stderr or "").strip()[-300:]
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        return self.truthy(lines[-1] if lines else None), source, None

    def read_genropy_revision(self):
        """The revision of the mounted GenroPy tree, read on the host."""
        if not self.genropy_tree:
            return None, "tree GenroPy non dichiarato"
        try:
            done = subprocess.run(["git", "-C", self.genropy_tree, "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=60)
        except Exception as failure:                             # noqa: BLE001
            return None, repr(failure)[:200]
        if done.returncode != 0:
            return None, (done.stderr or "").strip()[-200:]
        return done.stdout.strip(), None

    def inspect_form(self, form):
        """What the load's own request says: page_id present, bypass absent."""
        carries = bool(str(form.get(PAGE_ID_FIELD) or "").strip())
        bypass = BYPASS_FIELD in form
        return carries, bypass

    # ------------------------------------------------------ parte diagnostica
    def read_entries(self, session=None):
        """The entries in the live cache. Returns (status, entries, source, note)."""
        if self.stack == "bridge":
            return self.read_entries_from_console()
        return self.read_entries_from_page(session)

    def read_entries_from_console(self):
        source = f"console eval su {self.base}{self.console_path}, processo worker"
        console = BridgeConsole(self.base, self.console_path)
        try:
            targets = console.worker_targets
        except ConsoleUnavailable as failure:
            return "unavailable", None, source, str(failure)
        except Exception as failure:                             # noqa: BLE001
            return "unavailable", None, source, repr(failure)[:200]
        entries = {}
        for target in targets:
            try:
                entries[target] = self.count_entries(console.evaluate(target, EXPR_ENTRIES))
            except Exception as failure:                         # noqa: BLE001
                entries[target] = None
                return "partial", entries, source, f"{target}: {repr(failure)[:150]}"
        if not entries:
            return "unavailable", None, source, "la console non ha nominato worker"
        return "available", entries, source, ""

    def read_entries_from_page(self, session):
        source = f"{LEGACY_PAGE}, RPC {LEGACY_RPC}"
        note = ("richiede un account con tag superadmin o _DEV_. Nessun tag e' "
                "stato assegnato e nessun account creato: se l'accesso non c'e' "
                "gia', le entry restano non osservabili. Una singola richiesta "
                "parlerebbe comunque di un solo worker Gunicorn su quattro.")
        if session is None:
            return "unavailable", None, source, note
        try:
            return "available", {"gunicorn": self.count_entries(
                session.rpc(LEGACY_PAGE, LEGACY_RPC))}, source, ""
        except Exception as failure:                             # noqa: BLE001
            return "unavailable", None, source, f"{note} Errore: {repr(failure)[:200]}"

    # ------------------------------------------------------------- il verdetto
    def certify(self, form=None, session=None):
        """The certificate. Never raises: a blocking gap is a recorded gap."""
        enabled, configuration_source, configuration_error = self.read_configuration()
        revision, revision_error = self.read_genropy_revision()
        carries, bypass = self.inspect_form(form or {})
        status, entries, entries_source, entries_note = self.read_entries(session)
        blocking = []
        if enabled is not True:
            blocking.append(f"la preferenza {PREFERENCE_PKG}.{PREFERENCE_PATH} "
                            f"non e' certificata True: {enabled!r}"
                            + (f" ({configuration_error})" if configuration_error else ""))
        if not revision:
            blocking.append(f"la revisione del tree GenroPy non e' leggibile: "
                            f"{revision_error}")
        if form is None:
            blocking.append("il carico non e' stato ispezionato: nessuna richiesta esaminata")
        elif not carries:
            blocking.append(f"il carico non porta {PAGE_ID_FIELD}: la cache non "
                            f"potrebbe entrare in gioco")
        if bypass:
            blocking.append(f"il carico porta {BYPASS_FIELD}: bypassa la cache")
        return {
            "stack": self.stack,
            "configuration_enabled": enabled,
            "configuration_source": configuration_source,
            "configuration_error": configuration_error,
            "genropy_revision": revision,
            "genropy_tree": self.genropy_tree,
            "genropy_revision_error": revision_error,
            "requests_carry_page_id": carries if form is not None else None,
            "avoid_module_cache": bypass if form is not None else None,
            "entries_status": status,
            "entries": entries,
            "entries_source": entries_source,
            "entries_note": entries_note,
            "blocking": blocking,
            "note": ("le entry sono diagnostiche e non fermano la prova; "
                     "entries_status distingue 'non osservabili' da entries = 0; "
                     "GenroPy non tiene contatori di hit, e nessun hit e' simulato"),
        }

    # ---------------------------------------------------------------- utilita'
    def count_entries(self, repr_text):
        """How many entries a repr of a list describes, or None if unreadable."""
        if isinstance(repr_text, list):
            return len(repr_text)
        if not isinstance(repr_text, str):
            return None
        stripped = repr_text.strip()
        if stripped in ("[]", "()"):
            return 0
        if not stripped.startswith("["):
            return None
        return stripped.count("(")

    def truthy(self, repr_text):
        """Whether a repr means True. None when it means nothing readable."""
        if repr_text is None:
            return None
        text = str(repr_text).strip()
        if text in ("True", "1", "'1'", '"1"'):
            return True
        if text in ("False", "None", "0", "''", '""'):
            return False
        return None


class ConsoleUnavailable(RuntimeError):
    """The console is not mounted: the diagnostic reading is not obtainable."""


class BridgeConsole:
    """The core's MCP console, used only to read the diagnostic entries.

    The console exists when the recipe mounts it, which happens when
    ``GNR_ASGI_CONSOLE`` is set. Mounting IS the gate: unset, the door does not
    exist and this class says so instead of guessing.
    """

    def __init__(self, base, path="/_console", timeout=30):
        self.base = base
        self.path = path
        self.timeout = timeout
        self.sequence = 0

    def call(self, tool, arguments):
        """One JSON-RPC tools/call. Returns the structured content."""
        self.sequence += 1
        envelope = {"jsonrpc": "2.0", "id": self.sequence, "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments}}
        request = urllib.request.Request(
            self.base + self.path, data=json.dumps(envelope).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "Accept": "application/json, text/event-stream"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as answer:
                payload = json.loads(answer.read())
        except urllib.error.HTTPError as failure:
            if failure.code == 404:
                raise ConsoleUnavailable(
                    f"la console non e' montata su {self.base}{self.path}: "
                    f"serve GNR_ASGI_CONSOLE nell'ambiente del bridge") from failure
            raise
        if "error" in payload:
            raise RuntimeError(f"la console ha rifiutato {tool}: {payload['error']}")
        result = payload.get("result") or {}
        return result.get("structuredContent", result)

    @property
    def worker_targets(self):
        """Only the workers: the commander hosts no site, so it has no cache."""
        answer = self.call("targets", {})
        for value in answer.values():
            if isinstance(value, list):
                return [name for name in value if name != "commander"]
        return []

    def evaluate(self, target, expression):
        """One expression in one worker. Returns its repr, as the console gives it."""
        answer = self.call("eval", {"target": target, "expr": expression})
        if isinstance(answer, dict):
            return answer.get("repr", answer)
        return answer
