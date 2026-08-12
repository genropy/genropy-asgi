# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Unit tests for GenropyRegisterClient on the core worker ops — no daemon.

State is built through the register's own public commands against a REAL
GenropyWorker hosting the ``test_invoice_pg`` site: lifecycle via
``new_connection``/``new_page``/``change_connection_user``, capture via the
datachange commands, the pull via ``subscription_storechanges``/``handle_ping``.
The lifecycle ops announce on the CALL that causes them, so the tests open the
same sink ``service_call`` opens — the core's own test convention
(genro-asgi tests/test_spa_worker.py, ``call_sink``).

The whole module skips when GenroPy or the site is missing.
"""

import asyncio
import datetime
import importlib.util
import uuid
from contextlib import contextmanager

import pytest

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")


@contextmanager
def call_sink(worker):
    """Open the sinks a CALL would open (the core's own test convention)."""
    events_token = worker._call_events.set([])
    tasks_token = worker._call_tasks.set([])
    try:
        yield
    finally:
        worker._call_events.reset(events_token)
        worker._call_tasks.reset(tasks_token)


@pytest.fixture(scope="module")
def worker():
    """One real GnrWsgiSite hosted by a GenropyWorker for the whole module."""
    from genropy_asgi.spa.genropy_worker import GenropyWorker

    try:
        instance = GenropyWorker("W:test", source=_SITE, debug=False)
    except Exception as exc:  # site missing or broken: skip, don't fail
        pytest.skip(f"cannot build the {_SITE} site: {exc}")
    yield instance
    asyncio.run(instance.shutdown())


@pytest.fixture()
def client(worker):
    """The site's own register client (built by the GnrWsgiSite entry point)."""
    return worker.gnr_site.register


def fresh_ids():
    tag = uuid.uuid4().hex[:8]
    return f"c_{tag}", f"p_{tag}"


def open_page(client, worker, user=None, data=None, **page_fields):
    """Build the chain through the register's own commands (the public path)."""
    cid, page_id = fresh_ids()
    with call_sink(worker):
        client.new_connection(cid, user=user)
        client.new_page(page_id, None, connection_id=cid, user=user, data=data, **page_fields)
    return cid, page_id


# ------------------------------------------------------------------
# Lifecycle: reception, login-stays, demolition
# ------------------------------------------------------------------


def test_new_connection_is_born_guest_with_live_data_bag(client, worker):
    from gnr.core.gnrbag import Bag

    cid, _ = fresh_ids()
    with call_sink(worker):
        item = client.new_connection(cid)
    assert item["register_item_id"] == cid
    assert item["user"] == cid  # born guest: the naked sticky key
    assert isinstance(item["data"], Bag)
    assert item["data"] is item["store"]  # one live Bag, two names


def test_new_connection_twice_returns_the_live_row(client, worker):
    cid, _ = fresh_ids()
    with call_sink(worker):
        first = client.new_connection(cid)
        again = client.new_connection(cid)
    assert again is first


def test_new_page_seed_data_becomes_the_live_store(client, worker):
    from gnr.core.gnrbag import Bag

    seed = Bag()
    seed["rootenv.workdate"] = "2026-08-12"
    _, page_id = open_page(client, worker, data=seed)
    item = client.page(page_id, include_data="lazy")
    assert item["data"] is seed  # the seed IS the live store
    assert item["store"] is seed
    assert client.get_dbenv(page_id)["workdate"] == "2026-08-12"


def test_login_stays_pages_keep_their_worker(client, worker):
    cid, page_id = open_page(client, worker)
    with call_sink(worker):
        item = client.change_connection_user(cid, user="alice", user_id="U1")
    assert item["user"] == "alice"
    assert page_id in client.pages(connection_id=cid)  # the page never moved
    assert worker.registry.page_user(page_id) == "alice"
    assert client.user("alice") is not None
    assert client.connection(cid)["user_id"] == "U1"


def test_drop_page_demolishes_the_emptied_chain(client, worker):
    cid, page_id = open_page(client, worker)
    with call_sink(worker):
        client.drop_page(page_id)
    assert client.page(page_id) is None
    # the core cascade: the connection went with its last page
    assert client.connection(cid) is None


def test_drop_page_on_a_gone_page_is_a_noop(client, worker):
    client.drop_page("never_registered")  # no raise: a page may expire first


def test_logout_drop_connection_demolishes_pages_first(client, worker):
    cid, page_id = open_page(client, worker)
    with call_sink(worker):
        client.drop_connection(cid)
    assert client.page(page_id) is None
    assert client.connection(cid) is None
    with call_sink(worker):
        client.drop_connection(cid)  # double logout: a legitimate no-op


def test_refresh_stamps_server_clock_and_client_fields(client, worker):
    cid, page_id = open_page(client, worker)
    before = worker.page_items.get(page_id)["last_refresh_ts"]
    client_clock = datetime.datetime.now()
    user_item = client.refresh(page_id, ts=client_clock, lastRpc=client_clock)
    page = worker.page_items.get(page_id)
    assert page["last_refresh_ts"] >= before  # the server's own clock
    assert page["last_user_ts"] == client_clock  # the client's, as a plain field
    assert user_item is worker.user_items.get(cid)
    assert client.refresh("never_registered") is None


# ------------------------------------------------------------------
# Datachanges: deposit, user-store write, the pull
# ------------------------------------------------------------------


def test_page_datachange_roundtrip_is_destructive(client, worker):
    _, page_id = open_page(client, worker)
    client.set_datachange(page_id, "chat.msg", value="hello", register_name="page")
    changes = client.subscription_storechanges(None, page_id)
    assert len(changes) == 1
    change = changes[0]
    assert change.path == "chat.msg"
    assert change.value == "hello"
    assert change.change_ts.tzinfo is None  # naive at the legacy boundary
    assert client.subscription_storechanges(None, page_id) == []


def test_datachange_replace_coalesces(client, worker):
    _, page_id = open_page(client, worker)
    client.set_datachange(page_id, "gauge", value=1, register_name="page", replace=True)
    client.set_datachange(page_id, "gauge", value=2, register_name="page", replace=True)
    changes = client.subscription_storechanges(None, page_id)
    assert [c.value for c in changes] == [2]


def test_user_store_write_reaches_the_subscribed_page(client, worker):
    cid, page_id = open_page(client, worker)
    client.setStoreSubscription(page_id, storename="user", client_path="chat", active=True)
    client.set_datachange(cid, "chat.room1", value="ping", register_name="user")
    # the write landed on the live user store...
    assert client.user(cid, include_data="lazy")["data"]["chat.room1"] == "ping"
    # ...and the page's user_view captured it — the legacy pair: the
    # autocreated parent first, then the leaf (the daemon's triggers saw the same)
    changes = client.subscription_storechanges(None, page_id)
    assert [c.path for c in changes] == ["chat", "chat.room1"]
    assert changes[-1].value == "ping"


def test_subscribe_table_and_dbevents_dressed_at_the_envelope(client, worker):
    _, page_id = open_page(client, worker)
    client.subscribeTable(page_id, table="probe.tbl", subscribe=True)
    batch = [{"dbevent": "U", "pkey": "K1"}]
    client.notifyDbEvents({"probe.tbl": batch}, origin_page_id=page_id, dbevent_reason="probe")
    changes = client.subscription_storechanges(None, page_id)
    assert len(changes) == 1  # origin page NOT excluded: legacy semantics
    change = changes[0]
    assert change.path == "gnr.dbchanges.probe_tbl"  # dots dressed as underscores
    assert change.value == batch
    assert change.attributes["from_page_id"] == page_id
    assert change.attributes["dbevent_reason"] == "probe"
    assert client.subscription_storechanges(None, page_id) == []


def test_reset_and_drop_datachanges(client, worker):
    _, page_id = open_page(client, worker)
    client.set_datachange(page_id, "a.x", value=1, register_name="page")
    client.reset_datachanges(page_id, register_name="page")
    assert client.subscription_storechanges(None, page_id) == []
    client.set_datachange(page_id, "a.x", value=1, register_name="page")
    client.set_datachange(page_id, "b.y", value=2, register_name="page")
    client.drop_datachanges(page_id, "a", register_name="page")
    assert [c.path for c in client.subscription_storechanges(None, page_id)] == ["b.y"]


# ------------------------------------------------------------------
# The ping envelope
# ------------------------------------------------------------------


def test_handle_ping_answers_false_for_a_dead_page(client, worker):
    assert client.handle_ping(page_id="never_registered") is False


def test_handle_ping_builds_the_sc_i_envelope(client, worker):
    _, page_id = open_page(client, worker)
    client.set_datachange(page_id, "alert", value="fire", register_name="page")
    envelope = client.handle_ping(page_id=page_id)
    node = envelope.getNode("dataChanges.sc_0")
    assert node.attr["change_path"] == "alert"
    assert node.value == "fire"
    # drained: the next ping carries no dataChanges
    assert client.handle_ping(page_id=page_id).getItem("dataChanges") is None


def test_handle_ping_flags_the_running_batch_window(client, worker):
    cid, page_id = open_page(client, worker)
    with client.userStore(cid) as store:
        store.setItem("lastBatchUpdate", datetime.datetime.now())
    envelope = client.handle_ping(page_id=page_id)
    assert envelope.getItem("runningBatch") is True


# ------------------------------------------------------------------
# ServerStore: the peek that heals the serverbatch defect
# ------------------------------------------------------------------


def test_serverstore_datachanges_peeks_without_consuming(client, worker):
    _, page_id = open_page(client, worker)
    client.set_datachange(page_id, "thermo.q", value=42, register_name="page")
    store = client.pageStore(page_id)
    assert [c.path for c in store.datachanges] == ["thermo.q"]
    assert [c.path for c in store.datachanges] == ["thermo.q"]  # still pending
    assert [c.path for c in client.subscription_storechanges(None, page_id)] == ["thermo.q"]


def test_serverstore_subscribed_paths_mirrors_the_capture(client, worker):
    _, page_id = open_page(client, worker)
    client.subscribe_path(page_id, "srv.ctx", register_name="page")
    assert "srv.ctx" in client.pageStore(page_id).subscribed_paths
    # the capture is live: a write under the prefix becomes a pending change
    # (the legacy pair — autocreated parent, then the leaf)
    with client.pageStore(page_id) as store:
        store.setItem("srv.ctx.flag", True)
    assert [c.path for c in store.datachanges] == ["srv.ctx", "srv.ctx.flag"]


# ------------------------------------------------------------------
# Reads and the envelope helper
# ------------------------------------------------------------------


def test_pages_reads_and_the_filter_grammar(client, worker):
    cid, page_id = open_page(client, worker, pagename="probe_page")
    everything = client.pages()
    assert page_id in everything
    assert page_id in client.pages(connection_id=cid)
    assert page_id in client.pages(filters="pagename:probe.*")
    assert page_id not in client.pages(filters="pagename:elsewhere")
    assert client.exists(page_id, register_name="page")
    assert cid in client.connections()
    assert cid in client.users()  # the guest user is the cid itself


def test_changes_to_bag_numbers_sc_i_with_the_envelope_attrs(client):
    from gnr.web.gnrwebpage import ClientDataChange

    changes = [
        ClientDataChange("gnr.dbchanges.probe_tbl", [{"dbevent": "U"}], change_idx=1),
        ClientDataChange("x.y", 5, change_idx=2),
    ]
    bag = client._changes_to_bag(changes)
    assert len(bag) == 2
    node = bag.getNode("sc_0")
    assert node.attr["change_path"] == "gnr.dbchanges.probe_tbl"
    assert node.attr["change_ts"] is not None
    assert bag.getNode("sc_1").attr["change_path"] == "x.y"
    assert client._changes_to_bag([]) is None


def test_unknown_command_is_a_plain_attribute_error(client):
    # A command that is not a method here is not served — a deterministic
    # AttributeError, never a silent fallback to a daemon.
    with pytest.raises(AttributeError):
        client.someUnknownCommand("p1")
