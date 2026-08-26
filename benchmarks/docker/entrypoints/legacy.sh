#!/bin/bash
# The legacy stack, whole: genropy is already in the image; start the
# register daemon, wait for its descriptor, then run gunicorn via serveprod.
set -e
rm -f /lab/projects/lab_bench/instances/legacy_lab/site/sitedaemon.xml \
      /lab/projects/lab_bench/instances/legacy_lab/site/*.pik
gnrdaemon legacy_lab &
DESCRIPTOR=/lab/projects/lab_bench/instances/legacy_lab/site/sitedaemon.xml
for _ in $(seq 1 60); do [ -f "$DESCRIPTOR" ] && break; sleep 1; done
[ -f "$DESCRIPTOR" ] || { echo "sitedaemon.xml never appeared"; exit 1; }
exec python -m gnr.web.cli.gnrserveprod legacy_lab \
    -b 0.0.0.0:8099 -w "${LEGACY_WORKERS:-1}" -k gthread --threads 16
