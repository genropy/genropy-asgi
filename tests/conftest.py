# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Suite-wide environment: the daemon provider, declared before gnr.web loads.

genropy gates the ``gnr.web.daemon`` entry-point override on an explicit
request (genropy #1070): without ``GNR_DAEMON_PROVIDER`` the classic Pyro
client would load and the in-process register would never engage. Declared
here — conftest imports before any test module — so every test that builds a
site runs on the bridge's register, exactly as ``gnrasgiserve`` does.
"""

import os

os.environ.setdefault("GNR_DAEMON_PROVIDER", "genropy-asgi")

# macOS libpq negotiates GSS/Kerberos on every first connection: ~9 seconds of
# silence per child, enough to blow the 10-second presentation budget of the
# worker spawn (and under gunicorn it segfaults outright). The bench runbook
# has always set it; the suite sets it for the same reason.
os.environ.setdefault("PGGSSENCMODE", "disable")
