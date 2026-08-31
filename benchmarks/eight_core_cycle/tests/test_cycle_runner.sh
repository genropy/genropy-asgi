#!/bin/bash
# Il verdetto del runner, con un `docker` finto e un driver finto.
#
# Quello che questo test prova, e che non si puo' provare in Python:
#
#   - un'esecuzione che esce non-zero FERMA la sequenza, e il secondo stack non
#     parte affatto: e' la garanzia che una prova completa non segua uno smoke
#     fallito;
#   - il cleanup gira comunque, anche quando la sequenza si ferma;
#   - prima dello stack che parte l'altro viene fermato: i due non girano insieme;
#   - lo stato scritto su disco dice quale stack ha fallito e con che codice.
#
# COSA NON COPRE, e perche': la sequenza di due esecuzioni SANE e l'ordine
# inverso. Il controllo di prontezza del bridge interroga il census su una porta
# vera, e senza un bridge vivo ritenta per cinque minuti: un test non deve
# aspettare cinque minuti per sapere una cosa che
# ../bench_common/tests/test_lab_lifecycle.sh gia' prova col proprio docker
# finto, sullo stesso lab_run_legs che questo runner usa senza modifiche.
#
# Il `docker` finto appende ogni invocazione a un registro, cosi' "l'altro stack
# e' stato fermato" e' un fatto letto dal registro, non un'affermazione. Nessun
# container, nessuna immagine, nessuna rete.
#
#   bash tests/test_cycle_runner.sh
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCENARIO="$(cd "$HERE/.." && pwd)"
BENCH="$(cd "$SCENARIO/.." && pwd)"
FAILED=0

# Il runner esige /proc/loadavg prima di toccare il laboratorio: attendere che la
# macchina sia scarica e' una precondizione della misura, non un dettaglio. Quel
# file esiste solo su Linux, che e' dove il laboratorio gira. Su macOS il test
# dichiara di non potersi eseguire invece di provare un runner mutilato.
if [ ! -r /proc/loadavg ]; then
  echo "SALTATO: serve /proc/loadavg, che esiste solo su Linux."
  echo "  Il laboratorio gira su Linux: esegui questo test la'."
  exit 0
fi

SANDBOX="$(mktemp -d)"

report () {
  # report <codice> <etichetta>
  if [ "$1" = "0" ]; then
    echo "  [ok  ] $2"
  else
    echo "  [FAIL] $2"
    FAILED=$((FAILED + 1))
  fi
}

cleanup () { rm -rf "$SANDBOX"; }
trap cleanup EXIT

# ---------------------------------------------------------------- il finto lab
mkdir -p "$SANDBOX/bin" "$SANDBOX/lab" "$SANDBOX/scenario/overrides" "$SANDBOX/scenario/traces"
REGISTRO="$SANDBOX/docker.log"
: > "$REGISTRO"

# Il finto dichiara che ENTRAMBI gli stack sono in piedi: e' la condizione in cui
# lab_stop_others ha qualcosa da fermare, ed e' quella che il test deve provare.
# Con un `docker ps` vuoto non ci sarebbe niente da fermare e l'assenza dello stop
# non direbbe nulla.
cat > "$SANDBOX/bin/docker" <<EOF
#!/bin/bash
echo "docker \$*" >> "$REGISTRO"
case "\$1 \$2" in
  "compose config") echo "services: {}" ;;
  "ps --format") printf '%s\\n' genro-bench-lab-bridge-1 genro-bench-lab-legacy-1 ;;
esac
exit 0
EOF
cat > "$SANDBOX/bin/curl" <<EOF
#!/bin/bash
echo "curl \$*" >> "$REGISTRO"
echo -n "200"
exit 0
EOF
chmod 755 "$SANDBOX/bin/docker" "$SANDBOX/bin/curl"
export PATH="$SANDBOX/bin:$PATH"

echo "GENROPY_TREE=/finto/genropy" > "$SANDBOX/lab/.env"
echo "services: {}" > "$SANDBOX/lab/compose.yaml"
cp "$SCENARIO/overrides/"*.yaml "$SANDBOX/scenario/overrides/"
cp "$SCENARIO/run_cycle.sh" "$SANDBOX/scenario/run_cycle.sh"
chmod 755 "$SANDBOX/scenario/run_cycle.sh"
# Il runner cerca bench_common accanto allo scenario: un link al vero, cosi' il
# lifecycle sotto prova e' quello condiviso e non una copia.
ln -s "$BENCH/bench_common" "$SANDBOX/bench_common"
mv "$SANDBOX/scenario" "$SANDBOX/scenario_dir"
mkdir -p "$SANDBOX/root"
mv "$SANDBOX/bench_common" "$SANDBOX/root/bench_common"
mv "$SANDBOX/scenario_dir" "$SANDBOX/root/scenario"
SCENARIO_SANDBOX="$SANDBOX/root/scenario"

echo '{"users":1}' > "$SCENARIO_SANDBOX/traces/cycle_smoke_plan.json"
( cd "$SCENARIO_SANDBOX/traces" && shasum -a 256 cycle_smoke_plan.json \
  > cycle_smoke_plan.json.sha256 2>/dev/null \
  || sha256sum cycle_smoke_plan.json > cycle_smoke_plan.json.sha256 )

# Il driver finto: registra di essere stato chiamato ed esce col codice chiesto.
cat > "$SCENARIO_SANDBOX/cycle_probe.py" <<'EOF'
import os, sys
stack = sys.argv[sys.argv.index("--stack") + 1]
out = sys.argv[sys.argv.index("--out") + 1]
open(os.environ["DRIVER_LOG"], "a").write(f"driver {stack}\n")
open(out + "_outcome.json", "w").write('{"stack": "%s"}' % stack)
sys.exit(int(os.environ.get("DRIVER_EXIT_" + stack.upper(), "0")))
EOF

DRIVER_LOG="$SANDBOX/driver.log"
export DRIVER_LOG
: > "$DRIVER_LOG"

echo "== uno smoke incompleto esce non-zero e il secondo stack non parte =="
export DRIVER_EXIT_LEGACY=3 DRIVER_EXIT_BRIDGE=0
export LAB_DIR="$SANDBOX/lab" WORK_DIR="$SANDBOX/run1"
export PLAN="$SCENARIO_SANDBOX/traces/cycle_smoke_plan.json"
export GNR_ASGI_WORKER_MAX_USERS=2 LEGACY_WORKERS=8 EXPECT_WORKERS=8
"$SCENARIO_SANDBOX/run_cycle.sh" legacy,bridge prova > "$SANDBOX/run1.log" 2>&1
RC=$?
[ "$RC" = "3" ]; report $? "la sequenza propaga il codice 3 del driver (ottenuto $RC)"
grep -q "driver legacy" "$DRIVER_LOG"; report $? "il legacy e' stato eseguito"
grep -q "driver bridge" "$DRIVER_LOG"; report $((1 - $?)) "il bridge NON e' stato eseguito"
grep -q "FERMATA SU legacy" "$SANDBOX/run1/run_cycle_status.txt"
report $? "lo stato su disco nomina lo stack fallito e il codice"
[ -f "$SANDBOX/run1/MANIFEST.sha256" ]; report $? "il manifest e' stato scritto comunque"
grep -q "cleanup" "$SANDBOX/run1.log"; report $? "il cleanup e' stato eseguito comunque"

echo
echo "== prima dello stack che parte, l'altro viene fermato =="
# lab_stop_others ferma il container per nome (docker stop), lab_stop_service
# ferma il servizio via compose: due comandi diversi, due righe diverse.
BRIDGE_STOP="$(grep -n "^docker stop genro-bench-lab-bridge-1" "$REGISTRO" | head -1 | cut -d: -f1)"
LEGACY_UP="$(grep -n "up -d --force-recreate --no-deps legacy" "$REGISTRO" | head -1 | cut -d: -f1)"
[ -n "$BRIDGE_STOP" ]; report $? "il bridge e' stato fermato per nome"
[ -n "$BRIDGE_STOP" ] && [ -n "$LEGACY_UP" ] && [ "$BRIDGE_STOP" -lt "$LEGACY_UP" ]
report $? "e lo e' stato PRIMA che il legacy partisse: i due non girano insieme"
grep -q "compose .*stop legacy" "$REGISTRO"
report $? "a esecuzione conclusa si ferma anche il legacy"

echo
echo "=================================================="
if [ "$FAILED" != "0" ]; then
  echo "FALLITI $FAILED"
  echo "--- registro del docker finto ---"
  sed 's/^/    /' "$REGISTRO" | head -20
  exit 1
fi
echo "tutti i controlli passati"
