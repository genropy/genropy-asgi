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

import datetime
import importlib.util
import sys
import threading
import uuid
from contextlib import contextmanager

import pytest

from genro_asgi.spa import GUEST_PREFIX

_HAS_GNR = importlib.util.find_spec("gnr") is not None
_SITE = "test_invoice_pg"

pytestmark = pytest.mark.skipif(not _HAS_GNR, reason="GenroPy not installed")

# The chains the reader/demolition race test churns through, and the FIXED
# reader budget it spins for (bounded: the test ends even if the churner stalls).
CHURN_CHAINS = 150
SPIN_ROUNDS = 400


@contextmanager
def call_sink(worker):
    """The old base required an open CALL sink around every op; the new base's
    verbs announce straight onto ``worker_events``. Kept as a no-op so the
    lifecycle helpers read unchanged across the rebase."""
    yield


@pytest.fixture(scope="module")
def lane():
    """One live lane — worker, handler, desk — for the whole module."""
    from tests.lane import start_site_lane

    try:
        instance = start_site_lane(_SITE)
    except Exception as exc:  # site missing or broken: skip, don't fail
        pytest.skip(f"cannot build the {_SITE} site: {exc}")
    yield instance
    instance.stop()


@pytest.fixture(scope="module")
def worker(lane):
    """The lane's GenropyWorker: a real GnrWsgiSite behind the full protocol."""
    return lane.worker


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


def open_tab(client, worker, cid, **page_fields):
    """A second tab: another page on a connection that already exists."""
    page_id = f"p_{uuid.uuid4().hex[:8]}"
    with call_sink(worker):
        client.new_page(page_id, None, connection_id=cid, **page_fields)
    return page_id


def login(client, worker, cid, name, user_name=None):
    """The login path: the avatar re-labels the live connection (login-stays).

    The user id is made unique per call — the worker (and so the user register)
    lives for the whole module, and a user entry is created only the first time
    its id is seen: a shared name would reuse an entry another test dressed.
    """
    user = f"{name}_{uuid.uuid4().hex[:8]}"
    with call_sink(worker):
        client.change_connection_user(cid, user=user, user_name=user_name)
    return user


# ------------------------------------------------------------------
# Lifecycle: reception, login-stays, demolition
# ------------------------------------------------------------------


def test_new_connection_is_born_guest_with_live_data_bag(client, worker):
    from gnr.core.gnrbag import Bag

    cid, _ = fresh_ids()
    with call_sink(worker):
        item = client.new_connection(cid)
    assert item["register_item_id"] == cid
    assert item["user"] == GUEST_PREFIX + cid  # born guest: the core mints the name
    assert isinstance(item["data"], Bag)
    # one live Bag, two names — ``store`` is the core's and stays on the core row
    assert item["data"] is worker.connection_items.get(cid)["store"]
    assert "store" not in item


def test_new_connection_twice_answers_the_same_row(client, worker):
    cid, _ = fresh_ids()
    with call_sink(worker):
        first = client.new_connection(cid)
        again = client.new_connection(cid)
    assert again["register_item_id"] == first["register_item_id"]
    assert again["data"] is first["data"]  # the same live row, hence the same Bag


def test_new_page_seed_data_becomes_the_live_store(client, worker):
    from gnr.core.gnrbag import Bag

    seed = Bag()
    seed["rootenv.workdate"] = "2026-08-12"
    _, page_id = open_page(client, worker, data=seed)
    item = client.page(page_id, include_data="lazy")
    assert item["data"] is seed  # the seed IS the live store
    assert worker.page_items.get(page_id)["store"] is seed
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


def test_a_page_close_leaves_the_connection_alive(client, worker):
    # The legacy contract: a closed tab never takes the browser with it —
    # gnrwebpage.py:624 passes cascade=False and the onClosedPage beacon
    # (gnrwsgisite.py:1429, fired on every pagehide) passes nothing at all.
    cid, page_id = open_page(client, worker)
    with call_sink(worker):
        client.drop_page(page_id)
    assert client.page(page_id) is None
    assert client.connection(cid) is not None  # its cookie still routes
    with call_sink(worker):
        client.drop_connection(cid)  # leave the module worker clean


def test_an_explicit_cascade_demolishes_the_emptied_chain(client, worker):
    cid, page_id = open_page(client, worker)
    with call_sink(worker):
        client.drop_page(page_id, cascade=True)
    assert client.page(page_id) is None
    # asked for it: the connection went with its last page
    assert client.connection(cid) is None


def test_drop_page_on_a_gone_page_is_a_noop(client, worker):
    client.drop_page("never_registered")  # no raise: a page may expire first
    cid, page_id = open_page(client, worker)
    with call_sink(worker):
        client.drop_page(page_id)
        client.drop_page(page_id)  # the double pagehide beacon: same no-op
        client.drop_connection(cid)  # leave the module worker clean


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
    # the client's clock, converted datetime -> epoch at the boundary: the row
    # keeps the core's own stamp type (the freeze valve compares floats)
    assert page["last_user_ts"] == client_clock.timestamp()
    assert user_item is worker.user_items.get(GUEST_PREFIX + cid)
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
    guest = GUEST_PREFIX + cid  # the legacy addresses the user store by page.user
    client.setStoreSubscription(page_id, storename="user", client_path="chat", active=True)
    client.set_datachange(guest, "chat.room1", value="ping", register_name="user")
    # the write rides the request slot and is applied at the exchange (the
    # core's addressed-write design); the pull retires it, the user_view
    # captures it, and the envelope keeps the daemon contract: only the
    # explicitly written leaf travels, the autocreated parent stays internal
    changes = client.subscription_storechanges(None, page_id)
    assert [c.path for c in changes] == ["chat.room1"]
    assert changes[0].value == "ping"
    # ...and by then the write has landed on the live user store
    assert client.user(guest, include_data="lazy")["data"]["chat.room1"] == "ping"


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
    with client.userStore(GUEST_PREFIX + cid) as store:
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
    # the capture is live: a write under the prefix becomes a pending change,
    # delivered leaf-only (the autocreated parent never reaches the envelope)
    with client.pageStore(page_id) as store:
        store.setItem("srv.ctx.flag", True)
    assert [c.path for c in store.datachanges] == ["srv.ctx.flag"]


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
    assert GUEST_PREFIX + cid in client.users()  # the guest user carries the prefix


# ------------------------------------------------------------------
# The legacy row contract: what gnr.* reads off a row WITHOUT a guard
# ------------------------------------------------------------------


def connected_users_row(user, arguments):
    """The per-row body of ``Connection.connected_users_bag``, transcribed.

    ``gnr/web/gnrwebpage_proxy/connection.py:186-212``. That method is a
    ``@public_method`` the chat component polls every 2 seconds
    (``chat_component.py:118``, ``cacheTime=2``), so anything this expression
    raises is a 500 on every poll and an empty user list in the browser. The
    load-bearing lines are the two subtractions from ``datetime.now()`` and the
    three keys read with no ``get``.
    """
    now = datetime.datetime.now()
    last_refresh_ts = arguments.get("last_refresh_ts") or arguments["start_ts"]
    last_user_ts = arguments.get("last_user_ts") or arguments["start_ts"]
    return {
        "_pkey": user.replace(".", "_").replace("@", "_"),
        "last_refresh_age": (now - last_refresh_ts).seconds,
        "last_event_age": (now - last_user_ts).seconds,
        "caption": arguments["user_name"] or user,
    }


def test_a_connection_row_answers_the_daemon_key_set(client, worker):
    # The set ``ConnectionRegister.create`` + ``addRegisterItem`` produced
    # (gnr/web/daemon/siteregister.py:339, :135), measured on a legacy trace of
    # the same session: no core field leaks, no daemon field is missing.
    cid, _ = open_page(client, worker)
    assert set(client.connection(cid)) == {
        "register_item_id", "start_ts", "connection_name", "user", "user_id",
        "user_name", "user_tags", "user_ip", "user_agent", "electron_static",
        "browser_name", "pages", "avatar_extra", "register_name", "datachanges",
        "datachanges_idx", "subscribed_paths",
    }
    # the daemon's create() seeds no ``avatar_extra`` and the login writes it
    # (``Connection.change_user``, connection.py:169). Here the key is always
    # answered and carries None until then: a key with no value and no key at all
    # say the same thing about the state (owner, 2026-08-25), which is why the
    # structural comparison drops null-valued keys from the shape it compares.
    assert client.connection(cid)["avatar_extra"] is None
    with call_sink(worker):
        client.change_connection_user(cid, user="dora", avatar_extra={"email": "d@x"})
    assert client.connection(cid)["avatar_extra"] == {"email": "d@x"}


def test_a_page_row_answers_the_daemon_key_set(client, worker):
    # ``PageRegister.create``:459 + ``addRegisterItem``, and ``data`` only when
    # the caller asks for it — the daemon attached it in ``get_item`` too.
    _, page_id = open_page(client, worker)
    expected = {
        "register_item_id", "pagename", "connection_id", "start_ts",
        "subscribed_tables", "user", "user_ip", "user_agent", "relative_url",
        "register_name", "datachanges", "datachanges_idx", "subscribed_paths",
    }
    assert set(client.page(page_id)) == expected
    assert set(client.page(page_id, include_data="lazy")) == expected | {"data"}


def test_a_user_row_answers_the_daemon_key_set(client, worker):
    # ``UserRegister.create``:319 + ``addRegisterItem``
    cid, _ = open_page(client, worker)
    user = login(client, worker, cid, "elio")
    assert set(client.user(user)) == {
        "register_item_id", "start_ts", "user", "user_id", "user_name", "user_tags",
        "avatar_extra", "connections", "register_name", "datachanges",
        "datachanges_idx", "subscribed_paths",
    }


def test_connected_users_reads_a_refreshed_row(client, worker):
    # a refresh moves the core's clocks, which stay on the core row: the reader
    # still finds its ages, dated from the birth stamp the view carries
    cid, page_id = open_page(client, worker)
    user = login(client, worker, cid, "alice", user_name="Alice A")
    client.refresh(page_id, ts=datetime.datetime.now())
    row = connected_users_row(user, client.users()[user])
    assert row["caption"] == "Alice A"
    assert row["last_refresh_age"] == 0  # just stamped, so it reads as live
    assert row["last_event_age"] == 0


def test_connected_users_reads_a_row_with_no_client_clock(client, worker):
    # the daemon put a client clock on a row only when refresh() reported one, so
    # the legacy view carries none and the reader dates the row from start_ts —
    # the branch the legacy wrote for exactly this case (connection.py:196)
    cid, _ = open_page(client, worker)
    user = login(client, worker, cid, "bruno", user_name="Bruno B")
    arguments = client.users()[user]
    assert "last_user_ts" not in arguments
    assert isinstance(arguments["start_ts"], datetime.datetime)
    assert connected_users_row(user, arguments)["last_event_age"] == 0


def test_connected_users_reads_a_guest_row(client, worker):
    # a guest never logged in, so it has no user_name: the key is the caption
    cid, _ = open_page(client, worker)
    guest = GUEST_PREFIX + cid
    row = connected_users_row(guest, client.users()[guest])
    assert row["caption"] == guest


def test_a_user_row_carries_its_own_name_as_the_user_field(client, worker):
    # The chat keys its rooms on the node ATTRIBUTE, not on the key:
    # prepare_usersbag does setItem(n.attr.user, ...) (chat_component.js:180),
    # and an undefined path crashes the client Bag (htraverse). The daemon
    # seeded user=user on the row (daemon/siteregister.py:319-323); the core
    # keys the entry by name instead, so the legacy view restores the field.
    cid, page_id = open_page(client, worker)
    user = login(client, worker, cid, "carla", user_name="Carla C")
    assert client.users()[user]["user"] == user
    assert client.user(user)["user"] == user
    guest_cid, _ = open_page(client, worker)
    guest = GUEST_PREFIX + guest_cid
    assert client.users()[guest]["user"] == guest  # uniform contract
    # ownership on a PAGE row stays derived, never stored (cemented): the key the
    # daemon carried is answered, and nothing is written on the row to answer it
    assert client.page(page_id)["user"] == user
    assert "user" not in worker.page_items.get(page_id)


def test_stale_connections_finds_no_clock_on_a_connection_row(client, worker):
    # ``datacollector.stale_connections``:54 reads ``c['last_refresh_ts']`` bare,
    # so on a row no refresh has touched it raises — on the daemon too, whose
    # create() seeded no clock either. Parity, and the reader has no caller in
    # genropy: the sweep that does expire rows is the core's, on the core row.
    cid, _ = open_page(client, worker)
    assert "last_refresh_ts" not in client.connections()[cid]
    assert isinstance(worker.connection_items.get(cid)["last_refresh_ts"], float)


def test_a_page_row_carries_its_own_birth_stamp(client, worker):
    # ``gnrasync.registerPage`` reads page_item['start_ts'] with no guard
    _, page_id = open_page(client, worker)
    born = client.page(page_id)["start_ts"]
    assert isinstance(born, datetime.datetime)
    client.refresh(page_id, ts=datetime.datetime.now())
    assert client.page(page_id)["start_ts"] == born  # a birth stamp never moves


def test_the_core_rows_keep_the_stamps_the_sweep_reads(client, worker):
    # the projection is a view: the clocks stay where the expiry sweep reads them
    _, page_id = open_page(client, worker)
    assert "last_refresh_ts" not in client.page(page_id)
    assert isinstance(worker.page_items.get(page_id)["last_refresh_ts"], float)


# ------------------------------------------------------------------
# The user filter: resolved through ownership, not off the page row
# ------------------------------------------------------------------


def test_a_user_filter_finds_every_page_of_that_user(client, worker):
    cid, first_tab = open_page(client, worker)
    second_tab = open_tab(client, worker, cid)
    carla = login(client, worker, cid, "carla")
    other_cid, other_tab = open_page(client, worker)
    login(client, worker, other_cid, "dario")
    matched = client.pages(filters=f"user:{carla}")
    assert sorted(matched) == sorted([first_tab, second_tab])
    assert other_tab not in matched


def test_a_user_addressed_push_reaches_both_tabs_and_nobody_else(client, worker):
    # ``gnr.chat.room_alert``: setInClientData(filters='user:X') — the push that
    # reached nobody while the filter read a field the core does not store
    cid, first_tab = open_page(client, worker)
    second_tab = open_tab(client, worker, cid)
    elena = login(client, worker, cid, "elena")
    other_cid, other_tab = open_page(client, worker)
    login(client, worker, other_cid, "fabio")
    client.setInClientData(
        "gnr.chat.room_alert", value="ring", filters=f"user:{elena}", fired=True
    )
    for page_id in (first_tab, second_tab):
        changes = client.subscription_storechanges(None, page_id)
        assert [(c.path, c.value) for c in changes] == [("gnr.chat.room_alert", "ring")]
    assert client.subscription_storechanges(None, other_tab) == []


def test_an_explicit_page_id_still_delivers(client, worker):
    # no filters: the daemon's other branch, the addressed page and only it
    _, page_id = open_page(client, worker)
    _, bystander = open_page(client, worker)
    client.setInClientData("gnr.msg", value="direct", page_id=page_id)
    assert [c.value for c in client.subscription_storechanges(None, page_id)] == ["direct"]
    assert client.subscription_storechanges(None, bystander) == []


def test_a_non_user_filter_still_reads_the_page_row(client, worker):
    # pagename/user_ip/relative_url keep answering off the row, as the daemon did
    _, page_id = open_page(client, worker, pagename="chatroom", user_ip="10.0.0.9")
    _, bystander = open_page(client, worker, pagename="elsewhere")
    client.setInClientData("gnr.msg", value="by_name", filters="pagename:chatroom")
    assert [c.value for c in client.subscription_storechanges(None, page_id)] == ["by_name"]
    assert client.subscription_storechanges(None, bystander) == []
    assert page_id in client.pages(filters="user_ip:10.0.0.9")


# ------------------------------------------------------------------
# The readers against the demolitions: rows vanish mid-read
# ------------------------------------------------------------------


def test_the_readers_tolerate_rows_demolished_mid_read(client, worker):
    """The race the sweep and every logout create against the read side.

    The readers walk a KEY SNAPSHOT and re-fetch each row without
    ``dispatch_lock`` — they are hot paths, and the lock belongs to the writers
    — so a row demolished on another thread leaves its key behind. Handing that
    ``None`` out is what ``Connection.connected_users_bag`` subscripts on the
    next chat poll (connection.py:195), two seconds later, forever.

    The user-addressed reads ride the same race twice over: ``pages(user=...)``
    and ``connections(user=...)`` iterate the user entry's LIVE edge sets while
    the drops mutate them in place, and the ``user:`` filter walks the
    page -> connection -> user chain while the demolition tears it — the walk
    must skip a gone chain, never raise. The spin is a FIXED budget of rounds
    (bounded even if the churner stalls); each round polls the global readers
    and the three user-addressed reads for a user the churner is dropping.
    """
    chains = []
    for _ in range(CHURN_CHAINS):
        cid, _ = open_page(client, worker)
        open_tab(client, worker, cid)
        chains.append(cid)
    users = [GUEST_PREFIX + cid for cid in chains]
    churn_failure = []

    def churn():
        try:
            for cid in chains:
                with call_sink(worker):
                    client.drop_connection(cid)
        except Exception as exc:  # noqa: BLE001 — reported to the main thread
            churn_failure.append(exc)

    # Force the interpreter to switch threads constantly: the window is between
    # the key snapshot and the row read, a few instructions wide.
    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    churner = threading.Thread(target=churn)
    churner.start()
    try:
        for round_i in range(SPIN_ROUNDS):
            user = users[round_i % len(users)]
            for rows in (
                client.users(),
                client.connections(),
                client.pages(),
                client.pages(user=user),
                client.connections(user=user),
                client.pages(filters=f"user:{user}"),
            ):
                assert None not in rows.values()
    finally:
        sys.setswitchinterval(previous_interval)
        churner.join(timeout=30)
    assert churn_failure == []


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
