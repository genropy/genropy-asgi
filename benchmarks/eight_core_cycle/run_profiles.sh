#!/bin/bash
# Tre configurazioni in sequenza, sullo stesso piano della prova valida.
#
#   ./run_profiles.sh [ordine] [prefisso]
#
#   ordine     i profili, separati da virgola.
#              Default "legacy_w12,bridge_dynamic,legacy_w16".
#   prefisso   battezza gli output. Default "e8cp".
#
# I profili:
#   legacy_w12       Gunicorn -w 12 -k gthread --threads 16
#   bridge_dynamic   il pool con la policy REALE: decide da se' quanti worker
#   legacy_w16       Gunicorn -w 16 -k gthread --threads 16
#
# Le configurazioni gia' misurate — legacy a 8 processi e bridge a 8 worker fissi
# — NON si ripetono: i loro dati sono la corsa `e8cv5`, sigillata.
#
# Tutto il resto e' identico a quella corsa: otto core, quattro gibibyte, gli
# stessi 120 utenti, LO STESSO PIANO byte per byte, la stessa politica di login,
# la stessa fase full_warmup, le stesse due guardie. Cambia una cosa per volta.
#
# Variabili d'ambiente:
#   WORK_DIR          dove finiscono output e log.  Default <scenario>/runs/<prefisso>
#   LAB_DIR           il laboratorio Docker.        Default <scenario>/../docker
#   PLAN              il piano.                     Default traces/cycle_plan.json
#   PLAN_SHA256_ATTESO l'hash della corsa valida: obbligatorio, blocca se diverso
#   CYCLE_MEM_LIMIT   il limite di memoria.         Default 4g
#   MEMORY_THRESHOLD  la soglia del guardiano.      Default 80
#
# Un profilo che finisce male ferma la sequenza: i successivi non partono.
set -u

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd "$SCENARIO_DIR/.." && pwd)"
. "$BENCH_DIR/bench_common/lab_lifecycle.sh"

ORDER="${1:-legacy_w12,bridge_dynamic,legacy_w16}"
PREFIX="${2:-e8cp}"
LAB_DIR="${LAB_DIR:-$SCENARIO_DIR/../docker}"
WORK_DIR="${WORK_DIR:-$SCENARIO_DIR/runs/$PREFIX}"
PLAN="${PLAN:-$SCENARIO_DIR/traces/cycle_plan.json}"
export CYCLE_MEM_LIMIT="${CYCLE_MEM_LIMIT:-4g}"
MEMORY_THRESHOLD="${MEMORY_THRESHOLD:-80}"
# L'hash del piano della corsa valida e8cv5. Dichiararlo qui rende impossibile
# misurare un workload diverso credendo di confrontarsi con quella corsa.
PLAN_SHA256_ATTESO="${PLAN_SHA256_ATTESO:-376ea43df7141f77d631d97126a38bd0ef8fa58427b930b3704d966458d96bdd}"

export LAB_PROJECT=genro-bench-lab
export CYCLE_DIR="$SCENARIO_DIR"

lab_require_tools || exit 9
LAB_DIR="$(cd "$LAB_DIR" && pwd)" || { lab_log "LAB_DIR inesistente"; exit 9; }
GENROPY_TREE="$(sed -n 's/^GENROPY_TREE=//p' "$LAB_DIR/.env" | tail -1)"
[ -n "$GENROPY_TREE" ] || { lab_log "STOP: GENROPY_TREE assente da $LAB_DIR/.env"; exit 9; }
export CYCLE_RUNTIME_DIR="$LAB_DIR/runtime"
mkdir -p "$CYCLE_RUNTIME_DIR"
lab_require_free_dir "$WORK_DIR" || exit 9
WORK_DIR="$(cd "$WORK_DIR" && pwd)"
STATUS="$WORK_DIR/run_profiles_status.txt"

lab_log "tre profili in sequenza: '$ORDER', prefisso '$PREFIX'"
lab_log "  genropy $GENROPY_TREE"
lab_log "  piano   $PLAN"
lab_log "  lavoro  $WORK_DIR"
lab_log "  8 cpu, memoria $CYCLE_MEM_LIMIT, soglia ${MEMORY_THRESHOLD}%"

if [ ! -f "$PLAN" ]; then
  lab_log "STOP: piano assente: $PLAN"
  lab_log "  Generalo una volta per campagna:"
  lab_log "    ../bench_common/make_plans.sh $SCENARIO_DIR"
  exit 6
fi
lab_verify_trace "$(dirname "$PLAN")" "$(basename "$PLAN").sha256" || exit 6
PLAN_SHA256="$(lab_plan_digest "$PLAN")" || exit 6
if [ "$PLAN_SHA256" != "$PLAN_SHA256_ATTESO" ]; then
  lab_log "STOP: il piano NON e' quello della corsa valida e8cv5."
  lab_log "  atteso   $PLAN_SHA256_ATTESO"
  lab_log "  ottenuto $PLAN_SHA256"
  lab_log "  Un workload diverso non si confronta con quella corsa."
  exit 6
fi
lab_log "  piano sha256 $PLAN_SHA256 — lo stesso di e8cv5"
lab_wait_for_load 8.0 || exit 4

lab_arm_cleanup

certify_live () {
  # certify_live <profilo> <servizio> <container> <processi attesi>
  # La configurazione VIVA, letta dal container, prima di misurare. Blocca.
  local profile="$1" service="$2" container="$3" expected="$4"
  local out="$WORK_DIR/${PREFIX}_${profile}_live_config.txt"
  local problems=0
  {
    echo "profilo: $profile"
    echo "container: $container"
    echo "--- limiti visti dal kernel del container ---"
    docker exec "$container" sh -c 'cat /sys/fs/cgroup/cpu.max; cat /sys/fs/cgroup/memory.max'
    echo "--- contatori di pressione iniziali ---"
    docker exec "$container" cat /sys/fs/cgroup/memory.events
    echo "--- processi di servizio ---"
    docker exec "$container" sh -c 'for d in /proc/[0-9]*; do p=${d#/proc/}; \
      cmd=$(tr "\0" " " < $d/cmdline 2>/dev/null); \
      ppid=$(awk "{print \$4}" $d/stat 2>/dev/null); \
      [ -n "$cmd" ] && echo "$p $ppid $cmd"; done'
    echo "--- ambiente del pid 1 ---"
    docker exec "$container" sh -c 'tr "\0" "\n" < /proc/1/environ | grep -E "^(GNR_ASGI_|LEGACY_)" | sort'
  } > "$out" 2>&1

  local cpu_max mem_max
  cpu_max="$(docker exec "$container" cat /sys/fs/cgroup/cpu.max 2>/dev/null)"
  mem_max="$(docker exec "$container" cat /sys/fs/cgroup/memory.max 2>/dev/null)"
  # 8 core = 800000 su un periodo di 100000. 4 GiB = 4294967296.
  case "$cpu_max" in
    "800000 100000") lab_log "  cpu.max $cpu_max = 8 core: ok" ;;
    *) lab_log "  STOP: cpu.max e' '$cpu_max', attesi 8 core"; problems=1 ;;
  esac
  case "$mem_max" in
    4294967296) lab_log "  memory.max $mem_max = 4 GiB: ok" ;;
    *) lab_log "  STOP: memory.max e' '$mem_max', attesi 4294967296"; problems=1 ;;
  esac

  local oom
  oom="$(docker exec "$container" sh -c 'awk "/^(max|oom|oom_kill|oom_group_kill) /{s+=\$2} END{print s+0}" /sys/fs/cgroup/memory.events')"
  if [ "$oom" = "0" ]; then
    lab_log "  contatori OOM iniziali a zero: ok"
  else
    lab_log "  STOP: i contatori OOM valgono $oom prima di misurare"; problems=1
  fi

  if [ "$service" = "legacy" ]; then
    local gunicorn
    gunicorn="$(grep -c "gnrserveprod" "$out")"
    # Il master piu' i worker: expected+1 righe di gnrserveprod.
    if [ "$gunicorn" = "$((expected + 1))" ]; then
      lab_log "  gunicorn: 1 master + $expected worker: ok"
    else
      lab_log "  STOP: $gunicorn processi gnrserveprod, attesi $((expected + 1))"; problems=1
    fi
    grep -q "gnrdaemon" "$out" || { lab_log "  STOP: gnrdaemon assente"; problems=1; }
  else
    # GNR_ASGI_ORCH_LOG e' il path DENTRO il container, esportato da
    # lab_orders_paths: si verifica che il pid 1 lo porti davvero.
    if grep -q "GNR_ASGI_ORCH_LOG=$GNR_ASGI_ORCH_LOG" "$out"; then
      lab_log "  journal del container $GNR_ASGI_ORCH_LOG: ok"
    else
      lab_log "  STOP: il pid 1 non porta GNR_ASGI_ORCH_LOG=$GNR_ASGI_ORCH_LOG"
      problems=1
    fi
    if [ "$profile" = "bridge_dynamic" ]; then
      if grep -q "GNR_ASGI_WORKER_MAX_USERS" "$out"; then
        lab_log "  STOP: GNR_ASGI_WORKER_MAX_USERS presente: il cap e' vietato qui"
        problems=1
      else
        lab_log "  nessun cap di utenti per worker: ok"
      fi
    fi
  fi
  [ "$problems" = "0" ] || return 7
  lab_log "  configurazione viva certificata -> $(basename "$out")"
  return 0
}

run_leg () {
  # run_leg <profilo> — un profilo: ricrea il servizio, certifica, misura, chiude.
  local profile="$1"
  local service base container override instance topology
  local expect_workers expect_per_worker processes
  case "$profile" in
    legacy_w12)
      service=legacy; base="http://127.0.0.1:8099"; container=genro-bench-lab-legacy-1
      instance=legacy_lab; topology=fixed; processes=12
      expect_workers=12; expect_per_worker=0
      override="$SCENARIO_DIR/overrides/override_legacy_g12.yaml"
      export LEGACY_WORKERS=12
      ;;
    legacy_w16)
      service=legacy; base="http://127.0.0.1:8099"; container=genro-bench-lab-legacy-1
      instance=legacy_lab; topology=fixed; processes=16
      expect_workers=16; expect_per_worker=0
      override="$SCENARIO_DIR/overrides/override_legacy_g16.yaml"
      export LEGACY_WORKERS=16
      ;;
    bridge_dynamic)
      service=bridge; base="http://127.0.0.1:8098"; container=genro-bench-lab-bridge-1
      instance=bridge_lab; topology=dynamic; processes=0
      # In dinamica il numero non e' atteso: il driver passa l'osservato. Questi
      # due valori servono solo alla firma del driver e non asseriscono nulla.
      expect_workers=16; expect_per_worker=0
      override="$SCENARIO_DIR/overrides/override_bridge_dynamic.yaml"
      ;;
    *) lab_log "STOP: profilo sconosciuto: $profile"; return 2 ;;
  esac
  export LAB_OVERRIDE="$override"
  export LAB_SERVICE="$service"
  local out="$WORK_DIR/${PREFIX}_${profile}"

  lab_log "=== profilo $profile ==="
  lab_require_same_plan "$PLAN" "$PLAN_SHA256" "$profile" || return 6
  if [ "$service" = "bridge" ]; then
    lab_orders_paths "$CYCLE_RUNTIME_DIR" "${PREFIX}_${profile}" || return 9
  fi
  lab_render_compose "${out}_compose.yaml" || { lab_log "STOP: render fallito"; return 8; }
  lab_stop_others "$service"
  lab_recreate_service "$service" || { lab_log "STOP: il servizio non si e' ricreato"; return 8; }

  if [ "$service" = "bridge" ]; then
    python3 - "$base" <<'PY' || { lab_log "STOP: il bridge non ha risposto al census"; return 5; }
import json, sys, time, urllib.request
base = sys.argv[1]
for _ in range(150):
    try:
        with urllib.request.urlopen(base + "/_server/inspector/census", timeout=5) as answer:
            site = json.load(answer)["site"]
            pool = site["groups"]["pool"]
            print(f"  bridge pronto: worker {list(pool['workers'])}, "
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

  certify_live "$profile" "$service" "$container" "$processes" || return 7

  local extra=""
  if [ "$service" = "bridge" ]; then
    extra="--census $base/_server/inspector/census --journal $LAB_ORDERS_DECISIONS"
  fi
  lab_log "  avvio del driver (topologia $topology)"
  python3 "$SCENARIO_DIR/cycle_probe.py" --stack "$service" --run "${PREFIX}_${profile}" \
    --base "$base" --container "$container" --plan "$PLAN" --out "$out" \
    --topology "$topology" \
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
  lab_log "  profilo $profile: exit $rc"
  tail -4 "${out}_driver.log" | sed 's/^/    /'
  return $rc
}

lab_run_legs "$ORDER" "$STATUS" "$WORK_DIR"
exit $?
