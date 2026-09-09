# Copyright 2025-2026 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Contract: recording and replay preserve a session across distinct HTTP targets.

A real local WSGI server records both sessions. The replay must learn the new
page identifier and cookie, preserve repeated form fields and opaque bodies,
and correlate each replayed request with its source exchange. No GenroPy site
or historical dataset is required; source parity is supplied for these two
instances of the same test application.
"""

from contextlib import contextmanager
from threading import Thread
from types import SimpleNamespace
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIRequestHandler, make_server

from benchmarks.recording.http_recorder import HttpRecorder
from benchmarks.recording.run_archive import RunArchive
from benchmarks.replay.replica import REPLICA_HEADER, Replica, ReplicaClient, TraceReader


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


@contextmanager
def recorded_server(path, page_id, cookie):
    archive = RunArchive(str(path), run_id=path.stem, conditions={"stack": path.stem})
    received = []

    def application(environ, start_response):
        body = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
        received.append((environ["PATH_INFO"], environ["QUERY_STRING"],
                         environ.get("HTTP_COOKIE"), body))
        response = f"<html>page_id:'{page_id}'</html>".encode()
        start_response("200 OK", [("Content-Type", "text/html"),
                                   ("Content-Length", str(len(response))),
                                   ("Set-Cookie", f"session={cookie}; Path=/")])
        return [response]

    server = make_server("127.0.0.1", 0, HttpRecorder(application, archive),
                         handler_class=QuietHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, received
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        for connection in archive.connections.values():
            connection.close()


def test_recorded_session_replays_with_target_identity(tmp_path):
    source_id, target_id = "A" * 22, "B" * 22
    reference_path = tmp_path / "reference.sqlite"
    target_path = tmp_path / "target.sqlite"
    with recorded_server(reference_path, source_id, "source-cookie") as (port, _):
        client = ReplicaClient("127.0.0.1", port)
        try:
            assert client.send_request("GET", "/")[0] == 200
            assert client.send_request(
                "POST", f"/rpc?page_id={source_id}",
                f"page_id={source_id}&values=one&values=two",
                "application/x-www-form-urlencoded",
            )[0] == 200
            assert client.send_request(
                "POST", "/opaque", '{"message":"unaltered"}', "application/json",
            )[0] == 200
        finally:
            client.conn.close()

    trace = TraceReader(str(reference_path))
    try:
        with recorded_server(target_path, target_id, "target-cookie") as (port, received):
            replica = Replica(trace, "127.0.0.1", port,
                              parity=SimpleNamespace(aligned=True))
            try:
                assert replica.run() == []
            finally:
                replica.client.conn.close()
        assert len(received) == 3
        assert received[0][2] is None
        assert received[1][1] == f"page_id={target_id}"
        assert received[1][2] == "session=target-cookie"
        assert parse_qs(received[1][3].decode()) == {
            "page_id": [target_id], "values": ["one", "two"],
        }
        assert received[2][3] == b'{"message":"unaltered"}'
        target = TraceReader(str(target_path))
        try:
            assert [record["req_headers"][REPLICA_HEADER] for record in target.records] == [
                record["exchange_id"] for record in trace.exchanges
            ]
        finally:
            target.connection.close()
    finally:
        trace.connection.close()
