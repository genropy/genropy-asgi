#!/bin/bash
# La popolazione piena: 2000 utenti, working set 80, le due gambe in sequenza.
#
#   ./run_full.sh [ordine]
#
# Il limite di memoria e' 24 GiB, deciso dal titolare per la piena a 2000 utenti:
# su una macchina da 61 GiB lascia 37 GiB a host e PostgreSQL. Uguale alle due
# gambe. POP_MEM_LIMIT lo sovrascrive, se serve.
#
# IL GATE. Questo script si rifiuta di partire se il pilot non ha lasciato i suoi
# output. Il gate guarda che i file ESISTANO e non siano vuoti: NON ne giudica il
# contenuto. Leggerli e decidere se la piena ha senso e' del titolare, e un
# controllo automatico che pretendesse di farlo al suo posto sarebbe un GO
# implicito.
#
# Durata attesa, per gamba: 33 minuti di ingressi, 7 di riposo, 3 di risveglio,
# 20 di lavoro, 25 di rotazione, 7 di secondo riposo, il logout di 2000 sessioni
# e 5 minuti di osservazione — circa 1h45 per gamba, circa 3h30 per entrambe.
# Va annunciata prima di lanciarla.
set -u

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd "$SCENARIO_DIR/.." && pwd)"
. "$BENCH_DIR/bench_common/lab_lifecycle.sh"

PLAN="${PLAN:-$SCENARIO_DIR/traces/population_full.json}"
ORDER="${1:-bridge,legacy}"
export POP_MEM_LIMIT="${POP_MEM_LIMIT:-24g}"
PILOT_DIR="${PILOT_DIR:-$SCENARIO_DIR/runs/pop_pilot}"

# I tre file che il pilot lascia sempre, se e' arrivato in fondo.
PILOT_REQUIRED="
pop_pilot_bridge_phases.csv
pop_pilot_bridge_samples.csv
pop_pilot_bridge_outcome.json
"

missing=""
for name in $PILOT_REQUIRED; do
  if [ ! -s "$PILOT_DIR/$name" ]; then
    missing="$missing $name"
  fi
done
if [ -n "$missing" ]; then
  lab_log "STOP: il pilot non ha lasciato i suoi output in $PILOT_DIR"
  lab_log "  assenti o vuoti:$missing"
  lab_log "  La piena non parte prima del pilot. Lancia ./run_pilot.sh, leggi i"
  lab_log "  suoi dati, e solo allora decidi se questa corsa ha senso."
  exit 9
fi
lab_log "gate del pilot: i suoi output ci sono in $PILOT_DIR"
lab_log "  (esistenza e non-vuoto: il contenuto lo giudica il titolare, non questo script)"

exec "$SCENARIO_DIR/run_population.sh" "$PLAN" pop_full "$ORDER"
