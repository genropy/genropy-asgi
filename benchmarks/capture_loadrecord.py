"""Capture a valid, repeatable loadRecordCluster request for hey.

Logs in, opens the customer TH page (gets a live page_id + connection cookie),
then prints everything hey needs to replay the record-read thousands of times:
  - URL
  - Cookie header
  - body file (the urlencoded loadRecordCluster form, with the live page_id)

Then self-checks repeatability: fires the same POST 3x with urllib and prints status+len.
"""
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, "/Users/gporcari/Sviluppo/genro_ng/meta-genro-modules/"
                   "sub-projects/genropy-asgi/temp/benchmark/assets")
from replay_a1 import User, build_plan, inject_identity, load_capture  # noqa: E402

BASE = "http://127.0.0.1:8099"
CUSTOMER = "/sys/thpage/invc/customer"
BODY_FILE = "/tmp/loadrecord_body.txt"


def main():
    rows = load_capture("session_capture.jsonl")
    login_calls, pages = build_plan(rows)
    # find the captured loadRecordCluster form
    lrc = None
    for r in rows:
        if r.get("rpc_method") == "loadRecordCluster":
            lrc = dict(r["form"])
            break
    if not lrc:
        print("no loadRecordCluster in capture")
        return

    u = User(BASE, login_calls, pages, "amelia.martin", "a")
    # 1. login
    html = u._get("/")
    frame = u._page_id_from(html)
    for form in u.login_calls:
        f = inject_identity(form, "amelia.martin", "a")
        u._post("/", f, frame, "login")
    # 2. open the customer page -> live page_id
    html = u._get(pages[CUSTOMER]["get_path"] if CUSTOMER in pages else CUSTOMER)
    pid = u._page_id_from(html)
    u._post(CUSTOMER, pages[CUSTOMER]["main"], pid, "main")  # build contexts

    # 3. build the loadRecordCluster body with the live page_id
    lrc["page_id"] = pid
    lrc["callcounter"] = "99"
    body = urllib.parse.urlencode(lrc)
    with open(BODY_FILE, "w") as fh:
        fh.write(body)

    # cookie header from the jar: HTTPCookieProcessor stores it
    jar = None
    for h in u.opener.handlers:
        if hasattr(h, "cookiejar"):
            jar = h.cookiejar
    cookie_hdr = "; ".join(f"{c.name}={c.value}" for c in jar) if jar else ""

    print("URL:    ", BASE + CUSTOMER)
    print("COOKIE: ", cookie_hdr)
    print("BODY:   ", BODY_FILE, f"({len(body)} bytes)")

    # 4. self-check repeatability: same POST 3x
    print("\n=== repeatability check (same POST x3) ===")
    for i in range(3):
        req = urllib.request.Request(BASE + CUSTOMER,
                                     data=body.encode(), method="POST")
        req.add_header("Content-Type",
                       "application/x-www-form-urlencoded; charset=UTF-8")
        req.add_header("Cookie", cookie_hdr)
        with urllib.request.urlopen(req, timeout=30) as r:
            b = r.read()
        print(f"  attempt {i+1}: status={r.status} len={len(b)}")

    # 5. ready-to-run hey command
    print("\n=== hey command (record-read load) ===")
    print(f'hey -n 5000 -c 16 -m POST '
          f'-H "Content-Type: application/x-www-form-urlencoded; charset=UTF-8" '
          f'-H "Cookie: {cookie_hdr}" '
          f'-D {BODY_FILE} "{BASE + CUSTOMER}"')


if __name__ == "__main__":
    main()
