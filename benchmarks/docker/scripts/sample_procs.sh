#!/bin/bash
# Stream the process table of ONE lab container, one line per process per
# sample, read from /proc inside the container (the image has no ps).
# Cumulative cpu jiffies (utime+stime) ride each row; the report computes the
# deltas. rss is kilobytes as /proc reports it.
#
#   ./scripts/sample_procs.sh runtime/<name>_procs.csv [container] [seconds] &
#   ... run the driver ...
#   kill %1
OUT="${1:?usage: sample_procs.sh <out.csv> [container] [interval_seconds]}"
CONTAINER="${2:-genro-bench-lab-bridge-1}"
EVERY="${3:-2}"
echo "wall,pid,ppid,rss_kb,cpu_jiffies,starttime_jiffies,comm" > "$OUT"
while true; do
    docker exec -i "$CONTAINER" python3 - <<'PYEOF' 2>/dev/null >> "$OUT"
import os, time
now = int(time.time())
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    try:
        with open(f"/proc/{entry}/stat") as handle:
            row = handle.read()
        with open(f"/proc/{entry}/status") as handle:
            status = handle.read()
    except OSError:
        continue
    comm = row[row.index("(") + 1 : row.rindex(")")].replace(",", "_")
    fields = row[row.rindex(")") + 2 :].split()
    ppid, utime, stime, start = fields[1], fields[11], fields[12], fields[19]
    rss_kb = 0
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            rss_kb = int(line.split()[1])
            break
    print(f"{now},{entry},{ppid},{rss_kb},{int(utime) + int(stime)},{start},{comm}")
PYEOF
    sleep "$EVERY"
done
