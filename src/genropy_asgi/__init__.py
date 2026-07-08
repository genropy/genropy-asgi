# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""genropy-asgi — the bridge between genro-asgi and legacy GenroPy.

The first commander/worker attempt was archived: that model is now the core's
(``genro_asgi.applications.multi_worker_application``). The GenroPy bridge is being
rebuilt on top of it — the in-process siteregister and the SPA worker roles. This
package is intentionally empty until that lands.
"""

__version__ = "0.1.0"
