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
