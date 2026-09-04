#!/bin/bash
# The bridge stack: genropy + genro-asgi + genropy-asgi from the mounted
# trees, then the SPA pool. Knobs arrive from the compose environment
# (GNR_ASGI_WORKER_MAX_USERS, GNR_ASGI_INSPECTOR, GNR_DAEMON_PROVIDER).
set -e
# setuptools writes egg-info into the source tree, and the mount is
# read-only: install from a throwaway copy instead.
uv pip install --system --quiet /src/genro-asgi /src/genropy-asgi

# The monitor's two secrets arrive as a mounted FILE, never as compose values:
# a value would end up in the rendered compose, in `docker inspect` and in the
# campaign's manifest. The file is read as DATA — base64 payloads behind fixed
# names, decoded without eval and without sourcing it — so a password holding a
# space, a `#`, a `$`, a quote or a backslash arrives as itself. Missing,
# malformed or empty: no server. The certificate redacts them by name out of
# /proc/1/environ.
. /lab/entrypoints/read_monitor_secrets.sh
read_monitor_secrets /lab/monitor.secrets.env || exit 1
mkdir -p "${GNR_ASGI_MONITOR_STORAGE:?}"

# GNR_ASGI_POOL_RECIPE names an alternative server config (a ServerConfiguration
# file); unset, the package's built-in recipe serves as always.
if [ -n "${GNR_ASGI_POOL_RECIPE:-}" ]; then
    exec gnrasgiserve bridge_lab -H 0.0.0.0 -p 8098 --nodebug --config "$GNR_ASGI_POOL_RECIPE"
fi
exec gnrasgiserve bridge_lab -H 0.0.0.0 -p 8098 --nodebug
