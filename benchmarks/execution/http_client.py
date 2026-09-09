"""Persistent HTTP transport with a cookie jar.

FIXED: this transport has no GenroPy or orchestration dependencies. Existing
callers retain their GET/POST behavior and response tuple (status, body).
PROVISIONAL: the historical StickyClient name is retained during migration.
"""

import http.client
import urllib.parse
from http.cookies import SimpleCookie


class StickyClient:
    """One persistent HTTP connection with a cookie jar."""

    def __init__(self, host, port):
        self.conn = http.client.HTTPConnection(host, port, timeout=60)
        self.cookies: dict[str, str] = {}

    def _headers(self):
        headers = {"Connection": "keep-alive"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return headers

    def _store_cookies(self, resp):
        for hk, hv in resp.getheaders():
            if hk.lower() == "set-cookie":
                jar = SimpleCookie()
                jar.load(hv)
                for name, morsel in jar.items():
                    self.cookies[name] = morsel.value

    def get(self, path):
        self.conn.request("GET", path, headers=self._headers())
        resp = self.conn.getresponse()
        body = resp.read()
        self._store_cookies(resp)
        return resp.status, body

    def post(self, path, form):
        data = urllib.parse.urlencode(form)
        headers = self._headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        self.conn.request("POST", path, body=data, headers=headers)
        resp = self.conn.getresponse()
        body = resp.read()
        self._store_cookies(resp)
        return resp.status, body
