# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""genropy-spa — GenroPy legacy bridge on the genro-asgi SPA core.

The bridge is being rebased onto the core ``SpaApplication``/
``UserStickyWorker`` pair (genro-asgi >= 0.30). The worker side lives in
``genropy_asgi.spa.genropy_worker`` (reached by dotted path, so importing
this package never requires GenroPy); the front application returns to this
namespace when its rewrite lands. The pre-rebase application modules import
core paths that no longer exist and are not re-exported here.
"""

__all__: list[str] = []
__version__ = "0.1.0"
