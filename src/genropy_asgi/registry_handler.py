# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""RegistryHandler — the worker_server's local registries and event processor.

Every register mutation the GenroPy site performs is enqueued raw on the site
register client's ``commander_queue`` (``{ts, op, args, kwargs}``) by the worker
threads. At the end of each request the worker_server drains that queue and hands
the messages here.

The handler is the worker_server's brain for shared state: it *steals* from each
raw event what it needs for its own local registries (users, connections, pages,
global_store) and *re-emits* the part the commander needs onto the response
piggyback. The commander event is built BEFORE the local steal, so a drop event
still sees the entry it is about to remove.

The registries are plain dicts on purpose (we are building incrementally): we
will learn from real data which fields matter before reaching for richer types.
This is the in-process state that, in the daemon-less model, replaces what the
daemon holds today.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("genropy_asgi.registry")


def _arg(args: tuple, kwargs: dict, index: int, *names: str) -> Any:
    """Read a value either positionally (args[index]) or by keyword name."""
    if len(args) > index:
        return args[index]
    for name in names:
        if name in kwargs:
            return kwargs[name]
    return None


class RegistryHandler:
    """Local registries of one worker_server, fed by the raw register events.

    Owned by the worker_server (semantic parent: ``self.worker``). ``process``
    is called at the end of each request with the messages drained from the
    site register client's queue; it updates the local registries and returns
    the (currently empty) list of events to piggyback to the commander.
    """

    # Events the commander needs for sticky routing: a connection is born here
    # (new_connection), a user lands on this worker at login
    # (change_connection_user), and leaves on logout/drop (so the commander can
    # drop the affinity). The commander mints the gnr_cid cookie on new_connection
    # and routes every later request by that connection_id. Page and guest-only
    # events are the worker's own business and stay local.
    _FOR_COMMANDER = frozenset(
        {
            "new_connection",
            "change_connection_user",
            "drop_connection",
            "drop_connections",
            "drop_user",
        }
    )

    def __init__(self, worker: Any) -> None:
        """Args:
        worker: the worker_server owning this handler (semantic parent).
        """
        self.worker = worker
        self.worker_name = os.environ.get("GENRO_WORKER_NAME")
        self.users: dict[str, dict[str, Any]] = {}
        self.connections: dict[str, dict[str, Any]] = {}
        self.pages: dict[str, dict[str, Any]] = {}
        self.global_store: dict[str, Any] = {}

    def process(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Update local registries from raw events; return events for the commander.

        Each message is ``{ts, op, args, kwargs}``. We dispatch on ``op``, steal
        the fields we need into the local registries (always), and re-emit the
        sticky-relevant events tagged with this worker's name (the part the
        commander must see, carried back on the response piggyback).
        """
        for_commander: list[dict[str, Any]] = []
        for message in messages:
            op = message.get("op")
            args = message.get("args") or ()
            kwargs = message.get("kwargs") or {}
            try:
                # Build the commander event BEFORE _steal mutates the local
                # registries: a drop_connection carries only the connection_id, and
                # the username it once indexed is about to be removed — capture what
                # the commander needs while the entry is still there.
                if op in self._FOR_COMMANDER:
                    for_commander.append(self._for_commander(op, args, kwargs))
                self._steal(op, args, kwargs)
            except Exception:
                log.exception("RegistryHandler failed on op %r", op)
        return for_commander

    def _for_commander(self, op: str, args: tuple, kwargs: dict) -> dict[str, Any]:
        """Build the commander-facing event for a sticky-relevant op.

        Tagged with this worker's name so the commander knows where the user is.
        new_connection and drop_connection carry ONLY the connection_id: the
        commander owns the connection_id -> user mapping, so it knows whose
        connection dropped — the worker need not remember the user. The login
        (change_connection_user) and drop_user/drop_connections carry the user,
        which those ops do supply.
        """
        event: dict[str, Any] = {
            "op": op,
            "worker": self.worker_name,
            "connection_id": _arg(args, kwargs, 0, "connection_id"),
        }
        if op == "change_connection_user":
            event["user"] = kwargs.get("user")
        elif op == "drop_user":
            event["user"] = _arg(args, kwargs, 0, "user")
        elif op == "drop_connections":
            event["user"] = _arg(args, kwargs, 0, "user")
        return event

    def _steal(self, op: str, args: tuple, kwargs: dict) -> None:
        """Update the local registries for a single event."""
        if op == "new_connection":
            cid = _arg(args, kwargs, 0, "connection_id")
            self.connections[cid] = {
                "connection_id": cid,
                "user": kwargs.get("user"),
                "user_id": kwargs.get("user_id"),
                "user_ip": kwargs.get("user_ip"),
            }
        elif op == "change_connection_user":
            cid = _arg(args, kwargs, 0, "connection_id")
            conn = self.connections.setdefault(cid, {"connection_id": cid})
            conn["user"] = kwargs.get("user")
            conn["user_id"] = kwargs.get("user_id")
            conn["user_name"] = kwargs.get("user_name")
            user = kwargs.get("user")
            if user:
                self.users[user] = {"user": user, "user_id": kwargs.get("user_id")}
        elif op == "new_user":
            user = _arg(args, kwargs, 0, "user")
            if user:
                self.users[user] = {"user": user}
        elif op == "new_page":
            pid = _arg(args, kwargs, 0, "page_id")
            self.pages[pid] = {
                "page_id": pid,
                "connection_id": kwargs.get("connection_id"),
                "user": kwargs.get("user"),
            }
        elif op == "drop_page":
            self.pages.pop(_arg(args, kwargs, 0, "page_id"), None)
        elif op == "drop_pages":
            cid = _arg(args, kwargs, 0, "connection_id")
            for pid in [p for p, v in self.pages.items() if v.get("connection_id") == cid]:
                self.pages.pop(pid, None)
        elif op == "drop_connection":
            self.connections.pop(_arg(args, kwargs, 0, "connection_id"), None)
        elif op == "drop_connections":
            user = _arg(args, kwargs, 0, "user")
            for cid in [c for c, v in self.connections.items() if v.get("user") == user]:
                self.connections.pop(cid, None)
        elif op == "drop_user":
            self.users.pop(_arg(args, kwargs, 0, "user"), None)

    def pop_user(self, user: str) -> dict[str, Any]:
        """Remove a user's local state and return it, for a move to another worker.

        Collects the user's own registry slices — the user entry, its connections,
        and its pages with their content — and removes them from this worker: after
        pop the user no longer lives here, it lives on the destination. Returns a
        plain dict; the caller pickles it for the opaque move blob.
        """
        connections = {c: v for c, v in self.connections.items() if v.get("user") == user}
        cids = set(connections)
        pages = {p: v for p, v in self.pages.items() if v.get("connection_id") in cids}
        data = {
            "user": user,
            "user_entry": self.users.get(user),
            "connections": connections,
            "pages": pages,
        }
        for cid in cids:
            self.connections.pop(cid, None)
        for pid in pages:
            self.pages.pop(pid, None)
        self.users.pop(user, None)
        return data

    def add_user(self, data: dict[str, Any]) -> None:
        """Add a moved user's state into the local registries.

        The inverse of pop_user: folds the user entry, connections and pages from a
        move blob into this worker, so a request routed here finds the user already
        in place.
        """
        user = data.get("user")
        if user and data.get("user_entry") is not None:
            self.users[user] = data["user_entry"]
        self.connections.update(data.get("connections") or {})
        self.pages.update(data.get("pages") or {})


if __name__ == "__main__":
    pass
