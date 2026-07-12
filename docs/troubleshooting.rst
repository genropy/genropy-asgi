Troubleshooting
===============

Startup
-------

**"site name is required"**
   You did not pass an instance. ``gnrasgiserve mysite``.

**"no root.py in the site provided"**
   The instance resolves but the site directory has no ``root.py``. Check the
   path GenroPy resolves for it (the same one ``gnrwsgiserve`` uses).

**GenroPy environment not found**
   genropy-asgi runs an existing, configured GenroPy site. Make sure
   ``~/.gnr/environment.xml`` exists and points at your GenroPy environment —
   the same setup ``gnrwsgiserve`` needs.

**Port already in use**
   Another process holds the port. Find it and free it:

   .. code-block:: console

      $ lsof -nP -iTCP:8080 -sTCP:LISTEN
      $ lsof -tiTCP:8080 -sTCP:LISTEN | xargs kill

The pool
--------

**The pool never grows beyond the initial workers**
   The pool grows only when *every* worker is at or over 80% of its cap. If you
   log in fewer users than that threshold, one worker is enough — that is
   correct behaviour, not a stall. Check the live state:

   .. code-block:: console

      $ curl -s http://127.0.0.1:8080/_server/monitor_state | python3 -m json.tool

**Too many workers spawn under a login burst**
   A fresh worker takes a few seconds to boot a full ``GnrWsgiSite``. When logins
   arrive faster than a worker can announce, they pile onto the last full worker
   until the new one is ready — the commander never stacks a second spawn while
   one is in flight. Under a realistic login rate the pool settles to the
   expected size. If you are load-testing, pace logins so each spawn can
   announce before the next wave.

**A user's session seems to reset between requests**
   Routing depends on the ``sticky_cid`` cookie. A client that drops cookies, or
   opens a fresh connection without carrying them, is treated as a new visitor
   each time and may land on the welcome worker instead of the worker holding
   the session. Make sure the client keeps cookies across requests.

**A shared global value lags on another worker**
   Expected: the global store is eventually coherent. A write on one worker
   reaches the others after one channel round-trip (commander master → replica
   push), not synchronously. See the global-store section of
   :doc:`single-vs-multi`.

Serving a stale build
---------------------

If behaviour does not match the code you expect, confirm no old server is still
listening — a pool left running from an earlier launch keeps serving its old
code:

.. code-block:: console

   $ lsof -nP -iTCP:8080 -sTCP:LISTEN     # is anything still there?
   $ lsof -tiTCP:8080 -sTCP:LISTEN | xargs kill

Then relaunch.
