#!/bin/bash
# Il ciclo a otto core: i due stack in sequenza, sullo stesso boot della stessa
# macchina, con lo stesso piano di richieste.
#
#   ./run_cycle.sh [ordine] [prefisso]
#
#   ordine     l'ordine dei due stack, separato da virgola. Default "legacy,bridge".
#              "bridge,legacy" funziona senza toccare una riga di codice.
#   prefisso   battezza gli output. Default "e8c".
#
# Variabili d'ambiente:
#   WORK_DIR          dove finiscono output e log.  Default <scenario>/runs/<prefisso>
#   LAB_DIR           il laboratorio Docker.        Default <scenario>/../docker
#   PLAN              il piano delle richieste.     Default traces/cycle_plan.json
#   CYCLE_MEM_LIMIT   il limite di memoria, uguale ai due stack. Default 4g
#   CYCLE_CPUS        i core per stack.             Default 8 (solo per il log)
#   LEGACY_WORKERS    i worker di Gunicorn.         Default 8
#   GNR_ASGI_WORKER_MAX_USERS   gli utenti per worker bridge. Default 15
#   EXPECT_WORKERS    i worker attesi.              Default 8
#   MEMORY_THRESHOLD  la soglia del guardiano.      Default 80
#
# Un'esecuzione che finisce male ferma la sequenza: proseguire su un laboratorio
# storto produce misure senza valore.
set -u

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd "$SCENARIO_DIR/.." && pwd)"
. "$BENCH_DIR/bench_common/lab_lifecycle.sh"

ORDER="${1:-legacy,bridge}"
PREFIX="${2:-e8c}"
LAB_DIR="${LAB_DIR:-$SCENARIO_DIR/../docker}"
WORK_DIR="${WORK_DIR:-$SCENARIO_DIR/runs/$PREFIX}"
PLAN="${PLAN:-$SCENARIO_DIR/traces/cycle_plan.json}"
export CYCLE_MEM_LIMIT="${CYCLE_MEM_LIMIT:-4g}"
export LEGACY_WORKERS="${LEGACY_WORKERS:-8}"
export GNR_ASGI_WORKER_MAX_USERS="${GNR_ASGI_WORKER_MAX_USERS:-15}"
EXPECT_WORKERS="${EXPECT_WORKERS:-8}"
CYCLE_CPUS="${CYCLE_CPUS:-8}"
MEMORY_THRESHOLD="${MEMORY_THRESHOLD:-80}"

export LAB_PROJECT=genro-bench-lab
export CYCLE_DIR="$SCENARIO_DIR"

lab_require_tools || exit 9
LAB_DIR="$(cd "$LAB_DIR" && pwd)" || { lab_log "LAB_DIR inesistente"; exit 9; }
# Il tree GenroPy montato: la sua revisione e' un fatto bloccante della
# certificazione, e i due stack devono montare lo stesso.
GENROPY_TREE="$(sed -n 's/^GENROPY_TREE=//p' "$LAB_DIR/.env" | tail -1)"
[ -n "$GENROPY_TREE" ] || { lab_log "STOP: GENROPY_TREE assente da $LAB_DIR/.env"; exit 9; }
lab_log "  genropy $GENROPY_TREE"
export CYCLE_RUNTIME_DIR="$LAB_DIR/runtime"
mkdir -p "$CYCLE_RUNTIME_DIR"
lab_require_free_dir "$WORK_DIR" || exit 9
WORK_DIR="$(cd "$WORK_DIR" && pwd)"
STATUS="$WORK_DIR/run_cycle_status.txt"

lab_log "ciclo a otto core: ordine '$ORDER', prefisso '$PREFIX'"
lab_log "  piano   $PLAN"
lab_log "  lavoro  $WORK_DIR"
lab_log "  lab     $LAB_DIR"
lab_log "  ${CYCLE_CPUS} cpu, memoria $CYCLE_MEM_LIMIT, soglia ${MEMORY_THRESHOLD}%"
lab_log "  bridge: $EXPECT_WORKERS worker da $GNR_ASGI_WORKER_MAX_USERS utenti"
lab_log "  legacy: gunicorn -w $LEGACY_WORKERS -k gthread --threads 16"

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
# Il digest si prende UNA VOLTA, e ogni esecuzione lo ricontrolla: i due stack
# devono leggere lo stesso file, byte per byte.
PLAN_SHA256="$(lab_plan_digest "$PLAN")" || exit 6
lab_log "  piano sha256 $PLAN_SHA256"
lab_wait_for_load 4.0 || exit 4

lab_arm_cleanup

run_leg () {
  # run_leg <stack> — un'esecuzione: ricrea il servizio, attende, misura, chiude.
  local stack="$1"
  local service base container expect_workers expect_per_worker override extra instance
  case "$stack" in
    bridge)
      service=bridge; base="http://127.0.0.1:8098"
      container=genro-bench-lab-bridge-1
      expect_workers="$EXPECT_WORKERS"; expect_per_worker="$GNR_ASGI_WORKER_MAX_USERS"
      instance=bridge_lab
      override="$SCENARIO_DIR/overrides/override_bridge_8c.yaml"
      ;;
    legacy)
      service=legacy; base="http://127.0.0.1:8099"
      container=genro-bench-lab-legacy-1
      # Il legacy non ha un pool: gli otto worker sono quelli di Gunicorn e li
      # certifica il campionatore, non il census.
      expect_workers="$LEGACY_WORKERS"; expect_per_worker=0
      instance=legacy_lab
      override="$SCENARIO_DIR/overrides/override_legacy_g8.yaml"
      ;;
    *) lab_log "STOP: stack sconosciuto: $stack"; return 2 ;;
  esac
  export LAB_OVERRIDE="$override"
  export LAB_SERVICE="$service"
  local out="$WORK_DIR/${PREFIX}_${stack}"

  lab_log "=== esecuzione su $stack ==="
  lab_require_same_plan "$PLAN" "$PLAN_SHA256" "$stack" || return 6
  if [ "$stack" = "bridge" ]; then
    # Il journal nasce col nome finale: nessun mv su un file che il bridge tiene
    # aperto. Il path si decide QUI, prima che il container esista, e i due
    # namespace — host e container — li separa lab_orders_paths.
    lab_orders_paths "$CYCLE_RUNTIME_DIR" "${PREFIX}_${stack}" || return 9
  fi
  lab_render_compose "${out}_compose.yaml" || { lab_log "STOP: render fallito"; return 8; }
  lab_stop_others "$service"
  lab_recreate_service "$service" || { lab_log "STOP: il servizio non si e' ricreato"; return 8; }

  # Attesa dell'avvio. L'asimmetria fra i due stack e' voluta e va detta:
  # sul bridge un GET / conia un guest, e un guest occupa uno slot di
  # worker_max_users falsando la distribuzione, quindi si attende il census;
  # sul legacy non esiste quella contabilita' e un GET / e' innocuo.
  #
  # DIFFERENZA DAL CONFRONTO L120: qui la crescita per CPU e' spenta, quindi il
  # pool nasce VUOTO e nessun worker esiste prima del primo placement. Si attende
  # che il census risponda, non che mostri worker running.
  if [ "$stack" = "bridge" ]; then
    python3 - "$base" <<'PY' || { lab_log "STOP: il bridge non ha risposto al census"; return 5; }
import json, sys, time, urllib.request
base = sys.argv[1]
for _ in range(150):
    try:
        with urllib.request.urlopen(base + "/_server/inspector/census", timeout=5) as answer:
            site = json.load(answer)["site"]
            pool = site["groups"]["pool"]
            print(f"  bridge pronto: pool vuoto atteso, worker {list(pool['workers'])}, "
                  f"utenti {len(site['user_map'])}")
            time.sleep(20)
            sys.exit(0)
    except Exception:
        pass
    time.sleep(2)
print("  census: nessuna risposta entro il timeout", file=sys.stderr)
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
  python3 "$SCENARIO_DIR/cycle_probe.py" --stack "$stack" --run "${PREFIX}_${stack}" \
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
  lab_log "  $stack: exit $rc"
  tail -4 "${out}_driver.log" | sed 's/^/    /'
  return $rc
}

lab_run_legs "$ORDER" "$STATUS" "$WORK_DIR"
exit $?
