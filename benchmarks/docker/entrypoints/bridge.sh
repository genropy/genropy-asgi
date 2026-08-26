#!/bin/bash
# The bridge stack: genropy + genro-asgi + genropy-asgi from the mounted
# trees, then the SPA pool. Knobs arrive from the compose environment
# (GNR_ASGI_WORKER_MAX_USERS, GNR_ASGI_INSPECTOR, GNR_DAEMON_PROVIDER).
set -e
# setuptools writes egg-info into the source tree, and the mount is
# read-only: install from a throwaway copy instead.
uv pip install --system --quiet /src/genro-asgi /src/genropy-asgi
exec gnrasgiserve bridge_lab -H 0.0.0.0 -p 8098 --nodebug
