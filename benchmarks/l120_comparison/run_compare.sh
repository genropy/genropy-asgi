#!/bin/bash
# Il confronto L120: le due gambe in sequenza, sullo stesso boot della stessa
# macchina, con lo stesso piano di richieste.
#
#   ./run_compare.sh [ordine] [prefisso]
#
#   ordine     l'ordine delle gambe, separato da virgola. Default "legacy,bridge".
#              "bridge,legacy" funziona senza toccare una riga di codice.
#   prefisso   battezza gli output. Default "l120".
#
# Variabili d'ambiente:
#   WORK_DIR         dove finiscono output e log.   Default <scenario>/runs/<prefisso>
#   LAB_DIR          il laboratorio Docker.         Default <scenario>/../docker
#   PLAN             il piano delle richieste.      Default traces/l120_plan.json
#   L120_MEM_LIMIT   il limite di memoria, uguale alle due gambe. Default 2g
#   LEGACY_WORKERS   i worker di Gunicorn.          Default 4
#   MEMORY_THRESHOLD la soglia del guardiano.       Default 80
#
# Una gamba che finisce male ferma la sequenza: proseguire su un laboratorio gia'
# storto produce misure senza valore.
set -u

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd "$SCENARIO_DIR/.." && pwd)"
. "$BENCH_DIR/bench_common/lab_lifecycle.sh"

ORDER="${1:-legacy,bridge}"
PREFIX="${2:-l120}"
LAB_DIR="${LAB_DIR:-$SCENARIO_DIR/../docker}"
WORK_DIR="${WORK_DIR:-$SCENARIO_DIR/runs/$PREFIX}"
PLAN="${PLAN:-$SCENARIO_DIR/traces/l120_plan.json}"
export L120_MEM_LIMIT="${L120_MEM_LIMIT:-2g}"
export LEGACY_WORKERS="${LEGACY_WORKERS:-4}"
MEMORY_THRESHOLD="${MEMORY_THRESHOLD:-80}"

export LAB_PROJECT=genro-bench-lab
export L120_DIR="$SCENARIO_DIR"

lab_require_tools || exit 9
LAB_DIR="$(cd "$LAB_DIR" && pwd)" || { lab_log "LAB_DIR inesistente"; exit 9; }
# Il tree GenroPy montato: la sua revisione e' un fatto bloccante della
# certificazione, e le due gambe devono montare lo stesso.
GENROPY_TREE="$(sed -n 's/^GENROPY_TREE=//p' "$LAB_DIR/.env" | tail -1)"
[ -n "$GENROPY_TREE" ] || { lab_log "STOP: GENROPY_TREE assente da $LAB_DIR/.env"; exit 9; }
lab_log "  genropy $GENROPY_TREE"
export L120_RUNTIME_DIR="$LAB_DIR/runtime"
mkdir -p "$L120_RUNTIME_DIR"
lab_require_free_dir "$WORK_DIR" || exit 9
WORK_DIR="$(cd "$WORK_DIR" && pwd)"
STATUS="$WORK_DIR/run_compare_status.txt"

lab_log "confronto L120: ordine '$ORDER', prefisso '$PREFIX'"
lab_log "  piano   $PLAN"
lab_log "  lavoro  $WORK_DIR"
lab_log "  lab     $LAB_DIR"
lab_log "  memoria $L120_MEM_LIMIT, gunicorn -w $LEGACY_WORKERS, soglia ${MEMORY_THRESHOLD}%"

# Il piano si verifica PRIMA di toccare il laboratorio. NON viene generato qui:
# lo genera bench_common/make_plans.sh, una volta per campagna, contro
# plans.spec.json. Se manca, la corsa si ferma e dice come ottenerlo.
if [ ! -f "$PLAN" ]; then
  lab_log "STOP: piano assente: $PLAN"
  lab_log "  Generalo una volta per campagna:"
  lab_log "    ../bench_common/make_plans.sh $SCENARIO_DIR"
  exit 6
fi
lab_verify_trace "$(dirname "$PLAN")" "$(basename "$PLAN").sha256" || exit 6
# Il digest si prende UNA VOLTA, e ogni gamba lo ricontrolla: le due gambe
# devono leggere lo stesso file, byte per byte.
PLAN_SHA256="$(lab_plan_digest "$PLAN")" || exit 6
lab_log "  piano sha256 $PLAN_SHA256"
lab_wait_for_load 4.0 || exit 4

lab_arm_cleanup

run_leg () {
  # run_leg <stack> — una gamba: ricrea il servizio, attende, misura, chiude.
  local stack="$1"
  local service base container expect_workers expect_per_worker override extra instance
  case "$stack" in
    bridge)
      service=bridge; base="http://127.0.0.1:8098"
      container=genro-bench-lab-bridge-1
      expect_workers=4; expect_per_worker=12
      instance=bridge_lab
      override="$SCENARIO_DIR/overrides/override_bridge_w4.yaml"
      ;;
    legacy)
      service=legacy; base="http://127.0.0.1:8099"
      container=genro-bench-lab-legacy-1
      # Il legacy non ha un pool: i quattro worker sono quelli di Gunicorn e li
      # certifica il campionatore, non il census.
      expect_workers="$LEGACY_WORKERS"; expect_per_worker=0
      instance=legacy_lab
      override="$SCENARIO_DIR/overrides/override_legacy_g4.yaml"
      ;;
    *) lab_log "STOP: stack sconosciuto: $stack"; return 2 ;;
  esac
  export LAB_OVERRIDE="$override"
  export LAB_SERVICE="$service"
  local out="$WORK_DIR/${PREFIX}_${stack}"

  lab_log "=== gamba $stack ==="
  lab_require_same_plan "$PLAN" "$PLAN_SHA256" "$stack" || return 6
  if [ "$stack" = "bridge" ]; then
    # Il journal nasce col nome finale: nessun mv su un file che il bridge tiene
    # aperto. Il path si decide QUI, prima che il container esista.
    lab_orders_paths "$L120_RUNTIME_DIR" "${PREFIX}_${stack}" || return 9
  fi
  lab_render_compose "${out}_compose.yaml" || { lab_log "STOP: render fallito"; return 8; }
  lab_stop_others "$service"
  lab_recreate_service "$service" || { lab_log "STOP: il servizio non si e' ricreato"; return 8; }

  # Attesa dell'avvio. L'asimmetria fra le due gambe e' voluta e va detta:
  # sul bridge un GET / conia un guest, e un guest occupa uno slot di
  # worker_max_users falsando la distribuzione, quindi si attende il census;
  # sul legacy non esiste quella contabilita' e un GET / e' innocuo.
  if [ "$stack" = "bridge" ]; then
    python3 - "$base" <<'PY' || { lab_log "STOP: il bridge non ha mostrato worker running"; return 5; }
import json, sys, time, urllib.request
base = sys.argv[1]
for _ in range(150):
    try:
        with urllib.request.urlopen(base + "/_server/inspector/census", timeout=5) as answer:
            site = json.load(answer)["site"]
            workers = site["groups"]["pool"]["workers"]
            if workers and all(w["state"] == "running" for w in workers.values()):
                print(f"  bridge pronto: {list(workers)}, utenti {len(site['user_map'])}")
                time.sleep(20)
                sys.exit(0)
    except Exception:
        pass
    time.sleep(2)
print("  census: nessun worker running entro il timeout", file=sys.stderr)
sys.exit(1)
PY
  else
    local ready=0 i
    for i in $(seq 1 150); do
      if curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$base/" 2>/dev/null | grep -q '^200$'; then
        lab_log "  legacy pronto dopo $((i * 2))s"; ready=1; break
      fi
      sleep 2
    done
    [ "$ready" = "1" ] || { lab_log "STOP: il legacy non ha risposto 200"; return 5; }
    sleep 20
  fi

  extra=""
  if [ "$stack" = "bridge" ]; then
    extra="--census $base/_server/inspector/census --journal $LAB_ORDERS_DECISIONS"
  fi
  lab_log "  avvio del driver"
  python3 "$SCENARIO_DIR/compare_probe.py" --stack "$stack" --run "${PREFIX}_${stack}" \
    --base "$base" --container "$container" --plan "$PLAN" --out "$out" \
    --expect-workers "$expect_workers" --expect-per-worker "$expect_per_worker" \
    --memory-threshold "$MEMORY_THRESHOLD" --plan-sha256 "$PLAN_SHA256" \
    --instance "$instance" --genropy-tree "$GENROPY_TREE" $extra \
    > "${out}_driver.log" 2>&1 &
  LAB_DRIVER_PID=$!
  export LAB_WRITER_PIDS="$LAB_DRIVER_PID"
  wait "$LAB_DRIVER_PID"
  local rc=$?
  LAB_DRIVER_PID=""
  export LAB_WRITER_PIDS=""
  lab_stop_service "$service"
  lab_log "  gamba $stack: exit $rc"
  tail -3 "${out}_driver.log" | sed 's/^/    /'
  return $rc
}

lab_run_legs "$ORDER" "$STATUS" "$WORK_DIR"
exit $?
