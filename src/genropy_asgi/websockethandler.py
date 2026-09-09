"""Initial legacy WebSocket facade; commands are intentionally not delivered."""


class WsgiWebSocketHandler:
    """Accept legacy setup without connecting to an async server."""

    client_module = "gnrwebsocket_asgi"

    def __init__(self, site):
        self.site = site

    def checkSocket(self):
        """Allow legacy WebSocket bootstrap without probing async.sock."""
        return True

    def sendCommandToPage(self, page_id, command, data):
        """Ignore commands until bridge delivery is implemented."""
        pass
