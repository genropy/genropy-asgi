# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Server monitor — a live dashboard of the MAIN server state.

A WsLiveApp (genro-ws-web) mounted on the MAIN server, serving one reactive
page: a tree of bordered boxes — the server, the apps mounted on it, and, for
each app, its own info sub-branch. Kept alive over the websocket (live_interval),
so the picture refreshes on its own.
"""

from .monitor_app import MonitorApp

__all__ = ["MonitorApp"]
