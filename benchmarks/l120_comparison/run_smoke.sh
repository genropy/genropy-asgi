#!/bin/bash
# Lo smoke del confronto L120: la stessa sequenza, finestre corte.
#
#   ./run_smoke.sh [ordine]
#
# Popola i 48 utenti veri e verifica la distribuzione [12,12,12,12], perche' e'
# la cosa che si rompe piu' facilmente; accorcia soltanto le finestre di carico.
# Dura circa cinque minuti per gamba: il popolamento e' 48 login a due secondi,
# e l'attesa dopo l'apply resta di 80 secondi — due beat pieni, che non si
# possono comprimere senza falsificare la certificazione della policy.
#
# Nessuna logica propria: e' run_compare.sh con un altro piano e un altro
# prefisso. Un runner che duplicasse la sequenza sarebbe un secondo runner da
# tenere allineato.
set -u

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORDER="${1:-legacy,bridge}"

PLAN="${PLAN:-$SCENARIO_DIR/traces/l120_smoke_plan.json}" \
WORK_DIR="${WORK_DIR:-$SCENARIO_DIR/runs/l120smoke}" \
exec "$SCENARIO_DIR/run_compare.sh" "$ORDER" l120smoke
