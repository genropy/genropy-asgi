"""HTTP recorder for the legacy/bridge comparison bench.

A WSGI middleware wrapping the site application. It mints an `exchange_id` for
every request, injects it into the request as the `X-Bench-Exchange-Id` header,
and appends one JSONL line per recorded exchange to `temp/http_trace.jsonl`.

That header is the seam between the two recorders. The register recorder reads
it back through `site.currentRequest.headers` — GenroPy's own per-thread
request (`GnrWsgiSite.currentRequest`, a `ThreadedDict` filled for the whole
dispatch, statics and `_ping` included) — and stamps every register call with
the exchange that caused it. Nothing else passes between the two recorders: no
shared state, no mutual import, only the name of a header. It also means the
join key is visible in the trace itself, among the recorded request headers.

Installation is a plain call, never logic living in a gunicorn hook, because
the bridge has no gunicorn and installs the same recorder the same way:

    worker.wsgi = HttpRecorder(worker.wsgi)

What carries no content: static assets, recognised by the response content type
(javascript, css, images, fonts) plus `favicon.ico`; and pings that rendered
nothing — the bare envelope, a null `result` and no `dataChanges`. Those get an
id-only STUB line — what the exchange was and why it was filtered, never a body
— because the register recorder stamps those exchanges too, and without the stub
their register calls would name an exchange this trace does not contain.
Everything else is recorded whole, with no truncation anywhere: a ping carrying
a datachange is a full record like any other, because that Bag is the register
answering.

A failure inside the recorder is written to the trace as `recorder_error` and
never reaches the response.
"""

import io
import json
import os
import re
import threading
import time
import urllib.parse
import uuid
from datetime import datetime

EXCHANGE_HEADER = "X-Bench-Exchange-Id"
EXCHANGE_ENVIRON_KEY = "HTTP_X_BENCH_EXCHANGE_ID"

TRACE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "temp", "http_trace.jsonl")

STATIC_CONTENT_TYPES = ("javascript", "text/css", "image/", "font")
XML_DECLARATION = re.compile(r"^<\?xml[^>]*\?>\s*")

# The ping that rendered nothing. `handle_ping` builds `Bag(dict(result=None))`
# and adds `dataChanges` only when there are changes to deliver
# (gnr/web/daemon/siteregister.py:928), so the bare envelope — a null `result`
# and nothing else — IS the empty answer on the wire.
EMPTY_PING_ANSWER = re.compile(
    r"^(<GenRoBag\s*/>"
    r"|<GenRoBag>\s*(<result\s+_T=\"NN\"\s*(/>|></result>))?\s*</GenRoBag>)$")


class HttpRecorder:
    """WSGI middleware writing one JSONL line per recorded HTTP exchange."""

    def __init__(self, application, trace_path=TRACE_PATH):
        self.application = application
        self.trace_path = trace_path
        self.lock = threading.Lock()
        self.trace = open(self.trace_path, "a", encoding="utf-8")

    def __call__(self, environ, start_response):
        exchange_id = uuid.uuid4().hex[:16]
        environ[EXCHANGE_ENVIRON_KEY] = exchange_id
        started = time.time()
        record = self.start_record(environ, exchange_id)
        reply = {}

        def recording_start_response(status, headers, exc_info=None):
            reply["status"] = status
            reply["headers"] = headers
            return start_response(status, headers, exc_info)

        body = self.application(environ, recording_start_response)
        return self.relay_body(body, record, reply, started)

    def relay_body(self, body, record, reply, started):
        # A static's body is never written, not even in its stub, so it is not
        # buffered either. Every exchange still reaches write_record, which
        # decides between the full record and the id-only stub — a static that
        # skipped it would leave its register calls naming an exchange this
        # trace does not contain. A failure in the buffering decision must not
        # reach the response: buffer, and let write_record hit the same failure
        # where it is recorded.
        try:
            buffered = not self.is_static(record.get("path"),
                                          reply.get("headers") or [])
        except Exception:
            buffered = True
        chunks = []
        try:
            for chunk in body:
                if buffered:
                    chunks.append(chunk)
                yield chunk
        finally:
            if hasattr(body, "close"):
                body.close()
            self.write_record(record, reply, chunks, started)

    def start_record(self, environ, exchange_id):
        record = {"exchange_id": exchange_id,
                  "ts": datetime.now().isoformat(),
                  "thread": threading.get_ident(),
                  "method": environ.get("REQUEST_METHOD"),
                  "path": environ.get("PATH_INFO"),
                  "query": environ.get("QUERY_STRING")}
        try:
            body = self.read_body(environ)
            record.update(req_headers=self.get_request_headers(environ),
                          req_body=body.decode("utf-8", "replace"),
                          req_len=len(body))
            record.update(self.get_rpc_payload(environ, body))
        except Exception as exc:
            record["recorder_error"] = f"{type(exc).__name__}: {exc}"
        return record

    def read_body(self, environ):
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if not length:
            return b""
        body = environ["wsgi.input"].read(length)
        environ["wsgi.input"] = io.BytesIO(body)
        return body

    def get_request_headers(self, environ):
        headers = {}
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                headers[key[5:].replace("_", "-").title()] = value
        for key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            if environ.get(key):
                headers[key.replace("_", "-").title()] = environ[key]
        return headers

    def get_rpc_payload(self, environ, body):
        content_type = (environ.get("CONTENT_TYPE") or "").lower()
        if not body or "x-www-form-urlencoded" not in content_type:
            return {"rpc_method": None, "form": None}
        parsed = urllib.parse.parse_qs(body.decode("utf-8", "replace"),
                                       keep_blank_values=True)
        return {"rpc_method": (parsed.get("method") or parsed.get("_M") or [None])[0],
                "form": {k: (v[0] if len(v) == 1 else v) for k, v in parsed.items()}}

    def write_record(self, record, reply, chunks, started):
        try:
            body = b"".join(chunks)
            headers = reply.get("headers") or []
            path = record.get("path")
            status = reply.get("status") or ""
            filtered = self.get_filter_reason(path, headers, body)
            if filtered:
                self.append_record(self.get_stub_record(record, status, filtered))
                return
            record.update(status=int(status.split(" ", 1)[0]) if status else None,
                          resp_headers=[[k, v] for k, v in headers],
                          resp_body=body.decode("utf-8", "replace"),
                          resp_len=len(body),
                          gnr_headers={k: v for k, v in headers
                                       if k.lower().startswith("x-gnr")},
                          duration_ms=round((time.time() - started) * 1000, 3))
            self.append_record(record)
        except Exception as exc:
            self.append_error(record.get("exchange_id"), exc)

    def get_filter_reason(self, path, headers, body):
        """Why this exchange carries no content, or None when it carries some."""
        if self.is_static(path, headers):
            return "static"
        if self.is_empty_ping(path, body):
            return "empty_ping"
        return None

    def get_stub_record(self, record, status, reason):
        """The id-only line of a filtered exchange: what it was, never a body.

        The register recorder stamps every call with the exchange that caused
        it, filtered exchanges included, so without this line those calls would
        name an exchange the HTTP trace does not contain — and a ping's register
        conversation could only be recognised by guessing from its verbs.
        """
        return {"exchange_id": record.get("exchange_id"),
                "ts": record.get("ts"),
                "thread": record.get("thread"),
                "method": record.get("method"),
                "path": record.get("path"),
                "query": record.get("query"),
                "rpc_method": record.get("rpc_method"),
                "status": int(status.split(" ", 1)[0]) if status else None,
                "filtered": reason}

    def is_static(self, path, headers):
        if (path or "").endswith("favicon.ico"):
            return True
        content_type = ""
        for key, value in headers:
            if key.lower() == "content-type":
                content_type = value.lower()
        return any(token in content_type for token in STATIC_CONTENT_TYPES)

    def is_empty_ping(self, path, body):
        if "_ping" not in (path or "").split("/"):
            return False
        answer = XML_DECLARATION.sub("", body.decode("utf-8", "replace").strip())
        return bool(EMPTY_PING_ANSWER.match(answer.strip()))

    def append_record(self, record):
        line = json.dumps(record, ensure_ascii=False)
        with self.lock:
            self.trace.write(line + "\n")
            self.trace.flush()

    def append_error(self, exchange_id, exc):
        try:
            self.append_record({"exchange_id": exchange_id,
                                "ts": datetime.now().isoformat(),
                                "thread": threading.get_ident(),
                                "recorder_error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
