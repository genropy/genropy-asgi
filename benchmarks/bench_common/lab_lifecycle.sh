#!/bin/bash
# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0
#
# The runner side of a benchmark run: how a stack is raised, how a driver is
# really stopped, and how the laboratory is put back the way it was found.
#
# Sourced, never executed. Every function is prefixed `lab_` and touches only
# variables it declares or receives.
#
# Three rules the previous campaign learned the hard way, and this file enforces:
#
# 1. THE CLEANUP IS NEVER INTERRUPTED. It runs with `set +e` inside its own
#    subshell-free body, every step is logged with its own exit code, and a step
#    that fails does not prevent the ones after it. A cleanup that stops halfway
#    leaves a container up and a freeze value changed.
#
# 2. NO OPEN LOG IS EVER RENAMED. The orders log path is decided BEFORE the
#    bridge is created and passed in through GNR_ASGI_ORCH_LOG, so the file is
#    born with its final name. The old `mv orders.log` left the bridge writing
#    into a renamed inode and produced an orders.log that no longer existed.
#
# 3. OUTPUTS ARE HASHED ONLY AFTER THEIR WRITERS ARE CLOSED. `lab_hash_outputs`
#    refuses to run while the driver or a sampler is still alive, because a
#    manifest taken over a file still being appended to is a manifest of
#    something that no longer exists.

# ------------------------------------------------------------------ diagnostica

lab_log() {
  printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"
}

lab_step() {
  # lab_step <descrizione> <comando...> — esegue e registra l'esito, senza mai fermarsi.
  local what="$1"; shift
  "$@" >/dev/null 2>&1
  local rc=$?
  if [ $rc -eq 0 ]; then lab_log "  cleanup ok   : $what"; else lab_log "  cleanup rc=$rc: $what"; fi
  return 0
}

# ------------------------------------------------------------------ collisioni

lab_require_free_dir() {
  # Una corsa non sovrascrive mai i risultati di un'altra.
  local dir="$1"
  if [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
    lab_log "STOP: la directory dei risultati esiste e non e' vuota: $dir"
    return 9
  fi
  mkdir -p "$dir" || return 9
  return 0
}

lab_require_tools() {
  # Gli strumenti Linux che i runner usano davvero. Su macOS questo fallisce, ed e' voluto.
  local missing=""
  for tool in bash python3 sha256sum bc docker; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
  done
  [ -r /proc/loadavg ] || missing="$missing /proc/loadavg"
  if [ -n "$missing" ]; then
    lab_log "STOP: strumenti assenti:$missing"
    return 9
  fi
  return 0
}

lab_verify_trace() {
  # L'hash della traccia si verifica PRIMA di toccare il laboratorio: una traccia
  # corrotta non deve ricreare un container ne' spostare un file.
  local dir="$1" manifest="$2"
  if [ ! -f "$dir/$manifest" ]; then
    lab_log "STOP: manifest della traccia assente: $dir/$manifest"
    return 6
  fi
  if ! ( cd "$dir" && sha256sum -c "$manifest" ); then
    lab_log "STOP: la traccia non corrisponde al suo hash"
    return 6
  fi
  lab_log "traccia verificata: $dir/$manifest"
  return 0
}

lab_plan_digest() {
  # Il digest del piano, una riga sola. Vuoto se il file non c'e'.
  local plan="$1"
  [ -f "$plan" ] || return 1
  python3 -c "
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())" "$plan"
}

lab_require_same_plan() {
  # lab_require_same_plan <piano> <digest atteso> <etichetta>
  #
  # Le due gambe devono leggere lo STESSO file, byte per byte. Il digest si
  # prende una volta prima della sequenza e si ricontrolla prima di ogni gamba:
  # cosi' un piano rigenerato o toccato fra le due misure non passa inosservato.
  # Il piano NON viene mai generato qui: lo genera make_plans.sh, una volta per
  # campagna.
  local plan="$1" want="$2" label="$3" got
  got="$(lab_plan_digest "$plan")" || {
    lab_log "STOP: piano assente prima della gamba $label: $plan"
    return 6
  }
  if [ "$got" != "$want" ]; then
    lab_log "STOP: il piano e' cambiato prima della gamba $label"
    lab_log "  atteso   $want"
    lab_log "  ottenuto $got"
    lab_log "  Le due gambe leggerebbero file diversi: il confronto non avrebbe valore."
    return 6
  fi
  lab_log "  piano certificato per $label: ${got:0:16}..."
  return 0
}

lab_wait_for_load() {
  # Il load deve scendere sotto la soglia, altrimenti la corsa non parte.
  local ceiling="${1:-4.0}" tries="${2:-60}"
  local i load
  for i in $(seq 1 "$tries"); do
    load=$(cut -d' ' -f1 /proc/loadavg)
    if [ "$(echo "$load < $ceiling" | bc)" = "1" ]; then
      lab_log "load $load sotto $ceiling: si parte"
      return 0
    fi
    lab_log "load $load troppo alto, attendo ($i/$tries)"
    sleep 10
  done
  lab_log "STOP: il load non e' sceso sotto $ceiling entro il timeout"
  return 4
}

# ------------------------------------------------------------------ orders log

lab_orders_paths() {
  # Decide i due path del journal PRIMA che il bridge nasca, e li esporta.
  # Il file umano e il JSONL delle decisioni nascono col nome finale: nessun mv.
  local runtime="$1" prefix="$2"
  export GNR_ASGI_ORCH_LOG="$runtime/${prefix}_orders.log"
  LAB_ORDERS_LOG="$GNR_ASGI_ORCH_LOG"
  LAB_ORDERS_DECISIONS="${GNR_ASGI_ORCH_LOG%.log}.decisions.jsonl"
  if [ -e "$LAB_ORDERS_LOG" ] || [ -e "$LAB_ORDERS_DECISIONS" ]; then
    lab_log "STOP: un journal con questo prefisso esiste gia': $LAB_ORDERS_LOG"
    return 9
  fi
  lab_log "journal della corsa: $LAB_ORDERS_LOG"
  lab_log "decisioni della corsa: $LAB_ORDERS_DECISIONS"
  return 0
}

# ------------------------------------------------------------------ compose

lab_compose() {
  # Un solo punto di verita' per project, project-directory, env e override.
  # LAB_DIR e LAB_OVERRIDE devono essere impostati dal runner.
  docker compose -p "${LAB_PROJECT:-genro-bench-lab}" \
    --project-directory "$LAB_DIR" --env-file "$LAB_DIR/.env" \
    -f "$LAB_DIR/compose.yaml" -f "$LAB_OVERRIDE" "$@"
}

lab_render_compose() {
  # Il render statico: certifica mount e variabili senza avviare nulla.
  local out="$1"
  lab_compose config > "$out" 2>"${out%.yaml}.err"
  local rc=$?
  lab_log "render compose rc=$rc -> $out"
  return $rc
}

lab_recreate_service() {
  # Un cambio di variabile d'ambiente NON sopravvive a `restart`: serve
  # --force-recreate. Il valore vivo si certifica dopo, da /proc/1/environ.
  local service="$1"
  lab_log "ricreo il servizio $service"
  lab_compose up -d --force-recreate --no-deps "$service"
  return $?
}

lab_stop_others() {
  # Le gambe girano in sequenza, MAI insieme: due stack accesi si contendono le
  # quattro CPU e il confronto misurerebbe la contesa. Prima di una gamba si
  # fermano gli altri stack, per nome di container e non per servizio, cosi' il
  # comando non dipende dagli override in vigore.
  local keep="$1" name
  for name in bridge legacy; do
    [ "$name" = "$keep" ] && continue
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "genro-bench-lab-${name}-1"; then
      lab_log "fermo lo stack $name: le gambe non girano insieme"
      docker stop "genro-bench-lab-${name}-1" >/dev/null 2>&1
    fi
  done
  return 0
}

lab_stop_service() {
  # A gamba conclusa lo stack si ferma: il suo container non deve restare acceso
  # mentre la gamba successiva misura.
  local service="$1"
  lab_log "fermo il servizio $service a gamba conclusa"
  docker stop "genro-bench-lab-${service}-1" >/dev/null 2>&1
  return 0
}

lab_certify_live_env() {
  # Il valore VIVO di una variabile dentro il container, letto da /proc/1/environ.
  local container="$1" name="$2"
  docker exec "$container" sh -c \
    "tr '\\0' '\\n' < /proc/1/environ | sed -n 's/^$name=//p'" 2>/dev/null
}

# ------------------------------------------------------------------ terminazione

lab_terminate_and_wait() {
  # TERM, poi attesa vera. Nessun kill: un kill troncherebbe i writer e
  # perderebbe i campioni che spiegano perche' la memoria e' cresciuta.
  local pid="$1" timeout="${2:-180}"
  local i
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    lab_log "  nessun processo da terminare (pid=${pid:-vuoto})"
    return 0
  fi
  lab_log "  TERM a $pid, attendo fino a ${timeout}s"
  kill -TERM "$pid" 2>/dev/null
  for i in $(seq 1 "$timeout"); do
    kill -0 "$pid" 2>/dev/null || { lab_log "  $pid concluso dopo ${i}s"; return 0; }
    sleep 1
  done
  lab_log "  ATTENZIONE: $pid ancora vivo dopo ${timeout}s; NON lo uccido, lo registro"
  return 1
}

# ------------------------------------------------------------------ hash

lab_writers_closed() {
  # Vero solo se ne' il driver ne' un campionatore sono ancora vivi.
  local pid
  for pid in "$@"; do
    [ -z "$pid" ] && continue
    if kill -0 "$pid" 2>/dev/null; then
      lab_log "writer ancora vivo: $pid"
      return 1
    fi
  done
  return 0
}

lab_hash_outputs() {
  # Il manifest si prende SOLO a writer chiusi. LAB_WRITER_PIDS elenca i pid da
  # controllare; se uno e' vivo, il manifest non si scrive affatto.
  local dir="$1" manifest="${2:-MANIFEST.sha256}"
  if ! lab_writers_closed ${LAB_WRITER_PIDS:-}; then
    lab_log "STOP: manifest non scritto, un writer e' ancora aperto"
    return 8
  fi
  ( cd "$dir" && find . -type f ! -name "$manifest" | sort | xargs sha256sum > "$manifest" )
  local rc=$?
  lab_log "manifest scritto rc=$rc: $dir/$manifest ($(wc -l < "$dir/$manifest" 2>/dev/null | tr -d ' ') righe)"
  return $rc
}

# ------------------------------------------------------------------ cleanup

lab_cleanup() {
  # Idempotente, sempre eseguito, mai interrotto. Registra ogni passo.
  # Le variabili che consuma, tutte opzionali:
  #   LAB_DRIVER_PID       il driver da terminare e attendere
  #   LAB_SAMPLER_PIDS     i campionatori di supporto
  #   LAB_SERVICE          il servizio da fermare a fine corsa
  #   LAB_FREEZE_RESTORE   il valore di freeze da ripristinare (anche vuoto)
  #   LAB_CLEANUP_DONE     guardia di idempotenza
  set +e
  if [ "${LAB_CLEANUP_DONE:-0}" = "1" ]; then
    lab_log "cleanup gia' eseguito: non lo ripeto"
    return 0
  fi
  LAB_CLEANUP_DONE=1
  lab_log "cleanup: inizio"
  local pid
  for pid in ${LAB_SAMPLER_PIDS:-}; do
    lab_step "termino il campionatore $pid" lab_terminate_and_wait "$pid" 30
  done
  if [ -n "${LAB_DRIVER_PID:-}" ]; then
    lab_terminate_and_wait "$LAB_DRIVER_PID" "${LAB_DRIVER_TIMEOUT:-240}"
    lab_log "  cleanup: driver $LAB_DRIVER_PID concluso o registrato"
  fi
  # Il freeze si ripristina SEMPRE, anche se la corsa e' morta a metà: il valore
  # sperimentale non deve sopravvivere alla prova che lo ha chiesto.
  if [ "${LAB_FREEZE_TOUCHED:-0}" = "1" ]; then
    export GNR_ASGI_IDLE_FREEZE_MINUTES="${LAB_FREEZE_RESTORE:-}"
    lab_step "ripristino il freeze a '${LAB_FREEZE_RESTORE:-<non impostato>}'" \
      lab_recreate_service "${LAB_SERVICE:-bridge}"
  fi
  if [ -n "${LAB_SERVICE:-}" ] && [ "${LAB_STOP_SERVICE:-1}" = "1" ]; then
    lab_step "fermo il servizio ${LAB_SERVICE}" lab_compose stop "${LAB_SERVICE}"
  fi
  lab_log "cleanup: fine"
  return 0
}

lab_arm_cleanup() {
  # Una sola trap per tutte le uscite. Da chiamare appena i path sono decisi.
  trap 'lab_cleanup' EXIT
  trap 'lab_log "ricevuto INT"; lab_cleanup; exit 130' INT
  trap 'lab_log "ricevuto TERM"; lab_cleanup; exit 143' TERM
}

# ------------------------------------------------------------------ la sequenza

lab_run_legs() {
  # lab_run_legs <ordine> <file di stato> — esegue le gambe nell'ordine dato,
  # chiamando la funzione `run_leg` che il runner definisce.
  #
  # Alla prima gamba che finisce male la sequenza SI FERMA: proseguire su un
  # laboratorio gia' storto produce misure senza valore. Gli output raccolti fino
  # a quel punto vengono comunque sigillati, e il file di stato dice dove ci si e'
  # fermati e con quale codice.
  local order="$1" status="$2" work="$3"
  local leg rc
  rm -f "$status"
  local legs
  IFS=',' read -r -a legs <<< "$order"
  for leg in "${legs[@]}"; do
    run_leg "$leg"
    rc=$?
    echo "$leg exit=$rc" >> "$status"
    if [ "$rc" -ne 0 ]; then
      echo "FERMATA SU $leg (exit=$rc): le gambe successive non sono state eseguite" >> "$status"
      lab_log "FERMATA SU $leg (exit=$rc)"
      lab_hash_outputs "$work"
      return "$rc"
    fi
  done
  echo "TUTTE COMPLETATE" >> "$status"
  lab_log "tutte le gambe completate"
  lab_hash_outputs "$work"
  return 0
}
