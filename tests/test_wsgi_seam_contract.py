# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Bridge contract of ``WsgiSeam``, the core's WSGI endpoint.

The core owns framing and HTTP records; the bridge owns the requirement that
their endpoint reconstructs the exact legacy environ and answer: cookies
joined with ``; ``, repeated headers joined with ``,``, the query string in
latin-1, ``SCRIPT_NAME``/``PATH_INFO``, a binary reply with every header.
These tests pass identically on the core before and after the two-envelope
frame of genro-asgi #72: they are a contract of the seam, not a proof of the
transport.
"""

from __future__ import annotations

from typing import Any

from genro_asgi_multiworker_spa.environ import WsgiSeam


class EndpointWorker:
    """The traffic-pool surface ``WsgiSeam`` requires."""

    async def run_sync(self, action: Any) -> Any:
        return action()


class RecordingSite:
    """A legacy WSGI endpoint that records its environ and returns raw bytes."""

    def __init__(self) -> None:
        self.environ: dict[str, Any] = {}

    def __call__(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
        self.environ = environ
        start_response(
            "201 Created",
            [
                ("Content-Type", "application/octet-stream"),
                ("Set-Cookie", "first=1; Path=/"),
                ("Set-Cookie", "second=2; Path=/"),
            ],
        )
        return [b"\x00reply\xff"]


async def serve_site(site: RecordingSite) -> list[dict[str, Any]]:
    """Call the public WSGI seam with the facts issue72 must preserve."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/rpc",
        "root_path": "",
        "query_string": b"raw=\xff&raw=two",
        "headers": [
            (b"host", b"legacy.example:8443"),
            (b"cookie", b"site=abc"),
            (b"cookie", b"feature=on"),
            (b"x-repeat", b"first"),
            (b"x-repeat", b"second"),
            (b"content-type", b"application/octet-stream"),
        ],
        "server": ("legacy.example", 8443),
        "client": ("127.0.0.8", 51234),
        "scheme": "https",
        "genro.identity": "alice",
        "genro.page_id": "page-1",
        "genro.reply_path": "/rpc/reply",
    }
    messages = [
        {"type": "http.request", "body": b"\x00request", "more_body": True},
        {"type": "http.request", "body": b"\xff", "more_body": False},
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await WsgiSeam(site, EndpointWorker())(scope, receive, send)
    return sent


async def test_opaque_http_reconstructs_the_legacy_environ() -> None:
    site = RecordingSite()
    await serve_site(site)

    environ = site.environ
    assert environ["wsgi.input"].read() == b"\x00request\xff"
    assert environ["QUERY_STRING"].encode("latin-1") == b"raw=\xff&raw=two"
    assert environ["HTTP_COOKIE"] == "site=abc; feature=on"
    assert environ["HTTP_X_REPEAT"] == "first,second"
    assert environ["CONTENT_TYPE"] == "application/octet-stream"
    assert environ["genro.identity"] == "alice"
    assert environ["genro.page_id"] == "page-1"
    assert environ["genro.reply_path"] == "/rpc/reply"
    assert (environ["SCRIPT_NAME"], environ["PATH_INFO"]) == ("", "/rpc")


async def test_opaque_http_preserves_binary_reply_and_duplicate_cookies() -> None:
    site = RecordingSite()
    sent = await serve_site(site)

    assert sent[0]["status"] == 201
    assert [value for name, value in sent[0]["headers"] if name.lower() == b"set-cookie"] == [
        b"first=1; Path=/",
        b"second=2; Path=/",
    ]
    assert sent[1]["body"] == b"\x00reply\xff"
