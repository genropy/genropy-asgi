# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""CLI entry point: serve one GenroPy instance as a single-process SPA — no daemon.

``gnrasgiserve <instance>`` resolves the instance name to its filesystem path, then starts a
standard genro-asgi ``AsgiServer`` from the fixed ``config.py`` recipe, whose only variable
element is that path (passed via the environment). The recipe mounts a single
``GenropySpaApplication`` on the root; auth and session stay inside the legacy GnrWsgiSite,
not the asgi layer. Single process, no commander.

The register is served ENTIRELY in-process (``GenropyRegisterClient``): lifecycle
registries, datachanges (both channels), stores and locks live inside the application.
No external register daemon is contacted, started or required.

Name -> path resolution is the legacy GenroPy step and lives here (it uses ``gnr.*``); the
generic SPA model only ever sees a path.

With ``--workers N`` the same command serves the instance through a commander with a
pool of N worker subprocesses (sticky routing per user; the workers reach the commander
back-channel at its own address). Still no daemon.

Usage:
    gnrasgiserve test_invoice_pg
    gnrasgiserve test_invoice_pg -p 8000
    gnrasgiserve test_invoice_pg -H 0.0.0.0 -p 8080 --nodebug
    gnrasgiserve test_invoice_pg --workers 2
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from genro_asgi import AsgiServer

CONFIG = Path(__file__).resolve().parent / "config.py"


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
    parser.add_argument("--reload", action="store_true", default=None)
    parser.add_argument("--nodebug", action="store_true")
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="serve through a commander with N worker subprocesses (0 = single process)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="server config.py (a ServerConfiguration) instead of the built-in recipe; "
        "the config carries the shape (pool, caps) while the CLI instance still wins",
    )
    opts = parser.parse_args(argv)

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
    # With --config the config owns the pool shape (workers + caps): the CLI leaves
    # GNR_ASGI_WORKERS untouched. Without it, --workers selects single vs pool.
    if opts.config is None and opts.workers:
        os.environ["GNR_ASGI_WORKERS"] = str(opts.workers)

    config_path = opts.config or CONFIG
    server = AsgiServer(config_path, host=opts.host, port=opts.port, reload=opts.reload)
    server.run()
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
