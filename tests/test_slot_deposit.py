# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Every request delivers what its slot still holds before it returns.

A commit's deposits used to leave the worker only inside ``collect_page``, which
only a page's own envelope reaches: a ``rootPage`` webhook, or a request that
failed after its commit, announced to nobody. The end of the stitching delivers
whatever the slot still holds through ``/commander/delivery/deposit`` — filed in
the subscribers' queues, nothing retired, because there is no page to answer —
and after a collect the slot is empty, so nothing is delivered twice. Migrated
from the core (genro-asgi ``tests/orchestration/test_contract_slot_deposit.py`` at
``f3c7962``) with #59: two GenropyWorkers under one commander.
"""

from __future__ import annotations

import pytest

from genro_asgi.spa.environ import WsgiSeam
from genro_asgi.spa.orchestration.worker_connector import CommanderCallFailed
from tests.lane import wait_until

TABLE = "customer"


@pytest.fixture
def two_pages(two_lanes):
    """The two lanes with one page each, ``p2`` subscribing the table."""
    lane, other = two_lanes
    lane.worker.add_connection("c1", sticky_cid="spa-c1")
    lane.worker.add_page("p1", "c1")
    lane.deliver_worker_events()
    other.worker.add_connection("c2", sticky_cid="spa-c2")
    other.worker.add_page("p2", "c2")
    other.deliver_worker_events()
    other.verb("subscribeTable", "alice", table=TABLE, page_id="p2")
    wait_until(lambda: lane.worker.subscribed_tables == {TABLE})
    return lane, other


def test_a_request_that_never_collected_delivers_its_deposits(two_pages):
    lane, other = two_pages
    lane.open_request()
    lane.verb("notifyDbEvents", "alice", dbevents={TABLE: ["ins:1"]}, page_id="p1")
    lane.verb("deliver_slot_deposits")
    delivery = other.verb("collect_page", "p2")
    assert [deposit["table"] for deposit in delivery["dbevents"]] == [TABLE]
    assert delivery["dbevents"][0]["from_page_id"] == "p1"


def test_a_collected_request_delivers_its_deposits_once(two_pages):
    lane, other = two_pages
    lane.open_request()
    lane.verb("notifyDbEvents", "alice", dbevents={TABLE: ["ins:1"]}, page_id="p1")
    lane.verb("collect_page", "p1")
    lane.verb("deliver_slot_deposits")
    assert len(lane.desk.page_dbevent_map["p2"]) == 1


def test_an_empty_slot_places_no_call(two_pages, monkeypatch):
    lane, _other = two_pages
    lane.open_request()
    lane.verb("notifyDbEvents", "alice", dbevents={"orders": ["ins:1"]}, page_id="p1")
    calls = []
    monkeypatch.setattr(lane.worker, "call", lambda *args, **kwargs: calls.append(args))
    lane.verb("deliver_slot_deposits")
    assert calls == []


def test_a_request_failing_after_its_commit_still_delivers(two_pages):
    lane, other = two_pages

    def wsgi_app(environ, start_response):
        lane.worker.notifyDbEvents("alice", dbevents={TABLE: ["ins:1"]}, page_id="p1")
        raise RuntimeError("the site failed after its commit")

    seam = WsgiSeam(wsgi_app)
    payload = {"http": {"method": "GET", "path": "/", "cid": "c1"}, "identity": "alice"}
    with pytest.raises(RuntimeError):
        lane.worker._serve_on_thread(seam, payload)
    assert [deposit["table"] for deposit in lane.desk.page_dbevent_map["p2"]] == [TABLE]


def test_a_refused_deposit_never_replaces_the_sites_own_exception(two_pages, monkeypatch):
    lane, other = two_pages

    async def refusing_call(path, data=None):
        raise CommanderCallFailed(path, "refused for the test")

    monkeypatch.setattr(lane.worker, "call", refusing_call)

    def wsgi_app(environ, start_response):
        lane.worker.notifyDbEvents("alice", dbevents={TABLE: ["ins:1"]}, page_id="p1")
        raise RuntimeError("the site failed after its commit")

    seam = WsgiSeam(wsgi_app)
    payload = {"http": {"method": "GET", "path": "/", "cid": "c1"}, "identity": "alice"}
    with pytest.raises(RuntimeError):
        lane.worker._serve_on_thread(seam, payload)
    assert "p2" not in lane.desk.page_dbevent_map
