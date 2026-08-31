#!/bin/bash
# Genera i piani di uno scenario UNA VOLTA per campagna, e li certifica.
#
#   make_plans.sh <directory dello scenario> [--force]
#
# I piani non sono versionati: pesano megabyte e si rigenerano identici dalla
# specifica `plans.spec.json`, che porta il seed, gli argomenti esatti e l'hash
# atteso di ognuno.
#
# UNA VOLTA PER CAMPAGNA, e non una per stack. Se un piano esiste gia', questo
# script lo LASCIA STARE e verifica soltanto il suo hash: rigenerarlo per la
# seconda gamba sarebbe l'unico modo di far leggere due file diversi alle due
# gambe, ed e' esattamente la cosa che la specifica esiste per impedire.
# `--force` rigenera, e serve solo quando si cambia la specifica.
#
# Un hash diverso da quello dichiarato ferma tutto: significa che il generatore o
# una delle sue dipendenze e' cambiata, e i numeri delle campagne precedenti non
# sarebbero piu' confrontabili.
set -u

SCENARIO="${1:?serve la directory dello scenario}"
FORCE="${2:-}"
SCENARIO="$(cd "$SCENARIO" && pwd)" || exit 9
SPEC="$SCENARIO/plans.spec.json"
[ -f "$SPEC" ] || { echo "STOP: specifica assente: $SPEC" >&2; exit 9; }

GENERATOR="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['generator'])" "$SPEC")"
[ -f "$SCENARIO/$GENERATOR" ] || { echo "STOP: generatore assente: $SCENARIO/$GENERATOR" >&2; exit 9; }
mkdir -p "$SCENARIO/traces"

FAILED=0
COUNT="$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))['plans']))" "$SPEC")"
INDEX=0
while [ "$INDEX" -lt "$COUNT" ]; do
  NAME="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['plans'][int(sys.argv[2])]['name'])" "$SPEC" "$INDEX")"
  WANT="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['plans'][int(sys.argv[2])]['sha256'])" "$SPEC" "$INDEX")"
  SEED="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['plans'][int(sys.argv[2])]['seed'])" "$SPEC" "$INDEX")"
  OUT="$SCENARIO/traces/$NAME"
  if [ -f "$OUT" ] && [ "$FORCE" != "--force" ]; then
    echo "  $NAME: esiste gia', non lo rigenero"
  else
    # shellcheck disable=SC2046
    ( cd "$SCENARIO" && python3 "$GENERATOR" --out "traces/$NAME" --seed "$SEED" \
        $(python3 -c "import json,sys; print(' '.join(json.load(open(sys.argv[1]))['plans'][int(sys.argv[2])]['args']))" "$SPEC" "$INDEX") \
        > /dev/null ) || { echo "  $NAME: GENERAZIONE FALLITA" >&2; FAILED=1; INDEX=$((INDEX + 1)); continue; }
    echo "  $NAME: generato"
  fi
  GOT="$(python3 -c "
import hashlib,sys
print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$OUT")"
  if [ "$GOT" = "$WANT" ]; then
    echo "    sha256 atteso: OK  ${GOT:0:16}..."
  else
    echo "    sha256 DIVERSO da quello dichiarato" >&2
    echo "      atteso  $WANT" >&2
    echo "      ottenuto $GOT" >&2
    echo "    Il generatore o una sua dipendenza e' cambiata: i numeri delle" >&2
    echo "    campagne precedenti non sarebbero piu' confrontabili." >&2
    FAILED=1
  fi
  printf '%s  %s\n' "$GOT" "$NAME" > "$OUT.sha256"
  INDEX=$((INDEX + 1))
done

if [ "$FAILED" != "0" ]; then
  echo "STOP: almeno un piano non corrisponde alla specifica" >&2
  exit 6
fi
echo "tutti i piani di $(basename "$SCENARIO") sono generati e certificati"
