"""Tiny logging HTTP proxy to capture a real GenroPy browser session.

Browser -> this proxy (:8090) -> target genropy server (:8099).
Every request is forwarded verbatim; request line + POST body (the pageCall
parameters) and response status are appended to a JSONL capture file so we
can later replay the exact sequence for the benchmark.

Stdlib only. Run: python3 capture_proxy.py
"""

import http.server
import json
import socketserver
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

LISTEN_PORT = 8090
TARGET = "http://127.0.0.1:8099"
CAPTURE = "/Users/gporcari/Sviluppo/genro_ng/meta-genro-modules/sub-projects/genropy-asgi/temp/benchmark/assets/session_capture.jsonl"

# headers we must not forward as-is (hop-by-hop / length recomputed)
SKIP_REQ_HEADERS = {"host", "connection", "keep-alive", "proxy-authenticate",
                    "proxy-authorization", "te", "trailers", "transfer-encoding",
                    "upgrade", "accept-encoding"}
SKIP_RESP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-encoding"}


def log_entry(entry):
    entry["ts"] = datetime.now().isoformat()
    with open(CAPTURE, "a") as f:
        f.write(json.dumps(entry) + "\n")


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _do(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        # extract the pageCall method + the full form params (values too)
        rpc_method, form_keys, form = None, None, None
        ctype = (self.headers.get("Content-Type") or "").lower()
        if body and "x-www-form-urlencoded" in ctype:
            try:
                parsed = urllib.parse.parse_qs(body.decode("utf-8", "replace"),
                                               keep_blank_values=True)
                rpc_method = (parsed.get("method") or parsed.get("_M") or [None])[0]
                form_keys = sorted(parsed.keys())
                # keep full values, but truncate very long ones to keep file sane
                form = {k: (v[0] if len(v) == 1 else v) for k, v in parsed.items()}
                for k, v in list(form.items()):
                    if isinstance(v, str) and len(v) > 2000:
                        form[k] = v[:2000] + f"...<+{len(v)-2000}b>"
            except Exception:
                pass

        # forward to target
        url = TARGET + self.path
        req = urllib.request.Request(url, data=body if body else None, method=method)
        for k, v in self.headers.items():
            if k.lower() not in SKIP_REQ_HEADERS:
                req.add_header(k, v)
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            status, resp_headers, resp_body = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            status, resp_headers, resp_body = e.code, e.headers, e.read()
        except Exception as exc:
            self.send_error(502, f"proxy error: {exc}")
            return

        log_entry({
            "method": method,
            "path": self.path,
            "rpc_method": rpc_method,
            "form_keys": form_keys,
            "form": form,
            "req_len": length,
            "status": status,
            "resp_len": len(resp_body),
        })

        # relay response
        self.send_response(status)
        for k, v in resp_headers.items():
            if k.lower() not in SKIP_RESP_HEADERS:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def do_GET(self):
        self._do("GET")

    def do_POST(self):
        self._do("POST")

    def log_message(self, *a):
        pass  # silence default stderr logging


class ThreadingProxy(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    open(CAPTURE, "w").close()  # truncate previous capture
    print(f"capture proxy on :{LISTEN_PORT} -> {TARGET}")
    print(f"capture file: {CAPTURE}")
    ThreadingProxy(("127.0.0.1", LISTEN_PORT), ProxyHandler).serve_forever()
