# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""genropy-spa — GenroPy legacy bridge for the genro-spa SPA model (collaudo).

A ``GenropySpaApplication`` is a ``genro_asgi.applications.spa_application.SpaApplication`` whose hosted app is a GenroPy
``GnrWsgiSite``. The only ``gnr.*``-aware piece; everything generic comes from genro_asgi.applications.spa_application.
"""

from .genropy_spa_application import GenropySpaApplication
from .genropy_worker_application import GenropyWorkerApplication

__all__ = ["GenropySpaApplication", "GenropyWorkerApplication"]
__version__ = "0.1.0"
