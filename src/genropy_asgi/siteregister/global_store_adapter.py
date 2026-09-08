# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GlobalStoreAdapter — the legacy dotted path onto the commander's one dictionary.

The commander owns ``dict[str, Any]`` (genro-asgi #74): literal string keys — a
dot in a key is a character, never a path — opaque values, one FIFO lock, and no
replica anywhere. The legacy site instead speaks dotted paths on a Bag
(``CACHE_TS.invoices``, ``tables_user_conf_expire_ts``, ``RESTART_TS``). This
class is the ONLY place where the two meet:

- ``split_global_path`` takes the FIRST path segment as the dictionary key; the
  rest of the path is read and written INSIDE the legacy Bag stored under that
  key, here in the worker. The commander never sees a path, and the leaves are
  never flattened into keys.
- the intended shape is ``{"CACHE_TS": Bag, "globalServices_lastChangedConfigTS":
  Bag, "tables_user_conf_expire_ts": Bag, "RESTART_TS": datetime, "TASK_TS":
  datetime}``: a whole-key write is one ``set``, a subpath write is one keyed
  ``for_update`` turn.

**A subpath is never read normally and written back.** Between the read and the
write another worker may have written a sibling leaf, and the overwrite would
drop it: every subpath write and every subpath delete holds a keyed turn for
the whole read-modify-write. A delete under an ABSENT key aborts its turn — a
normal exit would publish the key as ``None``, which is not the same as absent.

**A ``with globalStore()`` block holds the WHOLE dictionary.** ``open_turn``
enters one ``for_update()`` with no key and keeps the lease in
``_active_global_turn``, a thread-local: every operation of that thread then
works on the private dictionary copy and its contained legacy Bags, and the
exit publishes the whole thing at once (or, on a body that raised, nothing).
The lease is never captured by a caller: a facade resolves the thread's state
on every single operation, so a second ``globalStore()`` obtained inside the
block finds the same turn, and one kept across the exit reaches the master
again. A nested block is refused by the core, whose ``RuntimeError`` travels
out untouched.

**Before the worker attaches** (the site touches its register while it is being
built) a local legacy Bag answers reads and writes. It is local only and is
never uploaded: after the attach the commander alone answers.

**The legacy date boundary, both ways.** The legacy world writes and compares
naive LOCAL clocks; the wire (TYTX) reads a naive datetime as UTC. So a naive
scalar handed to the wire as it is would come back shifted by the local offset.
On the way IN (``wire_value``) a naive datetime gets the local zone attached,
its clock preserved — 12:00 naive becomes 12:00 with the local tzinfo — on every
road to the master: the scalar ``set``, a value written into a turn's private
dictionary, and the whole dictionary a turn republishes. On the way OUT
(``legacy_value``) aware becomes naive local, naive is left alone, and the
leaves of a returned Bag are walked. The same walk is what makes the value a
private copy: nothing the site mutates afterwards reaches the store. A date
INSIDE a legacy Bag needs neither: its typed XML carries the local offset.
"""

from __future__ import annotations

import copy
import datetime
import threading
from typing import Any

from gnr.core.gnrbag import Bag

from .exceptions import GnrDaemonLocked

__all__ = ["GlobalStoreAdapter"]

#: Told apart from a stored ``None``: the key is not in the dictionary at all.
ABSENT = object()


class GlobalStoreAdapter:
    """The legacy global-store surface over the commander's dictionary.

    Args:
        register_client: the :class:`GenropyRegisterClient` whose site this is —
            the way to the hosting worker, and to ``None`` before it attaches.

    Carries both halves: the three named operations
    (``get_global_item``/``set_global_item``/``delete_global_item``) plus the
    whole-store reader ``store_bag``, and the legacy names the site calls
    (``getItem``/``setItem``/``delItem``) — so the object handed out as
    the global register item's ``data`` IS this adapter, resolving the thread's
    turn on every call instead of being a shared Bag.
    """

    def __init__(self, register_client: Any) -> None:
        self.register_client = register_client
        self._active_global_turn = threading.local()
        self.local_bag = Bag()

    # ------------------------------------------------------------------
    # Where the store is, and whose turn is in force
    # ------------------------------------------------------------------

    @property
    def core_store(self) -> Any:
        """The worker's ``GlobalStoreClient``, or None while no worker is attached."""
        worker = self.register_client.spa_worker
        return None if worker is None else worker.global_store

    @property
    def active_turn(self) -> Any:
        """The whole-dictionary lease this thread holds, or None."""
        return getattr(self._active_global_turn, "lease", None)

    def split_global_path(self, path: str | None) -> tuple[str | None, str]:
        """Split a legacy path into the dictionary key and the path inside its value.

        Args:
            path: the legacy dotted path; empty or None means the whole store.

        Returns:
            ``(key, rest)`` — ``rest`` is empty when the path names the key
            alone, and ``key`` is None for the whole store.
        """
        if not path:
            return None, ""
        key, _, rest = path.partition(".")
        return key, rest

    # ------------------------------------------------------------------
    # The turn of a ``with globalStore()`` block
    # ------------------------------------------------------------------

    def open_turn(self) -> None:
        """Take the whole dictionary for this thread, as the block's enter.

        A turn that cannot be acquired — no worker, channel down — becomes
        ``GnrDaemonLocked``, the exception the legacy already catches around a
        store lock. A NESTED block is a different thing: the core refuses it
        with ``RuntimeError`` and that is what the caller must see.
        """
        store = self.core_store
        if store is None:
            raise GnrDaemonLocked("global store turn: no worker attached")
        lease = store.for_update()
        try:
            lease.__enter__()
        except RuntimeError:
            raise
        except Exception as exc:
            raise GnrDaemonLocked(f"global store turn not acquired: {exc}") from exc
        self._active_global_turn.lease = lease

    def close_turn(self, exc_type: Any) -> None:
        """Publish the block's dictionary and release, or release applying nothing.

        Args:
            exc_type: the exception class the block is leaving on, or None.

        The thread-local is cleared FIRST, so the commit — a call of its own,
        which may raise ``GlobalStoreCommitUnconfirmed`` or
        ``CommanderCallFailed`` and does — leaves no turn behind on this thread.
        """
        lease = self.active_turn
        self._active_global_turn.lease = None
        if exc_type is None:
            for key, value in lease.value.items():
                lease.value[key] = self.wire_value(value)
        lease.__exit__(exc_type, None, None)

    # ------------------------------------------------------------------
    # The three operations
    # ------------------------------------------------------------------

    def get_global_item(self, path: str | None, default: Any = None) -> Any:
        """Read one legacy path: the key's value, or a path inside its Bag.

        Args:
            path: the legacy dotted path; empty or None answers the whole store.
            default: what an absent key or an unreachable path answers.
        """
        key, rest = self.split_global_path(path)
        if key is None:
            return self.store_bag
        turn = self.active_turn
        if turn is not None:
            if key not in turn.value:
                return default
            return self._legacy_read(turn.value[key], rest, default)
        store = self.core_store
        if store is None:
            return self.legacy_value(self.local_bag.getItem(path, default))
        value = store.get(key, ABSENT)
        if value is ABSENT:
            return default
        return self._legacy_read(value, rest, default)

    def set_global_item(self, path: str, value: Any = None) -> None:
        """Write one legacy path: the whole key, or one path inside its Bag.

        Args:
            path: the legacy dotted path.
            value: what to store. A global write carries no attributes — no
                caller has ever passed any — so there is nowhere to put them.
        """
        key, rest = self.split_global_path(path)
        if key is None:
            raise ValueError("the global store has no value of its own: a write needs a path")
        turn = self.active_turn
        if turn is not None:
            self._write_into(turn.value, key, rest, value)
            return
        store = self.core_store
        if store is None:
            self.local_bag.setItem(path, self.legacy_value(value))
            return
        if not rest:
            store.set(key, self.wire_value(value))
            return
        with store.for_update(key) as keyed:
            bag = keyed.value if keyed.exists and isinstance(keyed.value, Bag) else Bag()
            bag.setItem(rest, value)
            keyed.value = bag

    def delete_global_item(self, path: str) -> None:
        """Remove one legacy path: the whole key, or one path inside its Bag.

        Args:
            path: the legacy dotted path.

        A subpath under an absent key removes nothing and publishes nothing: the
        turn aborts, because a normal exit would leave the key stored as None.
        """
        key, rest = self.split_global_path(path)
        if key is None:
            raise ValueError("the global store has no value of its own: a delete needs a path")
        turn = self.active_turn
        if turn is not None:
            self._delete_from(turn.value, key, rest)
            return
        store = self.core_store
        if store is None:
            self.local_bag.delItem(path)
            return
        if not rest:
            store.delete(key)
            return
        with store.for_update(key) as keyed:
            if not keyed.exists or not isinstance(keyed.value, Bag):
                keyed.abort()
                return
            bag = keyed.value
            bag.delItem(rest)
            keyed.value = bag

    @property
    def store_bag(self) -> Bag:
        """The whole dictionary as one legacy Bag: the diagnostic reader's answer.

        Each key becomes a node carrying its value, and a Bag value is copied,
        so the answer is a snapshot nobody can write through. Outside a turn it
        costs one whole-store turn, ABORTED as soon as the snapshot is built:
        the read must not publish the dictionary it has just read.
        """
        turn = self.active_turn
        if turn is not None:
            return self._as_legacy_bag(turn.value)
        store = self.core_store
        if store is None:
            return self.legacy_value(self.local_bag)
        with store.for_update() as whole:
            snapshot = self._as_legacy_bag(whole.value)
            whole.abort()
        return snapshot

    # ------------------------------------------------------------------
    # The legacy names the site calls
    # ------------------------------------------------------------------

    def getItem(self, path: str | None, default: Any = None) -> Any:
        """The legacy read name of :meth:`get_global_item`."""
        return self.get_global_item(path, default)

    def setItem(self, path: str, value: Any = None) -> None:
        """The legacy write name of :meth:`set_global_item`."""
        self.set_global_item(path, value)

    def delItem(self, path: str) -> None:
        """The legacy delete name of :meth:`delete_global_item`."""
        self.delete_global_item(path)

    # ------------------------------------------------------------------
    # The legacy boundary: private copies, naive local dates
    # ------------------------------------------------------------------

    def legacy_value(self, value: Any) -> Any:
        """The value as the legacy side must see it: a private copy, naive local dates.

        A Bag is rebuilt node by node — ``Bag.deepcopy`` keeps a node's non-Bag
        value by reference, and that reference is the whole defect — with its
        dates normalized on the way. An aware datetime becomes naive local
        (the legacy world compares naive clocks); a naive one is left alone.
        """
        if isinstance(value, Bag):
            copied = Bag()
            for node in value:
                copied.addItem(
                    node.label, self.legacy_value(node.getStaticValue()), dict(node.getAttr())
                )
            return copied
        if isinstance(value, datetime.datetime) and value.tzinfo is not None:
            return value.astimezone().replace(tzinfo=None)
        if isinstance(value, (dict, list, set)):
            return copy.deepcopy(value)
        return value

    def wire_value(self, value: Any) -> Any:
        """The value as the wire must receive it: a naive datetime gets the local zone.

        TYTX reads a naive datetime as UTC, so a legacy ``datetime.now()`` would
        come back shifted by the local offset. ``astimezone()`` on a naive value
        attaches the process's own zone and keeps the clock, which is exactly
        what the legacy meant. Everything else travels as it is.
        """
        if isinstance(value, datetime.datetime) and value.tzinfo is None:
            return value.astimezone()
        return value

    def _legacy_read(self, value: Any, rest: str, default: Any) -> Any:
        """One stored value, answered for the legacy side; ``rest`` reads into its Bag."""
        if rest:
            if not isinstance(value, Bag):
                return default
            value = value.getItem(rest, default)
        return self.legacy_value(value)

    def _write_into(self, content: dict, key: str, rest: str, value: Any) -> None:
        """Write one path into a dictionary this thread owns (a turn's private copy)."""
        if not rest:
            content[key] = self.wire_value(self.legacy_value(value))
            return
        bag = content.get(key)
        if not isinstance(bag, Bag):
            bag = Bag()
            content[key] = bag
        bag.setItem(rest, self.legacy_value(value))

    def _delete_from(self, content: dict, key: str, rest: str) -> None:
        """Remove one path from a dictionary this thread owns (a turn's private copy)."""
        if not rest:
            content.pop(key, None)
            return
        bag = content.get(key)
        if isinstance(bag, Bag):
            bag.delItem(rest)

    def _as_legacy_bag(self, content: dict) -> Bag:
        """One dictionary as one legacy Bag: a node per key, values copied."""
        bag = Bag()
        for key, value in content.items():
            bag.setItem(key, self.legacy_value(value))
        return bag
