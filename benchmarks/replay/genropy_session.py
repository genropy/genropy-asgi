"""GenroPy login, indexed selection and logout adapter from the churn driver.

PROVISIONAL: legacy capture parsing still comes from replay_a1. This adapter
belongs to the bridge; the shared execution engine must not import it.
"""

import http.client
import threading
import time
import urllib.parse

from benchmarks.replay_a1 import User, build_plan, inject_identity, load_capture  # noqa: F401

WHERE = ('<?xml version="1.0" encoding="utf-8"?>\n'
         '<GenRoBag><c_0 op="equal" column="username" column_dtype="T" '
         'column_caption="Username">{username}</c_0></GenRoBag>::bag')


class LoggedUser:
    """One account logged in, with its own connection, its page and its thread.

    The login is the real two-call GenroPy dance (``inject_identity`` writes the
    identity into BOTH places, flat fields and the Bag of ``login_doLogin``);
    anything less logs every session in as the captured user and the whole run
    measures one identity.
    """

    def __init__(self, base, login_calls, pages, username, password, lookups, period):
        self.username = username
        self.period = period
        self.lookups = lookups
        self.netloc = urllib.parse.urlparse(base).netloc
        user = User(base, login_calls, pages, username, password)
        html = user._get("/")
        self.page_id = user._page_id_from(html)
        for form in login_calls:
            user._post("/", inject_identity(form, username, password), self.page_id, "login")
        user._get("/")
        jar = None
        for handler in user.opener.handlers:
            if hasattr(handler, "cookiejar"):
                jar = handler.cookiejar
        self.cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
        self.connection = http.client.HTTPConnection(self.netloc, timeout=30)
        self.stop_event = threading.Event()
        self.thread = None

    @property
    def headers(self):
        return {"Cookie": self.cookie,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}

    def get_call_form(self, lookup, callcounter):
        """The load unit: one indexed getSelection, its filter rotating."""
        return {
            "method": "app.getSelection", "table": "adm.user",
            "where": WHERE.format(username=lookup), "queryMode": "S",
            "sortedBy": "username", "selectionName": "*V_adm_user_churn",
            "recordResolver": "false::B", "sqlContextName": "standard_list",
            "totalRowCount": "false::B", "row_start": "0",
            "excludeLogicalDeleted": "true::B", "excludeDraft": "true::B",
            "columns": "$username", "checkPermissions": "true::B",
            "row_count": "1::L", "storepath": ".store",
            "page_id": self.page_id, "callcounter": str(callcounter),
        }

    def start_traffic(self, record, record_failure):
        """Begin this user's own paced traffic; *record* takes (latency, failed)."""
        self.thread = threading.Thread(target=self.generate_traffic,
                                       args=(record, record_failure), daemon=True)
        self.thread.start()

    def generate_traffic(self, record, record_failure):
        """One call every ``period`` seconds until told to leave."""
        counter = 0
        while not self.stop_event.is_set():
            started = time.time()
            lookup = self.lookups[counter % len(self.lookups)]
            body = urllib.parse.urlencode(self.get_call_form(lookup, counter + 100))
            failed = self.send_call(body, record_failure)
            record(time.time() - started, failed)
            counter += 1
            self.stop_event.wait(max(0.0, self.period - (time.time() - started)))

    def send_call(self, body, record_failure):
        """Send one call, reconnecting once if the kept-alive socket was closed.

        A server that closed an idle keep-alive connection has not failed, and a
        browser does not report it: it opens another and asks again. Counting
        that as an error measures the server's keep-alive window instead of its
        behaviour under load — which is exactly what a first run of this driver
        did against gunicorn (2 s idle window, users calling every 3 s), and it
        read as half the calls failing.

        Returns:
            True when the call really failed — the retry included.
        """
        for attempt in (1, 2):
            try:
                self.connection.request("POST", "/", body=body, headers=self.headers)
                answer = self.connection.getresponse()
                payload = answer.read()
                if answer.status != 200 or b"<error>" in payload:
                    record_failure(f"http {answer.status}: {payload[:300]!r}")
                    return True
                return False
            except Exception as failure:
                self.connection.close()
                self.connection = http.client.HTTPConnection(self.netloc, timeout=30)
                if attempt == 2:
                    record_failure(f"{type(failure).__name__} twice: {failure}")
                    return True
        return True

    def log_out(self):
        """Stop the traffic, then tell the site the connection is over.

        The logout is what frees the user's placement: without it the register
        keeps him and the pool never learns the room is back.
        """
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        try:
            body = urllib.parse.urlencode({"method": "connection.logout", "page_id": self.page_id,
                                           "callcounter": "9999"})
            self.connection.request("POST", "/", body=body, headers=self.headers)
            self.connection.getresponse().read()
        except Exception:
            pass
        finally:
            self.connection.close()
