# Issue 72 bridge live acceptance

Date: 2026-09-08. Bridge HEAD `664dda96f6da7715af5eadbc993f3f0896c0c296`; candidate core working tree based on `2465fcc47dadb1761ec1c09466c847715725daa9`.

## Isolation and launch

- Site name: `test_invoice_pg`, resolved through private `GENRO_GNRFOLDER=$PWD/temp/issue72-runtime/gnr`.
- Database: disposable `issue72_bridge_664dda9`; ownership and cleanup are in `temp/issue72-runtime/OWNERSHIP.md`.
- Runtime paths: `/private/tmp/i72b664/i` and `/private/tmp/i72b664/f`.
- Bound only to `127.0.0.1:38172`; no shared site, config, service, or database was changed.
- Launch command:

  ```sh
  env GENRO_GNRFOLDER="$PWD/temp/issue72-runtime/gnr" \
    PGGSSENCMODE=disable \
    PYTHONPATH="$PWD/src:$PWD/../core/src:/Users/gporcari/Sviluppo/Genropy/genropy/gnrpy" \
    GNR_ASGI_INSTANCE_DIR=/private/tmp/i72b664/i \
    GNR_ASGI_FROZEN_USERS_PATH=/private/tmp/i72b664/f \
    .venv/bin/python -m genropy_asgi.spa.cli test_invoice_pg -p 38172 --nodebug
  ```

## Actual live evidence

| Exercise | Result |
|---|---|
| `GET /` | 200, 8,069-byte bootstrap page |
| Browser cookies | Legacy site cookie plus `spa_connection_id` and `session_id` were set (values deliberately not recorded) |
| Dojo asset | 200, 76,759 bytes |
| Compiled Genro app asset | 200, 1,259,506 bytes |
| Page ping/datachanges poll | `POST /_ping` 200 with valid empty GenRoBag XML; repeated live pings stayed 200 |
| Login | Existing isolated fixture user logged in; UI displayed `Alexander King` |
| RPC/datastore | Opened Customers, ran the query, and rendered `Customers (3200/3200)` with rows |
| Browser diagnostics | No browser warning or error entries after HTTP-mode login/query |
| Deliberate invalid/stale requests | Stale ping returned 412 and malformed root POST returned 400 without terminating the worker |
| Second page/session | Independent fresh GET minted a distinct page/cookie session and returned 200; close beacons for distinct page IDs returned 200 |

Inline browser screenshots were captured for the login screen, authenticated home, and populated customer grid.

## Final automated regression

After the diagnostic, the private site configuration was restored to ordinary HTTP polling and the diagnostic `sitecustomize.py` directory was omitted from `PYTHONPATH`. The complete bridge suite was then run against the current candidate core with the same owned database and runtime paths:

```sh
env GENRO_GNRFOLDER="$PWD/temp/issue72-runtime/gnr" \
  PGGSSENCMODE=disable \
  PYTHONPATH="$PWD/src:$PWD/../core/src:/Users/gporcari/Sviluppo/Genropy/genropy/gnrpy" \
  GNR_ASGI_INSTANCE_DIR=/private/tmp/i72b664/i \
  GNR_ASGI_FROZEN_USERS_PATH=/private/tmp/i72b664/f \
  .venv/bin/pytest -q
```

Result: **260 passed, 24 warnings in 16.46 seconds** on Python 3.14.6. The two new opaque-boundary tests passed. The warnings are existing dependency deprecations; there were no failures, errors, or skips.

| Capability | Automated suite | Actual live site |
|---|---:|---:|
| Opaque `Frame(info, payload)` bridge boundary | Passed | Exercised through HTTP request/reply |
| Raw response bytes and duplicate headers/cookies | Passed | Assets and cookies observed |
| Legacy login, RPC, datastore | Existing coverage passed | Passed |
| HTTP datachanges polling | Existing coverage passed | Passed |
| Global-store datetime/lease behavior | Existing coverage passed | Not repeated manually |
| Worker lifecycle, freeze/wake, delivery | Existing coverage passed | Not repeated manually |
| Core WSX protocol | Core-owned tests pass | Upgrade accepted in diagnostic |
| Unmodified legacy browser over core WSX | Not represented as compatible | Blocked by protocol mismatch |

## Websocket compatibility diagnostic

The real site does **not** enable core WSX merely from legacy site configuration. With the correctly resolved private instance-root `siteconfig.xml` containing `<wsgi websockets="true">`, generated HTML still said `websockets_url: ::NN`. `GnrWsgiSite` calls the legacy `WsgiWebSocketHandler.checkSocket()` and disables websockets because no `gnrasync` Unix socket exists. XML value `required` is not usable here: the constructor first applies `boolean(config_value)`, which makes that string false, and the bridge factory supplies no `websockets='required'` constructor argument.

A temp-only `sitecustomize.py` forced `checkSocket()` true to isolate the next boundary. Page creation then immediately tried `registerNewPage` through the legacy `gnrasync` Unix-socket HTTP proxy and failed with `FileNotFoundError`. Suppressing that legacy registration call in the diagnostic allowed the browser page to load and advertise `/websocket`.

The browser connected successfully (`WebSocket /websocket [accepted]`, `connection open`), proving the ASGI upgrade route itself works. The wire protocols are incompatible:

- Legacy `gnrwebsocket.js` sends plain JSON such as serialized `{command: "connected", page_id: ...}` and then `{command: "ping", ...}` every second. RPC calls use `command: "call"` and `result_token`; replies are XML envelopes or literal `pong`.
- Core accepts only text beginning `WSX://`, with JSON routing fields (`id`, `method`, `path`, `page_id`, `reply_path`) and a TYTX string in `data`. Open-channel is `/_wsx/openchannel`; ping is `/_wsx/ping`; replies are `WSX://` envelopes.
- The live core log consequently emitted `Websocket: message dropped, this text is not a WSX:// message` for `connected` and every legacy ping. No channel was bound, so push/datachanges/freeze-wake over websocket cannot be claimed from this legacy browser.

## Scope conclusion

Issue 72's opaque frame migration preserves the current bridge HTTP/polling behavior, which passed both the 260-test bridge suite and this live browser exercise. Enabling WSX for an unmodified legacy Genro browser is a separate compatibility feature, not a factory flag: it needs an explicit adapter for browser request/reply envelopes **and** replacement of the site's server-side `gnrasync` registration/push proxy calls. Turning on `websockets` without both pieces breaks page creation or creates a connected socket whose messages are all dropped.

Therefore the safe acceptance claim for this issue is HTTP login/RPC/datastore/datachanges polling plus opaque transport regression. Live openchannel/push, two-page websocket datachanges, and websocket-assisted freeze/wake remain independently blocked by the documented protocol mismatch and should not be overclaimed. Whether to add a legacy-browser WSX adapter is a substantive scope decision still pending with the user; it is not required to establish the opaque transport regression result recorded here.
