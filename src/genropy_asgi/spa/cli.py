# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""CLI entry point: serve one GenroPy instance as a SPA — no daemon.

``gnrasgiserve <instance>`` resolves the instance name to its filesystem path, then starts a
standard genro-asgi ``AsgiServer`` from the fixed ``config.py`` recipe, whose only variable
element is that path (passed via the environment). The recipe mounts a single
``GenropySpaApplication`` on the root; auth and session stay inside the legacy GnrWsgiSite,
not the asgi layer. The pool always runs and sizes itself: there is no worker
count to declare and no single/pool selector.

The register is served ENTIRELY in-process (``GenropyRegisterClient``): lifecycle
registries, datachanges (both channels), stores and locks live inside the workers.
The legacy ``gnr.web.daemon`` namespace is replaced through its entry-point gate:
genropy (PR #1070) overrides it only when ``GNR_DAEMON_PROVIDER`` names the
provider, so this command declares itself before anything imports the site
machinery. No external register daemon is contacted, started or required.

Name -> path resolution is the legacy GenroPy step and lives here (it uses ``gnr.*``); the
generic SPA model only ever sees a path.

Usage:
    gnrasgiserve test_invoice_pg
    gnrasgiserve test_invoice_pg -p 8000
    gnrasgiserve test_invoice_pg -H 0.0.0.0 -p 8080 --nodebug

``--fulldebug`` adds the werkzeug debugger to debug; debug alone no longer
brings it, so an error page that evaluates Python never appears by accident.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from genro_asgi import AsgiServer

CONFIG = Path(__file__).resolve().parent / "config.py"

# The gnr.web.daemon provider this package declares (pyproject entry point
# ``gnr.web:daemon``); genropy replaces the namespace only when asked by name.
DAEMON_PROVIDER = "genropy-asgi"


def resolve_instance_path(instance: str) -> str:
    """Resolve a GenroPy instance/site name to its filesystem path.

    If ``instance`` is already an existing path it is returned as-is; otherwise it is
    resolved through the GenroPy ``PathResolver`` (the legacy name->path step).
    """
    if os.path.isdir(instance):
        return os.path.abspath(instance)
    from gnr.app.pathresolver import PathResolver

    return PathResolver().site_name_to_path(instance)


def cmd_serve(argv: list[str]) -> int:
    """Resolve the instance path and start a standard AsgiServer hosting the SPA."""
    parser = argparse.ArgumentParser(prog="gnrasgiserve")
    parser.add_argument("instance", help="GenroPy instance/site name (or path)")
    parser.add_argument("-H", "--host", default=None)
    parser.add_argument("-p", "--port", type=int, default=None)
    parser.add_argument(
        "--reload",
        action="store_true",
        default=None,
        help="accepted for surface compatibility; the core server has no reloader",
    )
    parser.add_argument("--nodebug", action="store_true")
    parser.add_argument(
        "--fulldebug",
        action="store_true",
        help="debug AND the werkzeug debugger: the error page with a traceback "
        "and a console that evaluates Python in the process. Debug alone gives "
        "the SQL counters and the developer's extras without that page",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="server config.py (a ServerConfiguration) instead of the built-in recipe; "
        "the config carries the pool shape while the CLI instance still wins",
    )
    opts = parser.parse_args(argv)

    # The daemon override is gated on the explicit request (genropy #1070):
    # declared BEFORE the site machinery is imported, so gnr.web.daemon is
    # this package's in-process register and never the Pyro client.
    os.environ.setdefault("GNR_DAEMON_PROVIDER", DAEMON_PROVIDER)

    # The CLI instance always wins: it is written to the environment BEFORE the server is
    # built, so a --config that reads GNR_ASGI_PATH serves the instance named on the CLI.
    path = resolve_instance_path(opts.instance)
    os.environ["GNR_ASGI_PATH"] = path
    if opts.host:
        os.environ["GNR_ASGI_HOST"] = opts.host
    if opts.port:
        os.environ["GNR_ASGI_PORT"] = str(opts.port)
    if opts.nodebug:
        os.environ["GNR_ASGI_DEBUG"] = ""
    if opts.fulldebug:
        os.environ["GNR_ASGI_DEBUG"] = "1"
        os.environ["GNR_ASGI_DEBUGGER"] = "1"
    if os.environ.get("GNR_ASGI_WORKERS"):
        print(
            "GNR_ASGI_WORKERS is set but no longer read: the pool always runs "
            "and sizes itself (the worker count is a reading, not a setting)."
        )

    if opts.reload:
        print("--reload: the core server has no reloader; flag accepted and ignored.")
    config_path = opts.config or CONFIG
    server = AsgiServer(str(config_path))
    server.serve(host=opts.host, port=opts.port)
    return 0


def main() -> int:
    """Entry point for the gnrasgiserve command."""
    try:
        return cmd_serve(sys.argv[1:])
    except KeyboardInterrupt:
        print("\nShutdown.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
