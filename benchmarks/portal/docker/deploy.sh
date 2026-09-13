#!/usr/bin/env bash
# Run on the portal host from its deployment directory, with .env already set.
# Uses immutable sha256 image digests; never touches benchmark workload projects.
set -euo pipefail
cd "$(dirname "$0")"
image="${1:?Pass the immutable registry image@sha256:digest}"
[[ "$image" =~ ^ghcr.io/genropy/genropy-asgi-portal@sha256:[0-9a-f]{64}$ ]] || exit 2
previous="$(cat current-image 2>/dev/null || true)"
export PORTAL_IMAGE="$image"
docker compose pull portal
docker compose up -d --wait db
# Version 1 is additive only; future destructive migrations need a separate process.
docker compose run --rm --no-deps portal python -m benchmarks.portal.store init
if ! docker compose up -d --no-build --wait --wait-timeout 90 portal; then
  if [ -n "$previous" ]; then
    export PORTAL_IMAGE="$previous"
    docker compose up -d --no-build --wait --wait-timeout 90 portal
  fi
  exit 1
fi
printf '%s\n' "$image" > current-image
