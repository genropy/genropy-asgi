# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""genropy-spa — GenroPy legacy bridge on the genro-asgi SPA core.

A :class:`GenropySpaApplication` is the core ``SpaApplication`` whose pool
workers are :class:`~genropy_asgi.spa.genropy_worker.GenropyWorker` instances,
each hosting a legacy ``GnrWsgiSite`` behind the core's ``wsgi_app`` seam.
The worker side is reached by dotted path (``worker_class``), so importing
this package never requires GenroPy.
"""

from .genropy_spa_application import GenropySpaApplication

__all__ = ["GenropySpaApplication"]
__version__ = "0.1.0"
