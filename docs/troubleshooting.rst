Troubleshooting
===============

Symptom → cause → fix. Find your symptom below.

Fix startup failures
--------------------

**"site name is required"**
   You did not pass an instance. Pass one: ``gnrasgiserve mysite``.

**"no root.py in the site provided"**
   The instance resolves but the site directory has no ``root.py``. Check the path
   GenroPy resolves for it (the same one ``gnrwsgiserve`` uses).

**GenroPy environment not found**
   genropy-asgi runs an existing, configured GenroPy site. Make sure
   ``~/.gnr/environment.xml`` exists and points at your GenroPy environment — the
   same setup ``gnrwsgiserve`` needs.

**Port already in use**
   Another process holds the port. Find it and free it:

   .. code-block:: console

      $ lsof -nP -iTCP:8080 -sTCP:LISTEN
      $ lsof -tiTCP:8080 -sTCP:LISTEN | xargs kill

Fix pool behaviour
------------------

**The pool never grows beyond the initial workers**
   The pool grows on measured **occupancy**, not user count: it spawns only when no
   non-reception worker is under the admission threshold (0.8). Idle or lightly
   loaded users do not move the occupancy, so a pool holding many idle sessions on
   one worker is behaving correctly — that is not a stall. It grows when the *work*
   (cpu, executor) rises, not when the head count does. Check the live state:

   .. code-block:: console

      $ curl -s http://127.0.0.1:8080/_server/monitor_state | python3 -m json.tool

**Too many workers spawn under a login burst**
   A fresh worker takes a few seconds to boot a full ``GnrWsgiSite``. When logins
   arrive faster than a worker can announce, they pile onto the last full worker
   until the new one is ready — the commander never stacks a second spawn while one
   is in flight. Under a realistic login rate the pool settles to the expected size.
   If you are load-testing, pace logins so each spawn can announce before the next
   wave.

**A user's session seems to reset between requests**
   Routing depends on the ``spa_connection_id`` cookie, which carries the
   connection id the site itself minted while serving. A client that drops
   cookies, or opens a fresh connection without carrying them, is a new visitor
   at every request and is placed as one. Make sure the client keeps cookies
   across requests.

**A shared global value lags on another worker**
   There are no replicas to lag. The master lives on the commander and nowhere
   else: a worker reads it with a call on the lane it already holds, and writes
   through a grant that lands all at once at its release. A value that looks
   stale is a value nobody has written yet. See the sharing section of
   :doc:`the-pool`.

Fix a stale build being served
------------------------------

If behaviour does not match the code you expect, confirm no old server is still
listening — a pool left running from an earlier launch keeps serving its old code:

.. code-block:: console

   $ lsof -nP -iTCP:8080 -sTCP:LISTEN     # is anything still there?
   $ lsof -tiTCP:8080 -sTCP:LISTEN | xargs kill

Then relaunch.
