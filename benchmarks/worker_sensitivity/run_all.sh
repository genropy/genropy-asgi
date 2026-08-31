#!/bin/bash
# Le quattro corse della sensitivity, in sequenza. Alla prima che fallisce si
# ferma: proseguire su un laboratorio gia' storto produce misure senza valore.
#
# Variabili d'ambiente: WORK_DIR e LAB_DIR, passate a run_one.sh.
set -u

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${WORK_DIR:-$SCENARIO_DIR/runs}"
mkdir -p "$WORK_DIR" || exit 1
WORK_DIR="$(cd "$WORK_DIR" && pwd)"
export WORK_DIR

STATUS="$WORK_DIR/run_all_status.txt"
rm -f "$STATUS"

for spec in "W1 1 48" "W2 2 24" "W4 4 12" "W8 8 6"; do
  set -- $spec
  "$SCENARIO_DIR/run_one.sh" "$1" "$2" "$3" > "$WORK_DIR/$1_run.log" 2>&1
  rc=$?
  echo "$1 exit=$rc" >> "$STATUS"
  if [ $rc -ne 0 ]; then
    echo "FERMATA SU $1 (exit=$rc): le corse successive non sono state eseguite" >> "$STATUS"
    echo "FERMATA SU $1 (exit=$rc)" >&2
    exit $rc
  fi
done

echo "TUTTE COMPLETATE" >> "$STATUS"
