"""Direct checks of the page-class cache certificate, with no Docker and no server.

What must hold, and it is the whole point of this file: the BLOCKING part is only
what makes the two stacks comparable — the DB preference True, the same GenroPy
revision, a load carrying a ``page_id``, no ``_avoid_module_cache`` — and the
entries are only diagnostic. Legacy entries that cannot be observed must NOT stop
the run, and "not observable" must never read as ``entries = 0``.

    python3 test_page_class_cache.py
"""

import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                os.pardir, os.pardir)))

from bench_common import page_class_cache as module                              # noqa: E402
from bench_common.page_class_cache import PageClassCacheCertificate             # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got!r}"
          + ("" if ok else f"  atteso {want!r}"))
    if not ok:
        failures.append(label)


GOOD_FORM = {"method": "app.getSelection", "table": "adm.user",
             "page_id": "AbCdEfGhIjKlMnOpQrStUv", "callcounter": "100"}
ENTRIES_TWO = ("[('AbCdEfGhIjKlMnOpQrStUv', '/lab/projects/x/webpages/invc/customer.py', "
               "'GnrCustomWebPage'), ('WxYzAbCdEfGhIjKlMnOpQr', "
               "'/lab/projects/x/webpages/invc/invoice.py', 'GnrCustomWebPage')]")


class Fake(PageClassCacheCertificate):
    """A certificate whose three readings are decided by the test."""

    def __init__(self, stack="bridge", enabled=True, revision="cad7055ce4f9",
                 entries=("available", {"pool_0001": 2}, "finto", "")):
        super().__init__(stack, "http://finto", container="c", instance="i",
                         genropy_tree="/tree")
        self.enabled = enabled
        self.revision = revision
        self.entries = entries

    def read_configuration(self):
        return self.enabled, "fonte finta", None if self.enabled is not None else "nessuna lettura"

    def read_genropy_revision(self):
        return self.revision, None if self.revision else "tree non leggibile"

    def read_entries(self, session=None):
        return self.entries


print("\n== i nove campi ci sono tutti, e si chiamano cosi' ==")
record = Fake().certify(form=GOOD_FORM)
for field in ("configuration_enabled", "configuration_source", "genropy_revision",
              "requests_carry_page_id", "avoid_module_cache", "entries_status",
              "entries", "entries_source", "entries_note"):
    check(f"campo {field}", field in record, True)

print("\n== la parte bloccante: quattro fatti, e solo quelli ==")
check("con tutto a posto non blocca nulla", Fake().certify(form=GOOD_FORM)["blocking"], [])
check("la preferenza False blocca",
      len(Fake(enabled=False).certify(form=GOOD_FORM)["blocking"]), 1)
check("e lo dice nominando la preferenza",
      "page_class_cache" in Fake(enabled=False).certify(form=GOOD_FORM)["blocking"][0], True)
check("la preferenza non determinabile blocca",
      len(Fake(enabled=None).certify(form=GOOD_FORM)["blocking"]), 1)
check("la revisione illeggibile blocca",
      len(Fake(revision=None).certify(form=GOOD_FORM)["blocking"]), 1)
check("un carico senza page_id blocca",
      len(Fake().certify(form={"method": "x"})["blocking"]), 1)
check("e lo dice nominando page_id",
      "page_id" in Fake().certify(form={"method": "x"})["blocking"][0], True)
check("un carico con _avoid_module_cache blocca",
      len(Fake().certify(form={**GOOD_FORM, "_avoid_module_cache": "1"})["blocking"]), 1)
check("un carico mai ispezionato blocca", len(Fake().certify(form=None)["blocking"]), 1)
check("e in quel caso i due fatti della richiesta non sono ne' veri ne' falsi",
      (Fake().certify(form=None)["requests_carry_page_id"],
       Fake().certify(form=None)["avoid_module_cache"]), (None, None))
check("un page_id vuoto non conta come presente",
      Fake().certify(form={**GOOD_FORM, "page_id": "  "})["requests_carry_page_id"], False)

print("\n== le entry NON bloccano mai ==")
for stato, valore in (("unavailable", None), ("partial", {"pool_0001": None}),
                      ("available", {"pool_0001": 0})):
    record = Fake(entries=(stato, valore, "finto", "una ragione")).certify(form=GOOD_FORM)
    check(f"entries_status {stato} non blocca", record["blocking"], [])
    check(f"  e lo stato resta {stato}", record["entries_status"], stato)

print("\n== 'non osservabili' non e' 'zero' ==")
absent = Fake(entries=("unavailable", None, "finto", "serve il tag _DEV_")).certify(form=GOOD_FORM)
empty = Fake(entries=("available", {"pool_0001": 0}, "finto", "")).certify(form=GOOD_FORM)
check("non osservabili: entries e' None", absent["entries"], None)
check("non osservabili: lo stato lo dice", absent["entries_status"], "unavailable")
check("non osservabili: la ragione c'e'", absent["entries_note"], "serve il tag _DEV_")
check("zero entry: entries e' zero", empty["entries"], {"pool_0001": 0})
check("zero entry: lo stato e' available", empty["entries_status"], "available")
check("i due casi non si confondono",
      absent["entries"] == empty["entries"], False)

print("\n== il legacy senza accesso: entry non osservabili, corsa che continua ==")
record = PageClassCacheCertificate("legacy", "http://finto",
                                   genropy_tree="/tree").certify(form=GOOD_FORM)
check("lo stato delle entry e' unavailable", record["entries_status"], "unavailable")
check("le entry non sono zero", record["entries"], None)
check("la nota nomina il tag che servirebbe", "_DEV_" in record["entries_note"], True)
check("la nota dice che nessun tag e' stato assegnato",
      "Nessun tag" in record["entries_note"], True)
check("la nota dice che una richiesta parlerebbe di un worker su quattro",
      "quattro" in record["entries_note"], True)
check("le entry non compaiono fra i fatti bloccanti",
      any("entr" in reason for reason in record["blocking"]), False)

print("\n== una sessione che rifiuta non produce un falso zero ==")


class Refusing:
    def rpc(self, page, method):
        raise PermissionError("auth_main superadmin,_DEV_")


class Allowed:
    def rpc(self, page, method):
        assert (page, method) == (module.LEGACY_PAGE, module.LEGACY_RPC), (page, method)
        return ENTRIES_TWO


legacy = PageClassCacheCertificate("legacy", "http://finto", genropy_tree="/tree")
status, entries, _, note = legacy.read_entries(Refusing())
check("rifiutata: stato unavailable", status, "unavailable")
check("rifiutata: entries None", entries, None)
check("rifiutata: l'errore e' nella nota", "auth_main" in note, True)
status, entries, _, _ = legacy.read_entries(Allowed())
check("permessa: stato available", status, "available")
check("permessa: due entry contate", entries, {"gunicorn": 2})

print("\n== una console non montata e' una diagnostica assente, non un blocco ==")
bridge = PageClassCacheCertificate("bridge", "http://127.0.0.1:1", genropy_tree="/tree")
status, entries, source, note = bridge.read_entries()
check("una console irraggiungibile da unavailable", status, "unavailable")
check("e non zero", entries, None)
check("con la ragione scritta", bool(note), True)

print("\n== il conteggio delle entry ==")
one = PageClassCacheCertificate("bridge", "http://finto")
check("lista vuota e' zero", one.count_entries("[]"), 0)
check("due entry sono due", one.count_entries(ENTRIES_TWO), 2)
check("una lista vera si conta", one.count_entries([1, 2, 3]), 3)
check("un repr illeggibile non diventa zero", one.count_entries("boh"), None)
check("None non diventa zero", one.count_entries(None), None)

print("\n== il certificato non tocca account, permessi o percorso misurato ==")


def executable_code(path):
    """The module's code with every docstring removed.

    The assertion must look at what the module DOES: its docstring names
    middleware and hook precisely to say that none is added, and a grep on the
    raw text would read that promise as a violation.
    """
    tree = ast.parse(open(path).read())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            body.pop(0)
    return ast.unparse(tree)


source = executable_code(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      os.pardir, "page_class_cache.py"))
for vietato in ("adm.user", "setTag", "insert", "UPDATE", "auth_main =",
                "middleware", "post_worker_init", "gunicorn.conf"):
    check(f"nessun {vietato}", vietato in source, False)
check("nessuna espressione che svuoti la cache", "clear_page_class_cache" in source, False)
check("nessun page_class_cache_enabled, che muta lo stato",
      "page_class_cache_enabled" in source, False)
check("le entry passano dall'API pubblica del loader",
      module.EXPR_ENTRIES.endswith("page_class_cache_entries()"), True)
check("la preferenza si legge con getPreference",
      "getPreference" in source, True)

print("\n" + "=" * 50)
if failures:
    print(f"FALLITI {len(failures)}:")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("tutti i controlli passati")
