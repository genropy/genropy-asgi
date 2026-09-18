"""Reception-only worker boundary and optional live WSX round trips."""

import asyncio
import io
import os
import re
import unittest

from genro_tytx import from_tytx, to_tytx

from genropy_asgi.spa.websocket_receiver import WebSocketReceiver


class ReceptionTest(unittest.TestCase):
    def receive(self, payload, page_id="page"):
        def forbidden_dispatch(environ, start_response):
            self.fail("WSK must not execute the legacy application")

        receiver = WebSocketReceiver(forbidden_dispatch)
        body = to_tytx(payload, "json").encode()
        environ = {"REQUEST_METHOD": "WSK", "genro.page_id": page_id,
                   "PATH_INFO": "/_websocket_receive",
                   "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}
        response = []
        chunks = receiver(environ, lambda status, headers: response.append(status))
        return response[0], from_tytx(b"".join(chunks).decode(), "json")

    def test_receives_without_execution_or_logging_values(self):
        with self.assertLogs("genropy_asgi.websocket", level="INFO") as captured:
            status, result = self.receive(
                {"method": "probe", "parameters": {"value": "private-value"}})
        self.assertEqual(status, "200 OK")
        self.assertTrue(result["received"])
        self.assertFalse(result["executed"])
        self.assertNotIn("private-value", " ".join(captured.output))

    def test_invalid_calls(self):
        for payload in ({}, {"method": "probe", "parameters": []}, []):
            with self.subTest(payload=payload):
                status, result = self.receive(payload)
                self.assertEqual(status, "400 Bad Request")
                self.assertFalse(result["executed"])

    def test_missing_page(self):
        self.assertEqual(self.receive({"method": "probe"}, page_id=None)[0], "400 Bad Request")

    def test_http_is_forwarded_unchanged(self):
        calls = []
        environ = {"REQUEST_METHOD": "POST"}

        def start(status, headers):
            pass

        output = [b"ordinary HTTP"]

        def application(env, callback):
            calls.append((env, callback))
            return output

        self.assertIs(WebSocketReceiver(application)(environ, start), output)
        self.assertEqual(calls, [(environ, start)])


@unittest.skipUnless(os.environ.get("GNR_WSX_TEST_URL"),
                     "set GNR_WSX_TEST_URL for the running test site")
class LiveReceptionTest(unittest.TestCase):
    def test_real_page_channel_and_calls(self):
        import httpx
        from websockets.asyncio.client import connect
        from genro_asgi.wsx import WsxEnvelope

        base = os.environ["GNR_WSX_TEST_URL"].rstrip("/")
        with httpx.Client(base_url=base, timeout=30) as client:
            page = client.get("/webpages/wsx_probe")
            page.raise_for_status()
            match = re.search(r"page_id:'([\w-]+)'", page.text)
            self.assertIsNotNone(match)
            page_id = match.group(1)
            cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())

        async def exercise():
            async with connect(base.replace("http", "ws", 1) + "/websocket",
                               additional_headers={"Cookie": cookie}, origin=base) as ws:
                async def call(identifier, path, payload, target=page_id):
                    await ws.send(WsxEnvelope(id=identifier, method="WSK", path=path,
                                              page_id=target, data=payload).encode())
                    answer = WsxEnvelope(await asyncio.wait_for(ws.recv(), 10))
                    self.assertEqual(answer.id, identifier)
                    return answer

                rejected = await call("foreign", "/_wsx/openchannel", {}, "not-owned")
                self.assertEqual(rejected.status, 403)
                opened = await call("open", "/_wsx/openchannel",
                                    {"parameters": {"sequential": True}})
                self.assertEqual(opened.status, 200)
                for index in range(2):
                    answer = await call(str(index), "/_websocket_receive", {
                        "method": "wsx_probe_must_not_execute", "parameters": {"sequence": index}})
                    self.assertEqual(answer.status, 200)
                    self.assertTrue(answer.data["received"])
                    self.assertFalse(answer.data["executed"])
                    self.assertEqual(answer.data["page_id"], page_id)
                bad = await call("bad", "/_websocket_receive", {})
                self.assertEqual(bad.status, 400)
                self.assertFalse(bad.data["executed"])

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
