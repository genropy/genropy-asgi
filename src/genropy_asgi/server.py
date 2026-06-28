# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyAsgiServer — AsgiServer identity for GenroPy deployments.

A thin marker subclass of AsgiServer. It boots from a ``config.py`` like any
AsgiServer; mounting a GenroPy site and registering its databases is done in
the recipe (a GenropyProxy app, database connections passed as kwargs), not
here. The subclass exists so a GenroPy deployment has its own server type.
"""

from __future__ import annotations

from genro_asgi import AsgiServer


class GenropyAsgiServer(AsgiServer):
    """AsgiServer used for GenroPy deployments. Behaviour is the base one."""
