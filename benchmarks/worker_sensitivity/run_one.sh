#!/bin/bash
# Una corsa della sensitivity. $1=nome (W1|W2|W4|W8) $2=worker attesi $3=utenti per worker
#
# Variabili d'ambiente:
#   WORK_DIR  dove finiscono log, campioni e reliquie. Default: <scenario>/runs
#   LAB_DIR   il laboratorio Docker.            Default: <scenario>/../docker
# Lo script funziona da qualunque directory corrente.
set -u

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SENSITIVITY_DIR="$SCENARIO_DIR"
WORK_DIR="${WORK_DIR:-$SCENARIO_DIR/runs}"
LAB_DIR="${LAB_DIR:-$SCENARIO_DIR/../docker}"

mkdir -p "$WORK_DIR" || exit 1
LAB_DIR="$(cd "$LAB_DIR" && pwd)" || { echo "LAB_DIR inesistente: $LAB_DIR" >&2; exit 1; }
WORK_DIR="$(cd "$WORK_DIR" && pwd)"

N=$1; EW=$2; EPW=$3
OVERRIDE="$SCENARIO_DIR/overrides/override_$N.yaml"
[ -f "$OVERRIDE" ] || { echo "override assente: $OVERRIDE" >&2; exit 1; }

echo "### CORSA $N: $EW worker attesi, $EPW utenti ciascuno"
echo "  scenario $SCENARIO_DIR"
echo "  lavoro   $WORK_DIR"
echo "  lab      $LAB_DIR"

# La traccia si verifica PRIMA di toccare il laboratorio: una traccia corrotta
# non deve spostare i log precedenti ne' ricreare il bridge. Il controllo sta in
# un if esplicito, non appeso a `set -e`, e non passa da una pipe: con la pipe
# l'exit code sarebbe quello di sed.
if [ ! -f "$SCENARIO_DIR/traces/worker_trace.sha256" ]; then
  echo "FALLITA $N: manifest della traccia assente: $SCENARIO_DIR/traces/worker_trace.sha256" >&2
  exit 6
fi
if ! ( cd "$SCENARIO_DIR/traces" && sha256sum -c worker_trace.sha256 ); then
  echo "FALLITA $N: la traccia worker_trace.jsonl non corrisponde al suo hash" >&2
  exit 6
fi
echo "  traccia worker_trace.jsonl: hash verificato"

# Il load deve scendere sotto 4 entro dieci minuti, altrimenti la corsa non parte.
load_ok=0
for i in $(seq 1 60); do
  L=$(cut -d' ' -f1 /proc/loadavg)
  if [ "$(echo "$L < 4.0" | bc)" = "1" ]; then echo "  load $L: si parte"; load_ok=1; break; fi
  echo "  load $L troppo alto, attendo"; sleep 10
done
if [ "$load_ok" != "1" ]; then
  echo "FALLITA $N: il load non e' sceso sotto 4.0 entro il timeout" >&2
  exit 4
fi

mv "$LAB_DIR/runtime/orders.log" "$WORK_DIR/orders_before_$N.log" 2>/dev/null
mv "$LAB_DIR/runtime/orders.decisions.jsonl" "$WORK_DIR/orders_before_$N.decisions.jsonl" 2>/dev/null

export SENSITIVITY_RUNTIME_DIR="$LAB_DIR/runtime"
docker compose -p genro-bench-lab --project-directory "$LAB_DIR" --env-file "$LAB_DIR/.env" \
  -f "$LAB_DIR/compose.yaml" -f "$OVERRIDE" up -d --force-recreate --no-deps bridge >/dev/null 2>&1

# Il census deve mostrare tutti i worker running entro cinque minuti.
python3 - <<'PY'
import json, sys, time, urllib.request
for i in range(150):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8098/_server/inspector/census', timeout=5) as r:
            site = json.load(r)['site']; w = site['groups']['pool']['workers']
            if w and all(x['state'] == 'running' for x in w.values()):
                print(f"  bridge pronto: {list(w)}, utenti {len(site['user_map'])}")
                time.sleep(20)
                sys.exit(0)
    except Exception: pass
    time.sleep(2)
print("  census: nessun worker running entro il timeout", file=sys.stderr)
sys.exit(1)
PY
if [ $? -ne 0 ]; then
  echo "FALLITA $N: il bridge non ha mostrato worker running entro il timeout" >&2
  exit 5
fi


docker exec genro-bench-lab-bridge-1 cat /sys/fs/cgroup/memory.events > "$WORK_DIR/memory_events_before_$N.txt"
python3 "$SCENARIO_DIR/worker_probe.py" --config "$N" --base http://127.0.0.1:8098 \
  --container genro-bench-lab-bridge-1 --census http://127.0.0.1:8098/_server/inspector/census \
  --journal "$LAB_DIR/runtime/orders.decisions.jsonl" --trace "$SCENARIO_DIR/traces/worker_trace.jsonl" \
  --out "$WORK_DIR/$N" --expect-workers "$EW" --expect-per-worker "$EPW"
rc=$?
docker exec genro-bench-lab-bridge-1 cat /sys/fs/cgroup/memory.events > "$WORK_DIR/memory_events_after_$N.txt"
curl -s --max-time 15 http://127.0.0.1:8098/_server/inspector/census -o "$WORK_DIR/${N}_census_post.json"
curl -s --max-time 15 http://127.0.0.1:8098/_orchestration/status -o "$WORK_DIR/${N}_status_post.json"
cp "$LAB_DIR/runtime/orders.decisions.jsonl" "$WORK_DIR/${N}_journal.jsonl" 2>/dev/null
cp "$LAB_DIR/runtime/orders.log" "$WORK_DIR/${N}_orders.log" 2>/dev/null
exit $rc
