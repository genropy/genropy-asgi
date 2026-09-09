# Copyright 2025-2026 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0
"""Build an explicitly selected genro-asgi checkout for the online reader."""
import argparse
import os
from pathlib import Path

from mkdocs.commands.build import build
from mkdocs.config import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkout', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    checkout, output = args.checkout.resolve(), args.out.resolve()
    if output.exists():
        parser.error('output must be a new directory')
    if output == checkout or checkout.is_relative_to(output) or output.is_relative_to(checkout):
        parser.error('output must be outside the source checkout')
    os.chdir(checkout)
    config = load_config('mkdocs.yml', strict=True, site_dir=str(output),
                         site_url='https://genroasgi.genropy.net/internals/')
    build(config)


if __name__ == '__main__':
    main()
