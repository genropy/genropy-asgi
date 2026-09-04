#!/bin/bash
# Stream the bridge pool census, one JSON line per sample: the group's
# user_worker_map, each worker's state/occupancy/pid — the placement view the
# per-second CSV cannot carry. Wall-stamped so it merges with the driver's CSV.
#
#   ./scripts/sample_census.sh runtime/<name>_census.jsonl [census_url] [seconds] &
#   ... run the driver ...
#   kill %1
OUT="${1:?usage: sample_census.sh <out.jsonl> [census_url] [interval_seconds]}"
URL="${2:-http://127.0.0.1:8098/_server/inspector/census}"
EVERY="${3:-3}"
: > "$OUT"
while true; do
    curl -s -m 4 "$URL" | python3 -c '
import json, sys, time
try:
    census = json.load(sys.stdin)
except Exception:
    sys.exit(0)
front = next(iter(census.values()))
groups = front.get("groups", {})
row = {"wall": int(time.time()), "groups": {}}
for name, group in groups.items():
    row["groups"][name] = {
        "user_worker_map": group.get("user_worker_map", {}),
        "workers": group.get("workers", {}),
        "memory_occupied_percent": group.get("memory_occupied_percent"),
    }
row["worker_pid"] = {
    name: worker.get("pid") for name, worker in front.get("workers", {}).items()
    if isinstance(worker, dict)
}
print(json.dumps(row))
' >> "$OUT"
    sleep "$EVERY"
done
