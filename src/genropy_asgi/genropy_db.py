# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Legacy GenroPy database for genro-asgi.

A ``database`` entry whose ``db_class`` is ``GenropyLegacyDb`` resolves its db
from a GenroPy instance name via ``GnrApp(instance).db`` — the legacy db that
already exposes ``execute``/``closeConnection`` and the rest of the GenroPy db
interface. ``GenropyDbHandler`` is the handler to pair with it.

Usage in a config.py recipe::

    from genropy_asgi.genropy_db import GenropyLegacyDb, GenropyDbHandler

    dbs = root.databases()
    dbs.database(code="default", db_class=GenropyLegacyDb,
                 db_handler_class=GenropyDbHandler, instance="invoice")

The ``gnr`` import is local to ``__init__``, so this module imports cleanly
even where the legacy framework is not installed; a missing instance fails at
boot when the db is built, not at import time.
"""

from __future__ import annotations

from typing import Any

from genro_asgi import AsgiDbHandlerBase


class GenropyLegacyDb:
    """Thin proxy over a legacy GenroPy db resolved from an instance name.

    ``instance`` names the GenroPy instance; the real db is ``GnrApp(instance).db``.
    Every attribute is proxied to it; the ``closeConnection`` contract is added
    by the handler that wraps this object.
    """

    def __init__(self, instance: str, **params: Any) -> None:
        from gnr.app.gnrapp import GnrApp

        self._db = GnrApp(instance).db

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._db, name)


class GenropyDbHandler(AsgiDbHandlerBase):
    """Handler for a legacy GenroPy db. ``closeConnection`` is inherited:
    the legacy db exposes it, so the base delegation is enough."""
