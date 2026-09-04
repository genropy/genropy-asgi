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


# ---------------------------------------------------------------------------
# The shared live lane of the data-plane suites (#59): one GenropyWorker hosting
# the test site, its handler, the bridge's commander with its desk — built once
# per session, because building a GnrWsgiSite is the slow part. Every test gets
# a fresh request slot and leaves behind no user it made.
# ---------------------------------------------------------------------------

import importlib.util  # noqa: E402 - the environment above must be set first

import pytest  # noqa: E402

_HAS_GNR = importlib.util.find_spec("gnr") is not None
SITE = "test_invoice_pg"


def _start_lane(sibling=None, worker_name=None):
    from tests.lane import WORKER_NAME, start_site_lane

    if not _HAS_GNR:
        pytest.skip("GenroPy not installed")
    try:
        return start_site_lane(SITE, sibling=sibling, worker_name=worker_name or WORKER_NAME)
    except Exception as exc:  # site missing or broken: skip, don't fail
        pytest.skip(f"cannot start the {SITE} lane: {exc}")


@pytest.fixture(scope="session")
def site_lane():
    """The session's live lane: worker, handler, the bridge's commander and desk."""
    instance = _start_lane()
    yield instance
    instance.stop()


@pytest.fixture(scope="session")
def second_site_lane(site_lane):
    """A SECOND worker under the same commander and group as ``site_lane``."""
    instance = _start_lane(sibling=site_lane, worker_name="pool_0002")
    yield instance
    instance.stop()


def _reset_lane(lane, users_before):
    worker = lane.worker
    for user in set(worker.user_register.keys()) - users_before:
        worker.drop_user(user)
    lane.deliver_worker_events()
    lane.wait_filter_synced()
    lane.open_request()


@pytest.fixture
def lane(site_lane):
    """The shared lane with a fresh request slot; whatever users the test made are dropped after."""
    users_before = set(site_lane.worker.user_register.keys())
    site_lane.open_request()
    yield site_lane
    _reset_lane(site_lane, users_before)


@pytest.fixture
def two_lanes(site_lane, second_site_lane):
    """Both lanes with fresh request slots; both cleaned after."""
    before = [set(one.worker.user_register.keys()) for one in (site_lane, second_site_lane)]
    site_lane.open_request()
    second_site_lane.open_request()
    yield site_lane, second_site_lane
    for one, users_before in zip((site_lane, second_site_lane), before, strict=True):
        _reset_lane(one, users_before)
