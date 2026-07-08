# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Unit tests for the GenropyRegisterClient channel-C plumbing — no daemon, no site.

They exercise the interception helpers on a bare instance (``__new__`` — the client
needs no daemon connection for these paths) wired to stub site/app objects, so they
run with GenroPy installed but WITHOUT a register daemon.
"""

import importlib.util
from types import SimpleNamespace

import pytest

_HAS_GNR = importlib.util.find_spec("gnr") is not None

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")


class StubMailbox:
    def __init__(self, changes_by_page):
        self.changes_by_page = changes_by_page

    def collect(self, page_id):
        return self.changes_by_page.pop(page_id, [])


class StubWorker:
    def __init__(self, name=None):
        self.name = name

    def dispatch(self, op, args, kwargs):
        return None  # no local registry in these units: the page user stays unknown


def make_client(app):
    from genropy_asgi.siteregister.siteregister_client import GenropyRegisterClient

    client = GenropyRegisterClient.__new__(GenropyRegisterClient)
    client.__dict__["site"] = SimpleNamespace(spa_application=app)
    return client


def raw_change(path="gnr.dbchanges.probe_tbl", idx=1):
    return {
        "path": path, "value": [{"dbevent": "U", "pkey": "K1"}],
        "attributes": {"from_page_id": "p1"}, "fired": False,
        "reason": None, "change_idx": idx, "delete": False,
    }


def single_app(mailbox):
    # worker.name None = the single: it pulls from its own commander-of-itself
    return SimpleNamespace(
        worker=StubWorker(),
        mailbox=mailbox,
        app_registry=None,
        collect_datachanges=lambda page_id, user=None: mailbox.collect(page_id),
    )


def test_collect_local_builds_client_datachanges_from_the_mailbox():
    app = single_app(StubMailbox({"p1": [raw_change()]}))
    client = make_client(app)
    changes = client._collect_local_datachanges("p1")
    assert len(changes) == 1
    change = changes[0]
    assert change.path == "gnr.dbchanges.probe_tbl"
    assert change.value == [{"dbevent": "U", "pkey": "K1"}]
    assert change.change_idx == 1
    assert change.change_ts is not None
    # the collect is destructive: a second pull finds nothing
    assert client._collect_local_datachanges("p1") == []


def test_changes_to_bag_numbers_sc_i_with_the_envelope_attrs():
    app = single_app(StubMailbox({"p1": [raw_change(), raw_change(path="x.y", idx=2)]}))
    client = make_client(app)
    bag = client._changes_to_bag(client._collect_local_datachanges("p1"))
    assert len(bag) == 2
    node = bag.getNode("sc_0")
    assert node.attr["change_path"] == "gnr.dbchanges.probe_tbl"
    assert node.attr["change_ts"] is not None
    assert bag.getNode("sc_1").attr["change_path"] == "x.y"


def test_changes_to_bag_is_none_when_empty():
    client = make_client(single_app(StubMailbox({})))
    assert client._changes_to_bag([]) is None


def test_pool_child_without_commander_url_collects_nothing(monkeypatch):
    monkeypatch.delenv("GENRO_COMMANDER_URL", raising=False)
    # a named worker = pool child: it must NOT touch a mailbox, only the commander
    app = SimpleNamespace(worker=StubWorker(name="pool_01"), mailbox=None,
                          app_registry=None)
    client = make_client(app)
    assert client._collect_local_datachanges("p1") == []


def test_pool_child_pulls_from_the_commander_endpoint(monkeypatch):
    monkeypatch.setenv("GENRO_COMMANDER_URL", "http://commander.test")

    class StubResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"datachanges": [raw_change()]}

    class StubHttpClient:
        def __init__(self):
            self.calls = []

        def post(self, path, params=None):
            self.calls.append((path, params))
            return StubResponse()

    app = SimpleNamespace(worker=StubWorker(name="pool_01"), mailbox=None,
                          app_registry=None)
    client = make_client(app)
    stub = StubHttpClient()
    client.__dict__["_commander_client"] = stub
    changes = client._collect_local_datachanges("p1")
    assert stub.calls == [("/_commander/datachanges", {"page_id": "p1"})]
    assert changes[0].path == "gnr.dbchanges.probe_tbl"
    # with the user known, the pull carries it (channel D served by the commander)
    client._collect_local_datachanges("p1", user="u1")
    assert stub.calls[1] == ("/_commander/datachanges", {"page_id": "p1", "user": "u1"})


def test_post_commands_fold_to_the_worker_and_are_explicit_methods():
    # Each POST command is an explicit public method that folds to the worker; there is
    # no _sr_call funnel and no per-string dispatch table.
    folded = []
    client = make_client(single_app(StubMailbox({})))
    client.__dict__["_fold"] = lambda op, args=(), kwargs=None: folded.append(op)
    client.subscribeTable("p1", table="probe.tbl")
    client.notifyDbEvents({"probe.tbl": ["evt"]})
    client.setStoreSubscription("p1", storename="user", client_path="chat", active=True)
    client.set_datachange("p1", "some.path", register_name="page", value=1)
    assert folded == ["subscribeTable", "notifyDbEvents", "setStoreSubscription", "set_datachange"]


def test_unknown_command_is_a_plain_attribute_error():
    # A command that is not a method here is not served — a deterministic AttributeError,
    # never a silent fallback to a daemon.
    client = make_client(single_app(StubMailbox({})))
    with pytest.raises(AttributeError):
        client.someUnknownCommand("p1")
