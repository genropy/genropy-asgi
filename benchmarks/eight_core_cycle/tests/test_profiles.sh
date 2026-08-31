#!/bin/bash
# L'ordine dei tre profili, con un `docker` finto e un driver finto.
#
# Quello che questo test prova, e che non si puo' provare in Python:
#
#   - l'ORDINE e' legacy_w12 -> bridge_dynamic -> legacy_w16, e il primo profilo
#     che esce non-zero ferma la sequenza: i successivi non partono affatto;
#   - il cleanup gira comunque, anche quando la sequenza si ferma;
#   - prima del profilo che parte l'altro stack viene fermato: mai due insieme;
#   - lo stato su disco dice quale profilo ha fallito e con che codice;
#   - il driver riceve la topologia giusta per ogni profilo, e i worker Gunicorn
#     giusti: dodici e sedici.
#
# COSA NON COPRE, e perche': una sequenza di tre profili SANI fino in fondo. Il
# controllo di prontezza del bridge interroga il census su una porta vera, e senza
# un bridge vivo ritenta per cinque minuti. La sequenza sana la prova
# ../bench_common/tests/test_lab_lifecycle.sh col proprio docker finto, sullo
# stesso lab_run_legs che questo runner usa senza modifiche.
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
cp "$SCENARIO/run_profiles.sh" "$SANDBOX/scenario/run_profiles.sh"
chmod 755 "$SANDBOX/scenario/run_profiles.sh"
# Il runner cerca bench_common accanto allo scenario: un link al vero, cosi' il
# lifecycle sotto prova e' quello condiviso e non una copia.
ln -s "$BENCH/bench_common" "$SANDBOX/bench_common"
mv "$SANDBOX/scenario" "$SANDBOX/scenario_dir"
mkdir -p "$SANDBOX/root"
mv "$SANDBOX/bench_common" "$SANDBOX/root/bench_common"
mv "$SANDBOX/scenario_dir" "$SANDBOX/root/scenario"
SCENARIO_SANDBOX="$SANDBOX/root/scenario"

echo '{"users":1}' > "$SCENARIO_SANDBOX/traces/cycle_plan.json"
( cd "$SCENARIO_SANDBOX/traces" && sha256sum cycle_plan.json > cycle_plan.json.sha256 )
# Il runner rifiuta un piano che non sia quello della corsa valida: qui gli si
# dichiara l'hash del piano finto, cosi' il controllo passa e resta esercitato.
PLAN_SHA256_ATTESO="$(awk '{print $1}' "$SCENARIO_SANDBOX/traces/cycle_plan.json.sha256")"
export PLAN_SHA256_ATTESO

# Il driver finto: registra di essere stato chiamato ed esce col codice chiesto.
cat > "$SCENARIO_SANDBOX/cycle_probe.py" <<'EOF'
import os, sys
def arg(name):
    return sys.argv[sys.argv.index(name) + 1]
run, out = arg("--run"), arg("--out")
profile = run.split("_", 1)[1]
open(os.environ["DRIVER_LOG"], "a").write(
    f"driver {profile} stack={arg('--stack')} topologia={arg('--topology')} "
    f"worker={arg('--expect-workers')}\n")
open(out + "_outcome.json", "w").write('{"run": "%s"}' % run)
sys.exit(int(os.environ.get("DRIVER_EXIT_" + profile.upper(), "0")))
EOF

DRIVER_LOG="$SANDBOX/driver.log"
export DRIVER_LOG
: > "$DRIVER_LOG"

echo "== il primo profilo che fallisce ferma la sequenza =="
export DRIVER_EXIT_LEGACY_W12=3 DRIVER_EXIT_BRIDGE_DYNAMIC=0 DRIVER_EXIT_LEGACY_W16=0
export LAB_DIR="$SANDBOX/lab" WORK_DIR="$SANDBOX/run1"
export PLAN="$SCENARIO_SANDBOX/traces/cycle_plan.json"
export CYCLE_MEM_LIMIT=4g
"$SCENARIO_SANDBOX/run_profiles.sh" legacy_w12,bridge_dynamic,legacy_w16 prova \
  > "$SANDBOX/run1.log" 2>&1
RC=$?
[ "$RC" = "3" ]; report $? "la sequenza propaga il codice 3 (ottenuto $RC)"
grep -q "driver legacy_w12" "$DRIVER_LOG"; report $? "legacy_w12 e' stato eseguito"
grep -q "driver bridge_dynamic" "$DRIVER_LOG"
report $((1 - $?)) "bridge_dynamic NON e' stato eseguito"
grep -q "driver legacy_w16" "$DRIVER_LOG"
report $((1 - $?)) "legacy_w16 NON e' stato eseguito"
grep -q "FERMATA SU legacy_w12" "$SANDBOX/run1/run_profiles_status.txt"
report $? "lo stato nomina il profilo fallito e il codice"
[ -f "$SANDBOX/run1/MANIFEST.sha256" ]; report $? "il manifest e' stato scritto comunque"
grep -q "cleanup" "$SANDBOX/run1.log"; report $? "il cleanup e' stato eseguito comunque"

echo
echo "== il primo profilo riceve topologia fissa e dodici worker =="
grep -q "driver legacy_w12 stack=legacy topologia=fixed worker=12" "$DRIVER_LOG"
report $? "legacy_w12: stack legacy, topologia fixed, dodici worker"

echo
echo "== l'ordine dichiarato e' quello del mandato =="
: > "$DRIVER_LOG"
export DRIVER_EXIT_LEGACY_W12=0 DRIVER_EXIT_BRIDGE_DYNAMIC=3
export WORK_DIR="$SANDBOX/run2"
"$SCENARIO_SANDBOX/run_profiles.sh" legacy_w12,bridge_dynamic,legacy_w16 prova2 \
  > "$SANDBOX/run2.log" 2>&1
grep -q "driver legacy_w12" "$DRIVER_LOG"; report $? "prima legacy_w12"
grep -q "driver legacy_w16" "$DRIVER_LOG"
report $((1 - $?)) "e legacy_w16 non parte dopo un bridge fallito"

echo
echo "== il piano deve essere quello della corsa valida =="
export WORK_DIR="$SANDBOX/run3"
PLAN_SHA256_ATTESO=0000000000000000000000000000000000000000000000000000000000000000 \
  "$SCENARIO_SANDBOX/run_profiles.sh" legacy_w12 prova3 > "$SANDBOX/run3.log" 2>&1
RC=$?
[ "$RC" = "6" ]; report $? "un piano diverso ferma tutto con 6 (ottenuto $RC)"
grep -q "NON e' quello della corsa valida" "$SANDBOX/run3.log"
report $? "e lo dice in chiaro"

echo
echo "== prima del profilo che parte, l'altro stack viene fermato =="
BRIDGE_STOP="$(grep -n "^docker stop genro-bench-lab-bridge-1" "$REGISTRO" | head -1 | cut -d: -f1)"
LEGACY_UP="$(grep -n "up -d --force-recreate --no-deps legacy" "$REGISTRO" | head -1 | cut -d: -f1)"
[ -n "$BRIDGE_STOP" ] && [ -n "$LEGACY_UP" ] && [ "$BRIDGE_STOP" -lt "$LEGACY_UP" ]
report $? "il bridge e' fermato PRIMA che il legacy parta: mai due insieme"

echo
echo "=================================================="
if [ "$FAILED" != "0" ]; then
  echo "FALLITI $FAILED"
  echo "--- registro del docker finto ---"
  sed 's/^/    /' "$REGISTRO" | head -25
  exit 1
fi
echo "tutti i controlli passati"
