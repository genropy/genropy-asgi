#!/bin/bash
# Rebuild the report pages from the CSV files of a Hetzner run.
# Usage: build_report.sh <csv-dir> <site-dir>
# <csv-dir> holds the session_bench output of both stacks plus the container
# samples; <site-dir> is the report tree whose run_* folders keep a copy of the
# CSV files beside each page.
set -e
CSV=$(cd "$1" && pwd)
SITE=$(cd "$2" && pwd)
GEN=$(cd "$(dirname "$0")/../.." && pwd)/session_report.py

# one stats file covering every window: the memory strip picks its own slice
STATS=$CSV/stats_all.csv
head -1 "$CSV/full_stats.csv" > "$STATS"
for f in "$CSV"/*stats*.csv; do
    [ "$f" = "$STATS" ] && continue
    tail -n +2 "$f" >> "$STATS"
done

place () {  # $1=name $2=run folder
    cp "$CSV/$1_calls.csv" "$CSV/$1_seconds.csv" "$SITE/$2/"
}

tour () {  # $1=stem $2=users $3=folder $4=page $5=label
    place "legacy_$1" "$3"; place "bridge_$1" "$3"
    python3 "$GEN" tour \
        --baseline "$CSV/legacy_hbase" "$CSV/bridge_hbase" \
        --run "$CSV/legacy_$1" "$CSV/bridge_$1" \
        --users "$2" --label "$5" --here "${3#$SITE/}/$4" \
        --out "$SITE/$3/$4"
}

churn () {  # $1=stem $2=peak $3=folder $4=page $5=entries_legacy $6=entries_bridge
    place "legacy_$1" "$3"; place "bridge_$1" "$3"
    python3 "$GEN" churn \
        --run "$CSV/legacy_$1" "$CSV/bridge_$1" \
        --peak "$2" --stats "$STATS" --entries "$5" "$6" \
        --here "$3/$4" --out "$SITE/$3/$4"
}

place legacy_hbase tipo1/run_base; place bridge_hbase tipo1/run_base
python3 "$GEN" tour \
    --baseline "$CSV/legacy_hbase" "$CSV/bridge_hbase" \
    --run "$CSV/legacy_hbase" "$CSV/bridge_hbase" \
    --users 1 --label "one user" --here tipo1/run_base/baseline.html \
    --out "$SITE/tipo1/run_base/baseline.html"
tour h8u  8  tipo1/run_8  eight_users.html      "eight users"
tour h16u 16 tipo1/run_16 sixteen_users.html    "sixteen users"
tour h32u 32 tipo1/run_32 thirtytwo_users.html  "thirty-two users"
churn churn16 16 tipo2/run_16 churn_16.html 36 36
churn churn32 32 tipo2/run_32 churn_32.html 75 75
churn churn64 64 tipo2/run_64 churn_64.html 157 157
echo "report rebuilt in $SITE"
