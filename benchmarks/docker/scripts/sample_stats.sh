#!/bin/bash
# Stream cpu and memory of the lab's containers, one line per container per
# second, each stamped with the wall clock so it merges with the driver's CSV.
# The kernel reports these, not the instrument: nothing inside the stacks is
# asked anything.
#
#   ./scripts/sample_stats.sh runtime/<name>_stats.csv &
#   ... run the driver ...
#   kill %1
#
# `mem_mb` is MEBIBYTES, converted from whatever unit docker chose. Until
# 2026-08-29 the unit was thrown away instead — `sed 's|MiB.*||; s|GiB.*||'` —
# so 1.56 GiB was written as `1.56` in a column of megabytes, and every value
# under 50 in the p1/p2 files is really a gigabyte figure. The old files keep
# that defect: they are the evidence of those runs and are not rewritten. A
# unit this script does not know produces the word INVALID, never a number.
#
# The conversion alone, for the tests:  ./sample_stats.sh --convert 1.5GiB

to_mib () {
    # One docker size — number plus unit, no space — as mebibytes, or INVALID.
    # Fed through a variable and judged in BEGIN: reading it as input would
    # skip an EMPTY size silently, which is the one case that must not pass.
    awk -v size="$1" '
        BEGIN {
            if (match(size, /^[0-9]+(\.[0-9]+)?/) == 0) { print "INVALID"; exit }
            value = substr(size, 1, RLENGTH) + 0
            unit = substr(size, RLENGTH + 1)
            if      (unit == "B")             factor = 1 / 1048576
            else if (unit == "KiB")           factor = 1 / 1024
            else if (unit == "MiB")           factor = 1
            else if (unit == "GiB")           factor = 1024
            else if (unit == "TiB")           factor = 1048576
            else if (unit == "kB")            factor = 1000 / 1048576
            else if (unit == "MB")            factor = 1000000 / 1048576
            else if (unit == "GB")            factor = 1000000000 / 1048576
            else { print "INVALID"; exit }
            # Plain decimal, never scientific: a CSV column of megabytes must
            # be readable as a number by anything that opens it.
            text = sprintf("%.6f", value * factor)
            sub(/0+$/, "", text)
            sub(/\.$/, "", text)
            print text
        }'
}

if [ "${1:-}" = --convert ]; then
    to_mib "${2:-}"
    exit 0
fi

OUT="${1:?usage: sample_stats.sh <out.csv> | sample_stats.sh --convert <size>}"
echo "wall,container,cpu_pct,mem_mb" > "$OUT"
while true; do
    NOW=$(date +%s)
    docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' 2>/dev/null \
        | grep genro-bench-lab \
        | while IFS=, read -r name cpu mem; do
              # MemUsage reads "970.3MiB / 2GiB": the charge is the first field.
              used=$(printf '%s' "$mem" | awk '{print $1}')
              echo "$NOW,$name,${cpu%\%},$(to_mib "$used")"
          done >> "$OUT"
    sleep 1
done
