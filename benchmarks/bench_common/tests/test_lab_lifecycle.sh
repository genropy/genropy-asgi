#!/bin/bash
# Controlli diretti del ciclo di vita dei runner, senza Docker e senza laboratorio.
#
# Un `docker` finto sta davanti al PATH e APPENDE ogni invocazione a un file:
# cosi' "quali comandi verrebbero eseguiti" e' un elenco misurato, e "nessun
# comando durante il cleanup" e' un numero, non un'affermazione.
#
#   ./test_lab_lifecycle.sh
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAILURES=0

report () {
  # report <esito 0|1> <etichetta> [dettaglio]
  if [ "$1" = "0" ]; then
    printf '  [ok  ] %s\n' "$2"
  else
    printf '  [FAIL] %s%s\n' "$2" "${3:+  -- $3}"
    FAILURES=$((FAILURES + 1))
  fi
}

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT INT TERM

mkdir -p "$SANDBOX/bin" "$SANDBOX/runtime" "$SANDBOX/work"
cat > "$SANDBOX/bin/docker" <<'FAKE'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  inspect) echo "${FAKE_INSPECT:-4242}" ;;
  exec)    echo "${FAKE_EXEC_OUT:-}" ;;
  compose) [ "${FAKE_COMPOSE_RC:-0}" = "0" ] || exit "${FAKE_COMPOSE_RC}"; echo "compose ok" ;;
esac
exit 0
FAKE
chmod +x "$SANDBOX/bin/docker"
export PATH="$SANDBOX/bin:$PATH"
export FAKE_DOCKER_LOG="$SANDBOX/docker.log"
: > "$FAKE_DOCKER_LOG"

calls () { wc -l < "$FAKE_DOCKER_LOG" | tr -d ' '; }

. "$HERE/../lab_lifecycle.sh"

echo "== il journal: due namespace dello stesso file, host e container =="
# Lo smoke del 2026-08-31 e' morto perche' al container era passato il path
# dell'host: il commander non trovava il file, on_startup sollevava, e il pool
# non nasceva. Questi controlli tengono separati i due nomi.
if lab_orders_paths "$SANDBOX/runtime" "prova" >/dev/null; then
  report 0 "i path sono decisi prima del container"
else
  report 1 "i path sono decisi prima del container"
fi
[ "$GNR_ASGI_ORCH_LOG" = "/lab/runtime/prova_orders.log" ]
report $? "GNR_ASGI_ORCH_LOG e' il path DEL CONTAINER" "$GNR_ASGI_ORCH_LOG"
case "$GNR_ASGI_ORCH_LOG" in
  "$SANDBOX"*) report 1 "e non contiene la directory dell'host" "$GNR_ASGI_ORCH_LOG" ;;
  *) report 0 "e non contiene la directory dell'host" ;;
esac
[ "$LAB_ORDERS_LOG" = "$SANDBOX/runtime/prova_orders.log" ]
report $? "LAB_ORDERS_LOG e' il path DELL'HOST" "$LAB_ORDERS_LOG"
[ "$LAB_ORDERS_DECISIONS" = "$SANDBOX/runtime/prova_orders.decisions.jsonl" ]
report $? "LAB_ORDERS_DECISIONS e' il path DELL'HOST" "$LAB_ORDERS_DECISIONS"
[ "$(basename "$GNR_ASGI_ORCH_LOG")" = "$(basename "$LAB_ORDERS_LOG")" ]
report $? "i due nomi indicano lo stesso file attraverso il bind mount" \
  "$(basename "$GNR_ASGI_ORCH_LOG")"
[ "$(basename "${GNR_ASGI_ORCH_LOG%.log}.decisions.jsonl")" = "$(basename "$LAB_ORDERS_DECISIONS")" ]
report $? "e cosi' il JSONL delle decisioni"
# Il driver gira sull'host, quindi --journal deve portargli il path dell'host.
for runner in "$HERE/../../l120_comparison/run_compare.sh" \
              "$HERE/../../population_freeze/run_population.sh"; do
  grep -q -- '--journal \$LAB_ORDERS_DECISIONS' "$runner"
  report $? "$(basename "$runner") passa al driver il path host del journal"
  grep -q -- '--journal \$GNR_ASGI_ORCH_LOG' "$runner" && \
    report 1 "  e non quello del container" "lo passa" || \
    report 0 "  e non quello del container"
done
# L'override consegna al container la variabile: sostituendola, il render non
# deve contenere una sola volta la directory dell'host.
OVERRIDE="$HERE/../../l120_comparison/overrides/override_bridge_w4.yaml"
RESO="$(sed "s|\${GNR_ASGI_ORCH_LOG:?}|$GNR_ASGI_ORCH_LOG|" "$OVERRIDE")"
printf '%s' "$RESO" | grep -q "GNR_ASGI_ORCH_LOG: /lab/runtime/prova_orders.log"
report $? "il render dell'override porta il path del container"
printf '%s' "$RESO" | grep -q "GNR_ASGI_ORCH_LOG: $SANDBOX" && \
  report 1 "e mai quello dell'host" "lo porta" || \
  report 0 "e mai quello dell'host"
# Il grep guarda le righe ESEGUIBILI, non i commenti: il vecchio schema
# `mv orders.log` e' citato nell'intestazione proprio per dire che non si usa.
grep -vE "^\s*#" "$HERE/../lab_lifecycle.sh" | grep -qE "(^|[^a-z])mv " && \
  report 1 "nessun mv eseguibile nel ciclo di vita" "presente" || \
  report 0 "nessun mv eseguibile nel ciclo di vita"
# Un journal che esiste gia' ferma la corsa invece di sovrascriverlo.
: > "$SANDBOX/runtime/gia_orders.log"
lab_orders_paths "$SANDBOX/runtime" "gia" >/dev/null 2>&1
[ $? -eq 9 ]
report $? "un journal con lo stesso prefisso ferma la corsa"

echo "== la traccia si verifica PRIMA di toccare il laboratorio =="
# Su macOS `sha256sum` non esiste: uno shim su `shasum -a 256` permette di
# esercitare davvero la verifica anche qui. Sui runner Linux e' il comando vero.
if ! command -v sha256sum >/dev/null 2>&1; then
  cat > "$SANDBOX/bin/sha256sum" <<'SHIM'
#!/bin/bash
exec shasum -a 256 "$@"
SHIM
  chmod +x "$SANDBOX/bin/sha256sum"
fi
mkdir -p "$SANDBOX/traces"
printf 'contenuto della traccia\n' > "$SANDBOX/traces/piano.json"
( cd "$SANDBOX/traces" && sha256sum piano.json > piano.json.sha256 )
: > "$FAKE_DOCKER_LOG"
lab_verify_trace "$SANDBOX/traces" "piano.json.sha256" >/dev/null 2>&1
report $? "una traccia integra fa proseguire"
[ "$(calls)" = "0" ]
report $? "e la verifica non ha invocato docker" "$(calls) invocazioni"
printf 'contenuto alterato\n' > "$SANDBOX/traces/piano.json"
lab_verify_trace "$SANDBOX/traces" "piano.json.sha256" >/dev/null 2>&1
[ $? -eq 6 ]
report $? "una traccia alterata ferma la corsa con 6"
[ "$(calls)" = "0" ]
report $? "e nemmeno allora si e' toccato il laboratorio" "$(calls) invocazioni"
lab_verify_trace "$SANDBOX/traces" "assente.sha256" >/dev/null 2>&1
[ $? -eq 6 ]
report $? "un manifest assente ferma la corsa con 6"

echo
echo "== le due gambe leggono lo stesso file, byte per byte =="
: > "$FAKE_DOCKER_LOG"
DIGEST="$(lab_plan_digest "$SANDBOX/traces/piano.json")"
[ -n "$DIGEST" ]
report $? "il digest del piano si prende una volta" "${DIGEST:0:16}..."
lab_require_same_plan "$SANDBOX/traces/piano.json" "$DIGEST" "legacy" >/dev/null
report $? "la prima gamba lo ritrova"
lab_require_same_plan "$SANDBOX/traces/piano.json" "$DIGEST" "bridge" >/dev/null
report $? "e la seconda pure"
printf 'un piano rigenerato\n' > "$SANDBOX/traces/piano.json"
lab_require_same_plan "$SANDBOX/traces/piano.json" "$DIGEST" "bridge" >/dev/null 2>&1
[ $? -eq 6 ]
report $? "un piano cambiato fra le due gambe ferma la seconda con 6"
lab_require_same_plan "$SANDBOX/traces/assente.json" "$DIGEST" "bridge" >/dev/null 2>&1
[ $? -eq 6 ]
report $? "un piano sparito ferma la gamba con 6"
[ "$(calls)" = "0" ]
report $? "nessuna di queste verifiche ha invocato docker" "$(calls) invocazioni"
grep -vE "^\s*#" "$HERE/../lab_lifecycle.sh" | grep -qE "make_trace|make_population_trace" && \
  report 1 "il ciclo di vita non genera piani" "li genera" || \
  report 0 "il ciclo di vita non genera piani"

echo
echo "== la directory dei risultati non si sovrascrive =="
lab_require_free_dir "$SANDBOX/work/nuova" >/dev/null
report $? "una directory nuova va bene"
echo x > "$SANDBOX/work/nuova/risultato.csv"
lab_require_free_dir "$SANDBOX/work/nuova" >/dev/null 2>&1
[ $? -eq 9 ]
report $? "una directory con dentro qualcosa ferma la corsa"

echo
echo "== il manifest si prende SOLO a writer chiusi =="
sleep 30 &
WRITER=$!
LAB_WRITER_PIDS="$WRITER"
lab_hash_outputs "$SANDBOX/work/nuova" >/dev/null 2>&1
[ $? -eq 8 ]
report $? "con un writer vivo il manifest non si scrive"
[ ! -f "$SANDBOX/work/nuova/MANIFEST.sha256" ]
report $? "e non lascia un manifest a meta'"
kill "$WRITER" 2>/dev/null; wait "$WRITER" 2>/dev/null
LAB_WRITER_PIDS=""
lab_hash_outputs "$SANDBOX/work/nuova" >/dev/null
report $? "a writer chiusi il manifest si scrive"
grep -q "risultato.csv" "$SANDBOX/work/nuova/MANIFEST.sha256"
report $? "il manifest elenca il risultato"
grep -q "MANIFEST.sha256" "$SANDBOX/work/nuova/MANIFEST.sha256" && \
  report 1 "il manifest non elenca se stesso" "si elenca" || \
  report 0 "il manifest non elenca se stesso"

echo
echo "== la terminazione: TERM, poi attesa vera, mai un kill =="
sleep 30 &
VICTIM=$!
lab_terminate_and_wait "$VICTIM" 10 >/dev/null
report $? "un processo che risponde a TERM viene atteso"
kill -0 "$VICTIM" 2>/dev/null && report 1 "ed e' davvero morto" || report 0 "ed e' davvero morto"
lab_terminate_and_wait "" 5 >/dev/null
report $? "un pid vuoto non e' un errore"
lab_terminate_and_wait 999999 2 >/dev/null
report $? "un pid inesistente non e' un errore"
grep -qE "kill -(9|KILL)" "$HERE/../lab_lifecycle.sh" && \
  report 1 "nessun kill -9 nel ciclo di vita" "presente" || \
  report 0 "nessun kill -9 nel ciclo di vita"

echo
echo "== il cleanup: idempotente, e non si ferma a meta' =="
: > "$FAKE_DOCKER_LOG"
LAB_CLEANUP_DONE=0
LAB_SERVICE=bridge
LAB_STOP_SERVICE=1
LAB_DIR="$SANDBOX"
LAB_OVERRIDE="$SANDBOX/override.yaml"
: > "$LAB_OVERRIDE"
: > "$SANDBOX/compose.yaml"
: > "$SANDBOX/.env"
LAB_FREEZE_TOUCHED=0
sleep 30 & S1=$!
sleep 30 & S2=$!
LAB_SAMPLER_PIDS="$S1 $S2"
lab_cleanup >"$SANDBOX/cleanup1.txt" 2>&1
report $? "il primo cleanup ritorna zero"
kill -0 "$S1" 2>/dev/null && report 1 "i campionatori sono terminati" || report 0 "i campionatori sono terminati"
grep -q "cleanup: fine" "$SANDBOX/cleanup1.txt"
report $? "il cleanup arriva alla fine e lo dichiara"
FIRST_CALLS=$(calls)
lab_cleanup >"$SANDBOX/cleanup2.txt" 2>&1
report $? "il secondo cleanup ritorna zero"
grep -q "gia' eseguito" "$SANDBOX/cleanup2.txt"
report $? "e dichiara di non ripetersi"
[ "$(calls)" = "$FIRST_CALLS" ]
report $? "il secondo cleanup non invoca docker" "prima $FIRST_CALLS, dopo $(calls)"

echo
echo "== un passo che fallisce non interrompe i successivi =="
LAB_CLEANUP_DONE=0
LAB_FREEZE_TOUCHED=1
LAB_FREEZE_RESTORE=""
export FAKE_COMPOSE_RC=1
sleep 30 & S3=$!
LAB_SAMPLER_PIDS="$S3"
lab_cleanup >"$SANDBOX/cleanup3.txt" 2>&1
report $? "il cleanup ritorna zero anche con un passo fallito"
grep -q "ripristino il freeze" "$SANDBOX/cleanup3.txt"
report $? "il ripristino del freeze e' stato tentato"
grep -q "cleanup: fine" "$SANDBOX/cleanup3.txt"
report $? "e i passi dopo quello fallito sono stati eseguiti"
grep -q "cleanup rc=1" "$SANDBOX/cleanup3.txt"
report $? "il passo fallito e' registrato col suo codice"
unset FAKE_COMPOSE_RC

echo
echo "== il freeze si ripristina sempre, anche uscendo male =="
cat > "$SANDBOX/trapped.sh" <<'SCRIPT'
set -u
. "$LIFECYCLE"
LAB_DIR="$SANDBOX"; LAB_OVERRIDE="$SANDBOX/override.yaml"
LAB_SERVICE=bridge; LAB_STOP_SERVICE=0
LAB_FREEZE_TOUCHED=1; LAB_FREEZE_RESTORE=""
lab_arm_cleanup
exit 7
SCRIPT
LIFECYCLE="$HERE/../lab_lifecycle.sh" SANDBOX="$SANDBOX" bash "$SANDBOX/trapped.sh" > "$SANDBOX/trap_exit.txt" 2>&1
[ $? -eq 7 ]
report $? "la trap non nasconde il codice di uscita"
grep -q "ripristino il freeze" "$SANDBOX/trap_exit.txt"
report $? "e il freeze e' stato ripristinato uscendo"

# Il segnale lo manda lo script A SE STESSO. Un figlio messo in background da una
# shell non interattiva ha SIGINT impostato a ignorare — e' il job control della
# shell, non il codice sotto prova — e un `kill -INT` da fuori non arriverebbe.
for signal in INT TERM; do
  cat > "$SANDBOX/trap_$signal.sh" <<'SCRIPT'
set -u
. "$LIFECYCLE"
LAB_DIR="$SANDBOX"; LAB_OVERRIDE="$SANDBOX/override.yaml"
LAB_SERVICE=bridge; LAB_STOP_SERVICE=0
LAB_FREEZE_TOUCHED=1; LAB_FREEZE_RESTORE=""
lab_arm_cleanup
kill "-$SIGNAL_NAME" $$
sleep 5
SCRIPT
  LIFECYCLE="$HERE/../lab_lifecycle.sh" SANDBOX="$SANDBOX" SIGNAL_NAME="$signal" \
    bash "$SANDBOX/trap_$signal.sh" > "$SANDBOX/out_$signal.txt" 2>&1
  code=$?
  grep -q "ricevuto $signal" "$SANDBOX/out_$signal.txt"
  report $? "$signal esegue il cleanup"
  grep -q "ripristino il freeze" "$SANDBOX/out_$signal.txt"
  report $? "$signal ripristina il freeze"
  expected=143; [ "$signal" = "INT" ] && expected=130
  [ "$code" = "$expected" ]
  report $? "$signal esce con $expected" "uscito con $code"
done

echo
echo "== le gambe non girano insieme =="
: > "$FAKE_DOCKER_LOG"
cat > "$SANDBOX/bin/docker" <<'FAKE'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  ps) echo "genro-bench-lab-bridge-1"; echo "genro-bench-lab-legacy-1" ;;
  inspect) echo "${FAKE_INSPECT:-4242}" ;;
esac
exit 0
FAKE
lab_stop_others bridge >/dev/null
grep -q "stop genro-bench-lab-legacy-1" "$FAKE_DOCKER_LOG"
report $? "prima della gamba bridge si ferma il legacy"
grep -q "stop genro-bench-lab-bridge-1" "$FAKE_DOCKER_LOG" && \
  report 1 "e non si ferma la gamba che sta partendo" "fermata" || \
  report 0 "e non si ferma la gamba che sta partendo"
: > "$FAKE_DOCKER_LOG"
lab_stop_others legacy >/dev/null
grep -q "stop genro-bench-lab-bridge-1" "$FAKE_DOCKER_LOG"
report $? "prima della gamba legacy si ferma il bridge"
: > "$FAKE_DOCKER_LOG"
lab_stop_service bridge >/dev/null
grep -q "stop genro-bench-lab-bridge-1" "$FAKE_DOCKER_LOG"
report $? "a gamba conclusa il suo stack si ferma"

echo
echo "== una gamba fallita ferma la sequenza =="
mkdir -p "$SANDBOX/legs"
LAB_WRITER_PIDS=""
run_leg () {
  echo "$1" >> "$SANDBOX/legs/eseguite.txt"
  [ "$1" = "$FALLISCE" ] && return 3
  return 0
}
: > "$SANDBOX/legs/eseguite.txt"
FALLISCE="__nessuna__"
lab_run_legs "legacy,bridge" "$SANDBOX/legs/stato_ok.txt" "$SANDBOX/legs" >/dev/null 2>&1
report $? "con due gambe sane la sequenza ritorna zero"
[ "$(tr '\n' ' ' < "$SANDBOX/legs/eseguite.txt")" = "legacy bridge " ]
report $? "e le esegue nell'ordine dichiarato" "$(tr '\n' ' ' < "$SANDBOX/legs/eseguite.txt")"
grep -q "TUTTE COMPLETATE" "$SANDBOX/legs/stato_ok.txt"
report $? "il file di stato dichiara il completamento"

: > "$SANDBOX/legs/eseguite.txt"
rm -f "$SANDBOX/legs/MANIFEST.sha256"
FALLISCE="legacy"
lab_run_legs "legacy,bridge" "$SANDBOX/legs/stato_ko.txt" "$SANDBOX/legs" >/dev/null 2>&1
[ $? -eq 3 ]
report $? "la sequenza propaga il codice della gamba fallita"
[ "$(tr '\n' ' ' < "$SANDBOX/legs/eseguite.txt")" = "legacy " ]
report $? "e la gamba successiva NON viene eseguita" "$(tr '\n' ' ' < "$SANDBOX/legs/eseguite.txt")"
grep -q "FERMATA SU legacy (exit=3)" "$SANDBOX/legs/stato_ko.txt"
report $? "il file di stato dice dove e con quale codice"
grep -q "TUTTE COMPLETATE" "$SANDBOX/legs/stato_ko.txt" && \
  report 1 "e non dichiara un completamento" "lo dichiara" || \
  report 0 "e non dichiara un completamento"
[ -f "$SANDBOX/legs/MANIFEST.sha256" ]
report $? "gli output raccolti fino al guasto sono comunque sigillati"

: > "$SANDBOX/legs/eseguite.txt"
FALLISCE="bridge"
lab_run_legs "bridge,legacy" "$SANDBOX/legs/stato_inv.txt" "$SANDBOX/legs" >/dev/null 2>&1
[ "$(tr '\n' ' ' < "$SANDBOX/legs/eseguite.txt")" = "bridge " ]
report $? "l'ordine inverso funziona senza toccare codice"
unset -f run_leg

echo
echo "== quali comandi docker verrebbero eseguiti =="
cat > "$SANDBOX/bin/docker" <<'FAKE'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  inspect) echo "${FAKE_INSPECT:-4242}" ;;
  exec)    echo "${FAKE_EXEC_OUT:-}" ;;
  compose) [ "${FAKE_COMPOSE_RC:-0}" = "0" ] || exit "${FAKE_COMPOSE_RC}"; echo "compose ok" ;;
esac
exit 0
FAKE
: > "$FAKE_DOCKER_LOG"
LAB_DIR="$SANDBOX"; LAB_OVERRIDE="$SANDBOX/override.yaml"; LAB_PROJECT=genro-bench-lab
lab_render_compose "$SANDBOX/render.yaml" >/dev/null 2>&1
lab_recreate_service bridge >/dev/null 2>&1
lab_certify_live_env genro-bench-lab-bridge-1 GNR_ASGI_IDLE_FREEZE_MINUTES >/dev/null 2>&1
echo "  --- registro del docker finto ---"
sed 's/^/    /' "$FAKE_DOCKER_LOG"
grep -q -- "--force-recreate" "$FAKE_DOCKER_LOG"
report $? "la ricreazione usa --force-recreate, non restart"
grep -q -- "--project-directory $SANDBOX" "$FAKE_DOCKER_LOG"
report $? "compose riceve sempre project-directory ed env-file"
grep -q "proc/1/environ" "$FAKE_DOCKER_LOG"
report $? "il valore vivo si legge da /proc/1/environ"
grep -cq "restart" "$FAKE_DOCKER_LOG" && \
  report 1 "nessun compose restart" "presente" || \
  report 0 "nessun compose restart"

echo
echo "=================================================="
if [ "$FAILURES" != "0" ]; then
  echo "FALLITI $FAILURES"
  exit 1
fi
echo "tutti i controlli passati"
