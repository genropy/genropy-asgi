#!/bin/bash
# Lo smoke del ciclo a otto core: la stessa topologia, il carico ridotto.
#
#   ./run_smoke.sh [ordine] [prefisso]
#
# Cosa resta IDENTICO alla prova completa, perche' e' cio' che lo smoke deve
# esercitare:
#   - otto core e quattro gibibyte per stack;
#   - otto processi per stack: otto worker del pool, otto worker di Gunicorn;
#   - un utente, una richiesta al secondo, ritmo per utente e non globale;
#   - la stessa guardia di latenza, con la stessa soglia e le stesse quindici
#     valutazioni consecutive;
#   - i due stack in sequenza, mai insieme;
#   - la certificazione della page-class cache e quella dei setpoint vivi.
#
# Cosa e' RIDOTTO:
#   - sedici utenti invece di centoventi, DUE per worker invece di quindici;
#   - otto in pausa invece di cinquanta;
#   - finestre, pausa, rientro e osservazione accorciati.
#
# Il carico ridotto non deve dire nulla sulla capacita': serve a dimostrare che
# gli strumenti girano, che il journal si scrive, che il classificatore legacy
# nomina gli otto worker, e che non resta nulla in piedi alla fine.
set -u

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export PLAN="${PLAN:-$SCENARIO_DIR/traces/cycle_smoke_plan.json}"
export GNR_ASGI_WORKER_MAX_USERS="${GNR_ASGI_WORKER_MAX_USERS:-2}"
export LEGACY_WORKERS="${LEGACY_WORKERS:-8}"
export EXPECT_WORKERS="${EXPECT_WORKERS:-8}"
export CYCLE_MEM_LIMIT="${CYCLE_MEM_LIMIT:-4g}"
export MEMORY_THRESHOLD="${MEMORY_THRESHOLD:-80}"

exec "$SCENARIO_DIR/run_cycle.sh" "${1:-legacy,bridge}" "${2:-e8csmoke}"
