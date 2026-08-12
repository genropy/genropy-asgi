# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Legacy-Bag capture and the ``::BAG`` wire type for the GenroPy bridge.

Two pieces, one module:

- ``LegacyBagCollector`` — the datachange capture attached to a legacy
  ``gnr.core.gnrbag.Bag``, implementing the collector contract the core
  registry and worker consume (``genro_bag.datachange.DataChangeCollector``
  is the model): ``drain(reset=True)``, ``append(change, replace=False)``,
  ``reset()``, ``drop(prefix)``, ``subscribe_path``/``unsubscribe_path``,
  ``detach()``, the ``changes`` list and the ``pending`` count. A change is
  genro-bag's plain dict — ``{key: {path, reason, fired}, value, attributes,
  delete, change_ts (aware UTC), change_idx}`` — so a change born on a
  legacy store travels the same machinery as one born on a core store.

- the ``::BAG`` wire type — importing this module registers the legacy Bag
  with genro-tytx under the code ``BAG``, the legacy's own historical
  datatype code: serialized as its typed XML (``toXml(catalog=...)``),
  parsed back with ``Bag(text)``. The new ``genro_bag.Bag`` keeps ``::X``;
  the two types are NEVER converted into each other.

Capture subscribes the legacy Bag with ``subscribe(id, any=callback)`` — the
legacy trigger vocabulary: an update event's ``pathlist`` already ends with
the node's label, an ``ins``/``del`` event's does not, so the full path is
rebuilt with the label there. Prefixes match on segment boundaries (``a.b``
captures ``a.b.c``, never ``a.bc``); ``paths=None`` captures everything, an
empty set captures nothing. The legacy Bag has no transaction rail, so the
three plain events are the whole capture surface. Locally captured changes
always carry ``fired=False``; a consumer forwarding a fire event supplies
``fired=True`` on the change it appends.

``append`` deposits a shallow copy and assigns ``change_idx`` to that copy
only — the caller's dict is never mutated, so one change can be forwarded to
any number of collectors. With ``replace=True`` the pending change carrying
an equal ``key`` is removed first: the coalesced change gets a fresh
``change_idx`` and goes to the tail. ``detach()`` stops capture and leaves
the pending list untouched.

This module imports ``gnr.*`` at the top BY DESIGN: it exists only where
GenroPy is installed, and the ``::BAG`` registration must happen at import.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from genro_tytx import register_type
from gnr.core.gnrbag import Bag
from gnr.core.gnrclasses import GnrClassCatalog

__all__ = ["LegacyBagCollector"]


def _bag_to_tytx(bag: Bag) -> str:
    """Serialize a legacy Bag to its typed XML — the ``::BAG`` wire payload."""
    return bag.toXml(catalog=GnrClassCatalog.convert())


def _bag_from_tytx(text: str) -> Bag:
    """Parse a ``::BAG`` wire payload back into a legacy Bag."""
    return Bag(text)


register_type(Bag, "BAG", _bag_to_tytx, _bag_from_tytx)


class LegacyBagCollector:
    """Capture the changes made to a legacy Bag, ready to be drained."""

    def __init__(self, bag: Bag, paths: set[str] | list[str] | None = None) -> None:
        """Attach a collector to a legacy Bag.

        Args:
            bag: The legacy Bag to observe.
            paths: Prefixes to capture. None captures every write.
        """
        self.bag = bag
        self.paths = None if paths is None else set(paths)
        self.changes: list[dict[str, Any]] = []
        self.subscriber_id = f"legacy_datachange_{id(self)}"
        self._change_idx = 0
        bag.subscribe(self.subscriber_id, any=self._on_event)

    # -------------------- capture --------------------------------

    def _on_event(
        self,
        node: Any = None,
        pathlist: Any = None,
        evt: str | None = None,
        oldvalue: Any = None,
        ind: Any = None,
        reason: Any = None,
    ) -> None:
        """One callback for the three legacy events (the ``any=`` subscription).

        ``ins``/``del`` carry the parent's pathlist, so the full path is
        rebuilt with the node's label; the update events (``upd_value``,
        ``upd_attrs``) already carry it. ``oldvalue`` and ``ind`` belong to
        the legacy signature and are not part of the change.
        """
        if evt in ("ins", "del"):
            path = ".".join(list(pathlist or []) + [node.label])
        else:
            path = ".".join(list(pathlist or []))
        if not self._captures(path):
            return
        delete = evt == "del"
        value = None if delete else node.staticvalue
        attributes = dict(node.attr) or None
        self._deposit(self._build_change(path, value, attributes, reason, delete))

    # -------------------- change construction --------------------------------

    def _under(self, path: str, prefix: str) -> bool:
        """Tell whether path is the prefix or sits below it, on a segment boundary."""
        return path == prefix or path.startswith(f"{prefix}.")

    def _captures(self, path: str) -> bool:
        """Tell whether path falls under one of the subscribed prefixes."""
        if self.paths is None:
            return True
        return any(self._under(path, p) for p in self.paths)

    def _build_change(
        self,
        path: str,
        value: Any,
        attributes: dict[str, Any] | None,
        reason: str | None,
        delete: bool,
        fired: bool = False,
    ) -> dict[str, Any]:
        """Build a change dict. The only place the shape is written."""
        return {
            "key": {"path": path, "reason": reason, "fired": fired},
            "value": value,
            "attributes": attributes,
            "delete": delete,
            "change_ts": datetime.now(UTC),
            "change_idx": 0,
        }

    def _deposit(self, change: dict[str, Any]) -> None:
        """Assign the next change_idx and append the change to the pending list."""
        self._change_idx += 1
        change["change_idx"] = self._change_idx
        self.changes.append(change)

    # -------------------- consumption --------------------------------

    def drain(self, reset: bool = True) -> list[dict[str, Any]]:
        """Return the pending changes ordered by change_idx.

        Args:
            reset: True empties the pending list, False leaves it intact.
        """
        changes = sorted(self.changes, key=lambda change: change["change_idx"])
        if reset:
            self.changes = []
        return changes

    def append(self, change: dict[str, Any], replace: bool = False) -> None:
        """Deposit a change that was not born from a local write.

        The collector deposits a shallow copy and assigns ``change_idx`` to
        that copy only: the caller's dict is never mutated.

        Args:
            change: The change dict, forwarded from elsewhere.
            replace: True removes the pending change with an equal ``key``
                first, so the two coalesce into the appended one.
        """
        if replace:
            self.changes = [c for c in self.changes if c["key"] != change["key"]]
        self._deposit(dict(change))

    def drop(self, prefix: str) -> None:
        """Discard the pending changes whose path falls under a prefix."""
        self.changes = [c for c in self.changes if not self._under(c["key"]["path"], prefix)]

    def reset(self) -> None:
        """Empty the pending list without reading it."""
        self.changes = []

    # -------------------- prefix set --------------------------------

    def subscribe_path(self, prefix: str) -> None:
        """Add a prefix to the captured set.

        On a collector capturing everything (``paths=None``) this starts
        restricting capture to the given prefix.
        """
        if self.paths is None:
            self.paths = set()
        self.paths.add(prefix)

    def unsubscribe_path(self, prefix: str) -> None:
        """Remove a prefix from the captured set.

        Removing the last prefix leaves an empty set, which captures nothing:
        it does not restore the capture-everything state that only
        ``paths=None`` means.
        """
        if self.paths is not None:
            self.paths.discard(prefix)

    # -------------------- lifecycle --------------------------------

    @property
    def pending(self) -> int:
        """Number of changes waiting to be drained."""
        return len(self.changes)

    def detach(self) -> None:
        """Stop capturing. Pending changes are left untouched."""
        self.bag.unsubscribe(self.subscriber_id, any=True)
