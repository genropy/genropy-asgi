"""WSK form adaptation and live parity with the ordinary legacy RPC cycle."""

import asyncio
import io
import os
import re
import unittest
from urllib.parse import parse_qs, urlencode

from genro_tytx import from_tytx, to_tytx

from genropy_asgi.spa.websocket_receiver import WebSocketReceiver


class RpcAdapterTest(unittest.TestCase):
    def environ(self, form):
        body = to_tytx({"form": form}, "json").encode()
        return {"REQUEST_METHOD": "WSK", "PATH_INFO": "/a/page",
                "wsgi.url_scheme": "wss",
                "QUERY_STRING": "rpc=must_not_override", "genro.page_id": "page",
                "genro.identity": object(), "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": io.BytesIO(body)}

    def test_form_context_and_response_preserved(self):
        original = self.environ(urlencode({"method": "probe", "value": "7::L"}))
        response = []

        def application(environ, start):
            self.assertEqual(environ["REQUEST_METHOD"], "POST")
            self.assertEqual(environ["wsgi.url_scheme"], "https")
            self.assertEqual(environ["QUERY_STRING"], "")
            self.assertEqual(environ["PATH_INFO"], "/a/page")
            self.assertIs(environ["genro.identity"], original["genro.identity"])
            self.assertEqual(environ["genro.transport"], "websocket")
            self.assertEqual(parse_qs(environ["wsgi.input"].read().decode()),
                             {"method": ["probe"], "value": ["7::L"], "page_id": ["page"]})
            start("200 OK", [("Content-Type", "text/xml"), ("X-GnrTime", "0.1"),
                             ("Set-Cookie", "test=hidden; HttpOnly")])
            return [b'<GenRoBag><result dtype="L">7</result></GenRoBag>']

        result = b"".join(WebSocketReceiver(application)(
            original, lambda status, headers: response.append(status)))
        payload = from_tytx(result.decode(), "json")
        self.assertIn('dtype="L"', payload["body"])
        self.assertEqual(payload["headers"]["x-gnrtime"], "0.1")
        self.assertNotIn("set-cookie", payload["headers"])
        self.assertEqual(original["REQUEST_METHOD"], "WSK")
        self.assertEqual(response, ["200 OK"])

    def test_invalid_forms_do_not_dispatch(self):
        def forbidden(environ, start):
            self.fail("invalid form reached application")

        for form in ("", "method=a&method=b", "method=a&page_id=other",
                     "method=a&rpc=b", "method=a&_plugin=b", "method=a&mode=text"):
            with self.subTest(form=form):
                status = []
                list(WebSocketReceiver(forbidden)(
                    self.environ(form), lambda value, headers: status.append(value)))
                self.assertEqual(status, ["400 Bad Request"])

    def test_application_failure_is_not_an_input_error(self):
        def application(environ, start):
            raise ValueError("application failure")

        with self.assertRaisesRegex(ValueError, "application failure"):
            list(WebSocketReceiver(application)(self.environ("method=probe"),
                                                lambda status, headers: None))


@unittest.skipUnless(os.environ.get("GNR_WSX_TEST_URL"), "requires a running test site")
class LiveRpcTest(unittest.TestCase):
    def test_http_wsk_results_errors_and_fresh_pages(self):
        import httpx
        from websockets.asyncio.client import connect
        from genro_asgi.wsx import WsxEnvelope
        from gnr.core.gnrbag import Bag

        base = os.environ["GNR_WSX_TEST_URL"].rstrip("/")
        path = "/webpages/wsx_rpc"
        with httpx.Client(base_url=base, timeout=30) as client:
            page = client.get(path)
            page.raise_for_status()
            match = re.search(r"page_id:'([\w-]+)'", page.text)
            self.assertIsNotNone(match)
            page_id = match.group(1)
            cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
            form = {"method": "probe", "value": "7::L", "page_id": page_id}
            http_reply = client.post(path, data=form)
            http_reply.raise_for_status()
            expected = Bag(http_reply.text)["result"]
            http_error = Bag(client.post(path, data={"method": "fail", "page_id": page_id}).text)

        async def exercise():
            async with connect(base.replace("http", "ws", 1) + "/websocket",
                               additional_headers={"Cookie": cookie}, origin=base) as ws:
                async def send(identifier, target, data):
                    await ws.send(WsxEnvelope(id=identifier, method="WSK", path=target,
                                              page_id=page_id, data=data).encode())
                    reply = WsxEnvelope(await asyncio.wait_for(ws.recv(), 15))
                    self.assertEqual(reply.id, identifier)
                    self.assertEqual(reply.status, 200, reply.data)
                    return reply.data

                await send("open", "/_wsx/openchannel", {"parameters": {"sequential": True}})
                previous = expected["invocation_id"]
                for index in range(2):
                    result = await send(str(index), path, {"form": urlencode(form)})
                    actual = Bag(result["body"])["result"]
                    self.assertNotEqual(actual["invocation_id"], previous)
                    previous = actual["invocation_id"]
                    for key in ("value", "value_type", "day", "amount",
                                "clean_before", "current_page"):
                        self.assertEqual(actual[key], expected[key], key)
                    self.assertTrue(actual["clean_before"])
                    self.assertTrue(actual["current_page"])
                    failed = await send("failure" + str(index), path,
                                        {"form": urlencode({"method": "fail"})})
                    self.assertEqual(Bag(failed["body"])["error"], http_error["error"])

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
