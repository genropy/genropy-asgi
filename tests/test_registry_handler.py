# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Tests for RegistryHandler: local registries + the commander piggyback.

The handler steals from each raw register event what its local registries need
and re-emits the slice the commander needs (_FOR_COMMANDER), built BEFORE the
local steal so a drop still sees the entry it is about to remove.
"""

import os

from genropy_asgi.registry_handler import RegistryHandler


def _msg(op: str, *args, **kwargs) -> dict:
    return {"ts": 0, "op": op, "args": list(args), "kwargs": kwargs}


def _handler(worker_name: str = "pool-1") -> RegistryHandler:
    os.environ["GENRO_WORKER_NAME"] = worker_name
    return RegistryHandler(worker=object())


def test_new_connection_piggybacks_to_commander():
    # new_connection is in _FOR_COMMANDER: it carries the connection_id (born guest)
    # so the commander can register it and mint the gnr_cid cookie.
    handler = _handler()
    out = handler.process([_msg("new_connection", "c1", user="guest_c1")])
    assert out == [{"op": "new_connection", "worker": "pool-1", "connection_id": "c1"}]
    assert "c1" in handler.connections  # also stolen locally


def test_login_piggybacks_user_and_worker():
    handler = _handler()
    out = handler.process([_msg("change_connection_user", "c1", user="amelia.martin")])
    assert out == [
        {"op": "change_connection_user", "worker": "pool-1",
         "connection_id": "c1", "user": "amelia.martin"}
    ]


def test_drop_connection_carries_only_the_connection_id():
    # The commander owns the cid -> user map: a drop_connection need not carry the user.
    handler = _handler()
    handler.process([_msg("change_connection_user", "c1", user="amelia.martin")])
    out = handler.process([_msg("drop_connection", "c1")])
    assert out == [{"op": "drop_connection", "worker": "pool-1", "connection_id": "c1"}]
    assert "c1" not in handler.connections


def test_worker_local_ops_do_not_piggyback():
    # new_user / pages are the worker's own business; nothing goes to the commander.
    handler = _handler()
    out = handler.process(
        [
            _msg("new_user", "amelia.martin"),
            _msg("new_page", "p1", connection_id="c1", user="amelia.martin"),
            _msg("drop_page", "p1"),
        ]
    )
    assert out == []
    assert "amelia.martin" in handler.users


def test_pop_user_removes_and_returns_user_slice():
    # pop_user collects the user's entry, connections and pages, and removes them.
    handler = _handler()
    handler.process(
        [
            _msg("change_connection_user", "c1", user="amelia.martin"),
            _msg("new_page", "p1", connection_id="c1", user="amelia.martin"),
            _msg("new_connection", "c2", user="bob.jones"),  # another user stays
        ]
    )
    data = handler.pop_user("amelia.martin")
    assert data["user"] == "amelia.martin"
    assert "c1" in data["connections"] and "p1" in data["pages"]
    # removed from this worker
    assert "amelia.martin" not in handler.users
    assert "c1" not in handler.connections and "p1" not in handler.pages
    # the other user is untouched
    assert "c2" in handler.connections


def test_add_user_round_trip():
    # add_user on a fresh worker installs exactly what pop_user handed over.
    source = _handler("pool-1")
    source.process(
        [
            _msg("change_connection_user", "c1", user="amelia.martin"),
            _msg("new_page", "p1", connection_id="c1", user="amelia.martin"),
        ]
    )
    blob = source.pop_user("amelia.martin")

    dest = _handler("pool-2")
    dest.add_user(blob)
    assert "amelia.martin" in dest.users
    assert "c1" in dest.connections and "p1" in dest.pages
