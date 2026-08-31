#!/bin/bash
# Il pilot: 200 utenti, working set 80, sul solo bridge.
#
#   ./run_pilot.sh
#
# Il limite di memoria e' 8 GiB, deciso dal titolare per il pilot a 200 utenti.
# POP_MEM_LIMIT lo sovrascrive, se serve.
#
# Il pilot NON passa alla popolazione piena. Produce dati, ripristina
# l'ambiente, termina. La piena parte soltanto con un GO esplicito del titolare,
# che legge questi dati per primo: questo script non li giudica, e non deve.
#
# Dura circa 40 minuti: 200 ingressi a un secondo, 7 minuti di riposo (che
# devono superare il freeze a 5 minuti piu' la banda di incertezza di 65s), 3
# minuti di risveglio, 5 di lavoro, 5 di rotazione, 7 di secondo riposo, il
# logout e 3 minuti di osservazione.
set -u

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLAN="${PLAN:-$SCENARIO_DIR/traces/population_pilot.json}"
ORDER="${1:-bridge}"
export POP_MEM_LIMIT="${POP_MEM_LIMIT:-8g}"

exec "$SCENARIO_DIR/run_population.sh" "$PLAN" pop_pilot "$ORDER"
