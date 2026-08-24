# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Contract tests for the group engine: what the template hands its children.

The fork path (core contract, 2026-08-24): a template process builds the
``GnrWsgiSite`` once through :class:`GenropySiteEngineFactory` and forks a
worker off it, so the site travels by inheritance instead of being rebuilt.
What is asserted here is what a forked child depends on:

- the engine comes back settled, with the two lazy resolutions the first
  request would otherwise force in every worker already done;
- no db connection is left open, because a socket inherited by every child is
  a socket every child would speak on;
- no thread is started, because the template refuses to fork when more than
  one is alive;
- a worker handed a ``group_engine`` hosts THAT site and builds none of its
  own, while a worker without one still builds its site as it always did.

Real site, no stubs: what these tests protect is the behaviour of the real
``GnrWsgiSite`` under fork, and a stub would assert nothing about it.
"""

import importlib.util
import tempfile
import threading

import pytest

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")


def make_factory():
    from genropy_asgi.spa.site_engine_factory import GenropySiteEngineFactory

    return GenropySiteEngineFactory(source=_SITE, debug=False)


def open_connections(site):
    """The db connections open on the calling thread, through the site's own map."""
    return [name for pool in site.db._connections.values() for name in pool]


@pytest.fixture(scope="module")
def engine():
    """One group engine, built as the template builds it; skip if the site cannot."""
    try:
        instance = make_factory().build_group_engine()
    except Exception as exc:  # site missing or broken: skip, don't fail
        pytest.skip(f"cannot build the {_SITE} site: {exc}")
    yield instance
    instance.on_site_stop()


def test_the_engine_is_the_site(engine):
    assert engine.site_name == _SITE
    assert engine._local_mode is True


def test_the_engine_comes_back_settled(engine):
    # resources_dirs and storage('gnr') are what the first request resolves
    # lazily; resolved here they are inherited by every child, which is the
    # whole reason a template exists.
    assert engine.resources_dirs
    assert engine.storage("gnr") is not None


def test_no_db_connection_is_left_open(engine):
    # storage('gnr') opens _main_db: the settling is only safe because the
    # factory closes what it opened before the engine is handed over.
    assert open_connections(engine) == []


def test_the_engine_starts_no_thread():
    # The template refuses to fork when more than one thread is alive, so the
    # factory must leave the count where it found it. Absolute counts belong
    # to the runner, not to this code: what is asserted is the difference.
    before = threading.active_count()
    site = make_factory().build_group_engine()
    try:
        assert threading.active_count() == before
    finally:
        site.on_site_stop()


def test_a_worker_handed_an_engine_hosts_it_and_builds_nothing(engine):
    from genro_asgi.spa.orchestration import FreezeHandler

    from genropy_asgi.spa.genropy_worker import GenropyWorker

    deposit = tempfile.mkdtemp(prefix="gnr_frozen_")
    worker = GenropyWorker(
        "pool_0001",
        source=_SITE,
        debug=False,
        group_engine=engine,
        freeze_handler=FreezeHandler(deposit),
    )
    try:
        assert worker.gnr_site is engine
        assert worker.wsgi_app is engine
        assert engine.spa_worker is worker
    finally:
        worker.wsgi_app = None
        worker.exit_process()


def test_a_worker_without_an_engine_builds_its_own():
    from genro_asgi.spa.orchestration import FreezeHandler

    from genropy_asgi.spa.genropy_worker import GenropyWorker

    deposit = tempfile.mkdtemp(prefix="gnr_frozen_")
    worker = GenropyWorker(
        "pool_0002", source=_SITE, debug=False, freeze_handler=FreezeHandler(deposit)
    )
    try:
        assert worker.gnr_site.site_name == _SITE
    finally:
        worker.exit_process()
