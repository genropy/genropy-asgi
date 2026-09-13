#!/usr/bin/env bash
set -euo pipefail
kind=diff
[ "$(date -u +%u)" = 7 ] && kind=full
docker exec --user postgres genro-asgi-portal-db-1 \
    pgbackrest --stanza=benchmark check
docker exec --user postgres genro-asgi-portal-db-1 \
    pgbackrest --stanza=benchmark --type="$kind" backup
