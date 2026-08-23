"""Isolation checks for the HTTP recorder: filters, whole bodies, and the
promise that a failure inside the recorder never reaches the response.

No site, no server, no database — a minimal WSGI app and a recorder wrapping it.
This is the machine evidence behind the recorder's two guarantees, so it lives
here rather than in a scratch file: evidence that is deleted is not evidence.

Run: python3 benchmarks/compare/http_recorder_check.py
"""

import io
import json
import os
import sys

from http_recorder import EXCHANGE_ENVIRON_KEY, HttpRecorder

TRACE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "temp", "http_recorder_check.jsonl")


def serve(recorder, path, body=b"", content_type="text/xml", answer=b"<answer/>",
          method="POST"):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
               "CONTENT_LENGTH": str(len(body)) if body else "",
               "CONTENT_TYPE": "application/x-www-form-urlencoded" if body else "",
               "HTTP_COOKIE": "session=abc", "wsgi.input": io.BytesIO(body)}
    seen = {}

    def app(env, start_response):
        seen["read"] = env["wsgi.input"].read()
        seen["header"] = env.get(EXCHANGE_ENVIRON_KEY)
        start_response("200 OK", [("Content-Type", content_type)])
        return [answer[:3], answer[3:]]

    def start_response(status, headers, exc_info=None):
        seen["status"] = status

    recorder.application = app
    served = b"".join(recorder(environ, start_response))
    return served, seen


def lines():
    if not os.path.exists(TRACE):
        return []
    with open(TRACE) as f:
        return [json.loads(line) for line in f if line.strip()]


def fresh():
    if os.path.exists(TRACE):
        os.remove(TRACE)
    return HttpRecorder(lambda e, s: [], trace_path=TRACE)


failures = []


def check(label, condition):
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


# 1. the happy path: whole bodies, the injected header, a distinct exchange_id
rec = fresh()
served, seen = serve(rec, "/test_invoice_pg_legacy/index",
                     body=b"method=login_doLogin&login=%3Clogin%3E%3C%2Flogin%3E")
check("response relayed intact", served == b"<answer/>")
check("app could still read the request body",
      seen["read"] == b"method=login_doLogin&login=%3Clogin%3E%3C%2Flogin%3E")
check("exchange id injected into the request", bool(seen["header"]))
served2, seen2 = serve(rec, "/other", body=b"method=x")
recorded = lines()
check("two lines written", len(recorded) == 2)
check("distinct exchange ids",
      recorded[0]["exchange_id"] != recorded[1]["exchange_id"])
first = recorded[0]
check("exchange id visible among the request headers",
      first["req_headers"].get("X-Bench-Exchange-Id") == seen["header"])
check("whole request body recorded",
      first["req_body"] == "method=login_doLogin&login=%3Clogin%3E%3C%2Flogin%3E")
check("whole response body recorded", first["resp_body"] == "<answer/>")
check("rpc method parsed", first["rpc_method"] == "login_doLogin")
check("form payload parsed", first["form"]["login"] == "<login></login>")
check("thread and duration recorded",
      isinstance(first["thread"], int) and first["duration_ms"] >= 0)
check("status recorded", first["status"] == 200)

# 2. the filters
rec = fresh()
serve(rec, "/_rsrc/js/gnr.js", content_type="application/javascript",
      answer=b"var a=1")
serve(rec, "/favicon.ico", content_type="application/octet-stream", answer=b"icon")
serve(rec, "/_ping", content_type="text/xml",
      answer=b"<?xml version='1.0' encoding='UTF-8'?>\n<GenRoBag></GenRoBag>")
# the real idle answer on the wire: handle_ping's bare envelope
serve(rec, "/_ping", content_type="text/xml",
      answer=b"<?xml version='1.0' encoding='UTF-8'?>\n"
             b"<GenRoBag><result _T=\"NN\"></result></GenRoBag>")
serve(rec, "/_ping", content_type="text/xml",
      answer=b"<?xml version='1.0' encoding='UTF-8'?>\n"
             b"<GenRoBag><result _T=\"NN\"/></GenRoBag>")
serve(rec, "/_ping", content_type="text/xml",
      answer=b"<?xml version='1.0' encoding='UTF-8'?>\n<GenRoBag><result _T=\"NN\">"
             b"</result><dataChanges><sc_0>x</sc_0></dataChanges></GenRoBag>")
recorded = lines()
check("statics, favicon and every empty ping shape are not recorded",
      len(recorded) == 1)
check("the ping carrying a datachange is recorded",
      recorded and "dataChanges" in recorded[0]["resp_body"])

# 3. the X-Gnr* breakdown
rec = fresh()
environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/p", "QUERY_STRING": "",
           "wsgi.input": io.BytesIO(b"")}


def gnr_app(env, start_response):
    start_response("200 OK", [("Content-Type", "text/plain"),
                              ("X-GnrTime", "0.12"), ("X-GnrSqlCount", "7")])
    return [b"body"]


rec.application = gnr_app
b"".join(rec(environ, lambda s, h, e=None: None))
check("X-Gnr* headers harvested",
      lines()[0]["gnr_headers"] == {"X-GnrTime": "0.12", "X-GnrSqlCount": "7"})

# 4. a failure on the reply side is recorded and does not reach the response
rec = fresh()
rec.is_static = lambda path, headers: (_ for _ in ()).throw(RuntimeError("boom"))
served, _ = serve(rec, "/p", answer=b"<intact/>")
recorded = lines()
check("response intact when the recorder fails on the reply",
      served == b"<intact/>")
check("the failure is recorded",
      recorded and recorded[0].get("recorder_error", "").startswith("RuntimeError"))

# 5. a failure on the request side is recorded and does not reach the response
rec = fresh()
rec.read_body = lambda environ: (_ for _ in ()).throw(ValueError("nope"))
served, _ = serve(rec, "/p", body=b"method=x", answer=b"<intact/>")
recorded = lines()
check("response intact when the recorder fails on the request",
      served == b"<intact/>")
check("the request-side failure is recorded",
      recorded and recorded[0].get("recorder_error", "").startswith("ValueError"))

os.remove(TRACE)
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
