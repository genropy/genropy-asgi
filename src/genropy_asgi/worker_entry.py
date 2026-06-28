# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Entry point for a single legacy-GenroPy worker.

``python -m genropy_asgi.worker_entry <site> -p <port>`` runs one GenroPy site
as a minimal ASGI worker: a ``GenropyProxy`` (ASGI->WSGI bridge, executes the
GnrWsgiSite in a thread via smartasync) mounted on a ``GenroAsgiWorker`` (the
minimal single-app server). This is the unit the commander spawns; it is the
executor, not the orchestrator.

Usage:
    python -m genropy_asgi.worker_entry test_invoice_pg -p 8095
    python -m genropy_asgi.worker_entry test_invoice_pg -p 8095 --nodebug
"""

from __future__ import annotations

import argparse
import os
import sys

from genro_asgi import GenroAsgiWorker

from .genropy_proxy import GenropyProxy


def cmd_serve(argv: list[str]) -> int:
    """Run one GenroPy site as a minimal ASGI worker."""
    parser = argparse.ArgumentParser(prog="genropy-asgi-worker")
    parser.add_argument("site_name")
    parser.add_argument("-H", "--host", default="127.0.0.1")
    parser.add_argument("-p", "--port", type=int, default=8000)
    parser.add_argument("--name", default=None, help="worker name (alfa, pool_01, ...)")
    parser.add_argument("--group", default=None, help="worker group (welcome, pool, ...)")
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help="dispatch thread-pool size (default: GENRO_WORKER_THREADS or min(32, cpu+4))",
    )
    parser.add_argument("--nodebug", action="store_true")
    opts = parser.parse_args(argv)

    max_workers = opts.threads
    if max_workers is None and os.environ.get("GENRO_WORKER_THREADS"):
        max_workers = int(os.environ["GENRO_WORKER_THREADS"])

    # The worker's identity, read by the site register client to tag the events
    # it re-emits to the commander (the register threads don't know who they are).
    if opts.name:
        os.environ["GENRO_WORKER_NAME"] = opts.name
    if opts.group:
        os.environ["GENRO_WORKER_GROUP"] = opts.group

    proxy = GenropyProxy(site_name=opts.site_name, debug=not opts.nodebug)
    worker = GenroAsgiWorker(proxy, host=opts.host, port=opts.port, max_workers=max_workers)
    worker.run()
    return 0


def main() -> int:
    """Entry point for the worker process."""
    try:
        return cmd_serve(sys.argv[1:])
    except KeyboardInterrupt:
        print("\nShutdown.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
