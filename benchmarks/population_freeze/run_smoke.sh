#!/bin/bash
# Lo smoke della prova di popolazione: venti utenti, working set cinque, freeze
# a un minuto.
#
#   ./run_smoke.sh [ordine]
#
# Serve a provare che le otto fasi girino, che il freeze arrivi davvero nel
# container e che un congelamento e un risveglio si vedano nei dati. Non e' una
# misura: venti utenti non pesano.
#
# Il freeze a un minuto obbliga un riposo di 150 secondi, perche' il vertice
# giudica l'inattivita' ogni sessanta secondi su una foto vecchia fino a cinque:
# la banda di incertezza e' di 65 secondi e va superata, non compressa.
#
# Dura circa nove minuti per gamba.
set -u

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLAN="${PLAN:-$SCENARIO_DIR/traces/population_smoke.json}"
ORDER="${1:-bridge}"
# Venti utenti non pesano: 4 GiB bastano e la prova non e' una misura.
export POP_MEM_LIMIT="${POP_MEM_LIMIT:-4g}"

FREEZE_MINUTES="${FREEZE_MINUTES:-1}" \
exec "$SCENARIO_DIR/run_population.sh" "$PLAN" pop_smoke "$ORDER"
