# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""CLI entry point for the genropy-asgi server.

``gnrasgiserve <site_name>`` serves a single GenroPy site through the ASGI
lifecycle. The launch parameters are passed to the standard configuration
(genropy_config.py) via environment variables; that config mounts the site as
the main application through GenropyProxy.

Usage:
    gnrasgiserve test_invoice_pg
    gnrasgiserve test_invoice_pg -p 8000
    gnrasgiserve test_invoice_pg --host 0.0.0.0 --reload
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from genro_asgi import AsgiServer

CONFIG = Path(__file__).resolve().parent / "genropy_config.py"


def cmd_serve(argv: list[str]) -> int:
    """Serve a GenroPy site by name through the ASGI lifecycle."""
    parser = argparse.ArgumentParser(prog="gnrasgiserve")
    parser.add_argument("site_name")
    parser.add_argument("-H", "--host")
    parser.add_argument("-p", "--port", type=int)
    parser.add_argument("--reload", action="store_true", default=None)
    parser.add_argument("--nodebug", action="store_true")
    opts = parser.parse_args(argv)

    os.environ["GNR_ASGI_SITE"] = opts.site_name
    if opts.host:
        os.environ["GNR_ASGI_HOST"] = opts.host
    if opts.port:
        os.environ["GNR_ASGI_PORT"] = str(opts.port)
    if opts.nodebug:
        os.environ["GNR_ASGI_DEBUG"] = ""

    server = AsgiServer(CONFIG, host=opts.host, port=opts.port, reload=opts.reload)
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
