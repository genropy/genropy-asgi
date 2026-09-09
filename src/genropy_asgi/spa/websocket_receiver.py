"""Adapt WSK calls to the ordinary ephemeral-page RPC lifecycle."""

import io
import logging
import os
from urllib.parse import parse_qsl, urlencode

from genro_tytx import from_tytx, to_tytx


log = logging.getLogger("genropy_asgi.websocket")


class WebSocketReceiver:
    """Pass HTTP through; retain the probe route and adapt page RPC calls."""

    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        if environ.get("REQUEST_METHOD") != "WSK":
            return self.application(environ, start_response)
        page_id = environ.get("genro.page_id")
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            payload = from_tytx(environ["wsgi.input"].read(length).decode(), "json")
            if not page_id or not isinstance(payload, dict):
                raise ValueError("a page and a call object are required")
            if environ.get("PATH_INFO") != "/_websocket_receive":
                rpc_environ = self.rpc_environ(environ, payload, page_id)
                return self.rpc_response(rpc_environ, start_response)
            method = payload.get("method")
            if not isinstance(method, str) or not method or len(method) > 256:
                raise ValueError("a method name is required")
            parameters = payload.get("parameters", {})
            if not isinstance(parameters, dict):
                raise ValueError("parameters must be an object")
        except (ValueError, TypeError, UnicodeError) as error:
            status = "400 Bad Request"
            result = {"error": str(error), "executed": False}
        else:
            log.info("WSK received page=%s method=%r parameter_names=%r pid=%s executed=false",
                     page_id, method, sorted(parameters), os.getpid())
            status = "200 OK"
            result = {"received": True, "executed": False, "page_id": page_id,
                      "method": method, "worker_pid": os.getpid()}
        body = to_tytx(result, "json").encode()
        start_response(status, [("Content-Type", "application/json"),
                                ("Content-Length", str(len(body)))])
        return [body]

    def rpc_environ(self, environ, payload, page_id):
        """Translate a serialized legacy form without accepting another page ID."""
        form = payload.get("form")
        if not isinstance(form, str):
            raise ValueError("form must contain serialized RPC parameters")
        pairs = parse_qsl(form, keep_blank_values=True)
        parameters = dict(pairs)
        if len(parameters) != len(pairs):
            raise ValueError("duplicate RPC parameters are not supported")
        method = parameters.get("method")
        if not method or len(method) > 4096:
            raise ValueError("a method name is required")
        if "rpc" in parameters or "_plugin" in parameters:
            raise ValueError("only ordinary page RPC dispatch is supported")
        if parameters.get("page_id", page_id) != page_id:
            raise ValueError("RPC page_id differs from its channel")
        if parameters.get("mode", "bag") not in ("bag", "json"):
            raise ValueError("only bag and json RPC results are supported")
        parameters["page_id"] = page_id
        body = urlencode(parameters).encode()
        converted = dict(environ)
        converted.update(REQUEST_METHOD="POST", QUERY_STRING="",
                         CONTENT_TYPE="application/x-www-form-urlencoded",
                         CONTENT_LENGTH=str(len(body)))
        converted["wsgi.input"] = io.BytesIO(body)
        scheme = converted.get("wsgi.url_scheme", "http")
        converted["wsgi.url_scheme"] = {"ws": "http", "wss": "https"}.get(scheme, scheme)
        converted["genro.transport"] = "websocket"
        log.info("WSK RPC page=%s method=%r pid=%s", page_id, method, os.getpid())
        return converted

    def rpc_response(self, environ, start_response):
        """Run the normal application and carry its textual response unchanged."""
        # Iteration occurs outside input validation, so application failures keep
        # their normal exception handling rather than becoming malformed-input errors.
        def response():
            collected = []
            status_headers = []

            def capture(status, headers, exc_info=None):
                if exc_info and status_headers:
                    raise exc_info[1].with_traceback(exc_info[2])
                status_headers[:] = [status, headers]
                return collected.append

            iterable = self.application(environ, capture)
            try:
                collected.extend(iterable)
            finally:
                close = getattr(iterable, "close", None)
                if close:
                    close()
            status, headers = status_headers
            payload = {"body": b"".join(collected).decode("utf-8"),
                       "headers": {key.lower(): value for key, value in headers
                                   if key.lower() == "content-type"
                                   or key.lower().startswith("x-gnr")}}
            body = to_tytx(payload, "json").encode()
            start_response(status, [("Content-Type", "application/json"),
                                    ("Content-Length", str(len(body)))])
            yield body

        return response()
