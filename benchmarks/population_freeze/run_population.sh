#!/bin/bash
# Una corsa della prova di popolazione: le gambe in sequenza, mai insieme.
#
#   ./run_population.sh <piano> <prefisso> [ordine]
#
#   piano      il file del piano, con il suo .sha256 accanto
#   prefisso   battezza gli output
#   ordine     l'ordine delle gambe. Default "bridge" — la sola gamba che ha il
#              freeze. "bridge,legacy" fa entrambe, "legacy,bridge" le inverte.
#
# Variabili d'ambiente:
#   POP_MEM_LIMIT     OBBLIGATORIA. Il limite di memoria del container, uguale
#                     alle due gambe. Nessun default: sceglierlo in silenzio
#                     renderebbe la prova un caso invece di una misura.
#   FREEZE_MINUTES    il freeze del bridge. Default 5.
#   WORK_DIR          dove finiscono output e log. Default <scenario>/runs/<prefisso>
#   LAB_DIR           il laboratorio Docker.       Default <scenario>/../docker
#   LEGACY_WORKERS    i worker di Gunicorn.        Default 4
#   MEMORY_THRESHOLD  la soglia del guardiano.     Default 80
#
# Il freeze viene SEMPRE ripristinato all'uscita, anche se la corsa muore a
# meta': la trap e' armata prima che il valore venga toccato, come nella campagna
# precedente, e per la stessa ragione.
set -u

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd "$SCENARIO_DIR/.." && pwd)"
. "$BENCH_DIR/bench_common/lab_lifecycle.sh"

PLAN="${1:?serve il piano}"
PREFIX="${2:?serve il prefisso}"
ORDER="${3:-bridge}"

if [ -z "${POP_MEM_LIMIT:-}" ]; then
  lab_log "STOP: POP_MEM_LIMIT non dichiarata."
  lab_log "  E' il limite di memoria del container, e deve essere lo stesso per le due"
  lab_log "  gambe. Va scelto dal titolare, non da questo script."
  exit 9
fi
export POP_MEM_LIMIT
export FREEZE_MINUTES="${FREEZE_MINUTES:-5}"
export LEGACY_WORKERS="${LEGACY_WORKERS:-4}"
MEMORY_THRESHOLD="${MEMORY_THRESHOLD:-80}"
LAB_DIR="${LAB_DIR:-$SCENARIO_DIR/../docker}"
WORK_DIR="${WORK_DIR:-$SCENARIO_DIR/runs/$PREFIX}"

export LAB_PROJECT=genro-bench-lab
export POP_DIR="$SCENARIO_DIR"

lab_require_tools || exit 9
LAB_DIR="$(cd "$LAB_DIR" && pwd)" || { lab_log "LAB_DIR inesistente"; exit 9; }
export POP_RUNTIME_DIR="$LAB_DIR/runtime"
mkdir -p "$POP_RUNTIME_DIR"
lab_require_free_dir "$WORK_DIR" || exit 9
WORK_DIR="$(cd "$WORK_DIR" && pwd)"
STATUS="$WORK_DIR/run_population_status.txt"

# Il piano NON viene generato qui: lo genera bench_common/make_plans.sh, una
# volta per campagna, contro plans.spec.json.
if [ ! -f "$PLAN" ]; then
  lab_log "STOP: piano assente: $PLAN"
  lab_log "  Generalo una volta per campagna:"
  lab_log "    ../bench_common/make_plans.sh $SCENARIO_DIR"
  exit 6
fi
PLAN="$(cd "$(dirname "$PLAN")" && pwd)/$(basename "$PLAN")"
lab_verify_trace "$(dirname "$PLAN")" "$(basename "$PLAN").sha256" || exit 6
lab_verify_trace "$SCENARIO_DIR/accounts" "load_users.txt.sha256" || exit 6
PLAN_SHA256="$(lab_plan_digest "$PLAN")" || exit 6
lab_log "  piano sha256 $PLAN_SHA256"
lab_wait_for_load 4.0 || exit 4

lab_log "prova di popolazione: piano '$(basename "$PLAN")', prefisso '$PREFIX', ordine '$ORDER'"
lab_log "  memoria $POP_MEM_LIMIT, freeze ${FREEZE_MINUTES} min, gunicorn -w $LEGACY_WORKERS"
lab_log "  lavoro  $WORK_DIR"

lab_arm_cleanup

run_leg () {
  local stack="$1"
  local service base container override out extra
  out="$WORK_DIR/${PREFIX}_${stack}"
  case "$stack" in
    bridge)
      service=bridge; base="http://127.0.0.1:8098"
      container=genro-bench-lab-bridge-1
      override="$SCENARIO_DIR/overrides/override_bridge_population.yaml"
      ;;
    legacy)
      service=legacy; base="http://127.0.0.1:8099"
      container=genro-bench-lab-legacy-1
      override="$SCENARIO_DIR/overrides/override_legacy_population.yaml"
      ;;
    *) lab_log "STOP: stack sconosciuto: $stack"; return 2 ;;
  esac
  export LAB_OVERRIDE="$override"
  export LAB_SERVICE="$service"

  lab_log "=== gamba $stack ==="
  lab_require_same_plan "$PLAN" "$PLAN_SHA256" "$stack" || return 6
  if [ "$stack" = "bridge" ]; then
    lab_orders_paths "$POP_RUNTIME_DIR" "${PREFIX}_${stack}" || return 9
    # Il freeze: si salva il valore precedente, si applica il nuovo, e il
    # ripristino e' garantito dalla trap gia' armata.
    LAB_FREEZE_RESTORE="${GNR_ASGI_IDLE_FREEZE_MINUTES:-}"
    LAB_FREEZE_TOUCHED=1
    export GNR_ASGI_IDLE_FREEZE_MINUTES="$FREEZE_MINUTES"
    export GNR_ASGI_FROZEN_USERS_PATH="/lab/runtime/${PREFIX}_frozen_users"
    lab_log "  freeze a ${FREEZE_MINUTES} min, deposito $GNR_ASGI_FROZEN_USERS_PATH"
  fi
  lab_render_compose "${out}_compose.yaml" || { lab_log "STOP: render fallito"; return 8; }
  lab_stop_others "$service"
  lab_recreate_service "$service" || { lab_log "STOP: il servizio non si e' ricreato"; return 8; }

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
    # Il valore vivo si certifica dall'ambiente del processo, non dal .env: un
    # cambio di variabile non sopravvive a `restart`, e questa e' la prova che
    # la ricreazione lo ha portato dentro.
    LIVE="$(lab_certify_live_env "$container" GNR_ASGI_IDLE_FREEZE_MINUTES)"
    lab_log "  GNR_ASGI_IDLE_FREEZE_MINUTES dentro il container: '${LIVE}'"
    if [ "$LIVE" != "$FREEZE_MINUTES" ]; then
      lab_log "STOP: il freeze non e' arrivato nel container"
      return 8
    fi
    extra="--census $base/_server/inspector/census --journal $LAB_ORDERS_DECISIONS"
    extra="$extra --frozen-users-path $GNR_ASGI_FROZEN_USERS_PATH"
    extra="$extra --expect-freeze-minutes $FREEZE_MINUTES"
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
    extra=""
  fi

  lab_log "  avvio del driver"
  python3 "$SCENARIO_DIR/population_probe.py" --stack "$stack" --run "${PREFIX}_${stack}" \
    --base "$base" --container "$container" --plan "$PLAN" --out "$out" \
    --memory-threshold "$MEMORY_THRESHOLD" --plan-sha256 "$PLAN_SHA256" $extra \
    > "${out}_driver.log" 2>&1 &
  LAB_DRIVER_PID=$!
  export LAB_WRITER_PIDS="$LAB_DRIVER_PID"
  wait "$LAB_DRIVER_PID"
  local rc=$?
  LAB_DRIVER_PID=""
  export LAB_WRITER_PIDS=""
  lab_stop_service "$service"
  lab_log "  gamba $stack: exit $rc"
  tail -4 "${out}_driver.log" | sed 's/^/    /'
  return $rc
}

lab_run_legs "$ORDER" "$STATUS" "$WORK_DIR"
exit $?
