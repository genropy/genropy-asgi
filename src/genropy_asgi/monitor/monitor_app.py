# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""MonitorApp — a one-page live dashboard of the MAIN server.

Mounted on the MAIN server on its own mount (e.g. ``/_monitor``), it serves a
single ``WsLivePage`` that draws a tree of bordered boxes: the server box, a box
per mounted app, and inside each app box its own info sub-branch. The page
declares ``live_interval`` so the server re-reads the state and pushes the
refreshed tree over the websocket — no client request.

First draft: the tree is rebuilt in ``tick()`` from what the MAIN already knows
(its apps, and — for the commander — its workers and per-user registry). Deeper
detail (each worker's own connection register, queried over HTTP) comes later.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from genro_ws_web.application import WsLiveApp
from genro_ws_web.page import WsLivePage

PAGE_TITLE = "Server monitor"


class MonitorPage(WsLivePage):
    """A live view of the server and its mounted apps.

    First draft: the whole state is rendered as one preformatted text block bound
    to a single data pointer, refreshed by the server every ``live_interval`` and
    pushed over the websocket. Bordered boxes / a real tree come next, once the
    live flow is confirmed.
    """

    live_interval = 2.0

    def setup(self, data: Any) -> None:
        """Fill the state once for the first paint."""
        self.set_data("monitor.text", self._state_text())

    def main(self, root: Any) -> None:
        """A titled pane with a <pre> bound to the live state text."""
        pane = root.div(class_="monitor")
        pane.h1("Server monitor")
        pane.pre("^monitor.text", class_="monitor-text")

    def tick(self) -> None:
        """Re-read the state and push it: the bound <pre> updates itself."""
        self.set_data("monitor.text", self._state_text())

    # -- state -> text -------------------------------------------------

    def _state_text(self) -> str:
        """Render the server + its mounted apps as an indented text tree."""
        application = self.application
        server = getattr(application, "server", None) if application else None
        if server is None:
            return "SERVER  (not mounted yet)"
        host = getattr(server, "host", "?") or "?"
        port = getattr(server, "port", "?")
        stamp = datetime.now().strftime("%H:%M:%S")
        lines = [f"SERVER  {host}:{port}    (updated {stamp})"]
        apps = dict(getattr(server, "apps", {}) or {})
        for mount, app in apps.items():
            label = mount or "(root)"
            lines.append(f"  APP  /{label}   [{type(app).__name__}]")
            for info in self._app_info(app):
                lines.append(f"      {info}")
        return "\n".join(lines)

    def _app_info(self, app: Any) -> list[str]:
        """The info lines an app contributes to its sub-branch.

        Generic apps report their protocol; the commander additionally reports
        its workers (group, host:port, status) and the per-user registry.
        """
        lines = [f"protocol: {getattr(app, 'app_protocol', '?')}"]
        orchestrator = getattr(app, "orchestrator", None)
        if orchestrator is not None:
            allocs = orchestrator.allocations()
            worker_metrics = getattr(app, "worker_metrics", {}) or {}
            lines.append(f"workers: {len(allocs)}")
            for alloc in allocs:
                lines.append(
                    f"  - {alloc.id} [{alloc.group}] "
                    f"{alloc.host}:{alloc.port} {alloc.status}"
                )
                m = worker_metrics.get(alloc.id)
                if m:
                    lines.append(
                        f"      occupancy {m['occupancy']:.0%} · "
                        f"busy={m['busy']}/{m['total']} · queue={m['queue_depth']}"
                    )
            registry = getattr(app, "user_registry", {}) or {}
            lines.append(f"users: {len(registry)}")
            for user, entry in registry.items():
                lines.append(f"  - {user} -> {entry['worker']}")
        return lines


class MonitorApp(WsLiveApp):
    """WsLiveApp serving only the monitor page (no package menu)."""

    def on_init(self, **kwargs: Any) -> None:
        """Set up a single-page app: only the monitor page, no menu discovery.

        Replaces WsLiveApp.on_init (which discovers pages from a package menu)
        with the minimum the live cycle needs: the page registry, the live-pages
        map, the deferred event loop and a client cache-buster.
        """
        self.pages = {"index": (PAGE_TITLE, MonitorPage)}
        self.live_pages = {}
        self.loop = None
        self.client_version = uuid4().hex[:8]


if __name__ == "__main__":
    pass
