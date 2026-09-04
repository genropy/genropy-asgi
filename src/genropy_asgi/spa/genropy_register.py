# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyRegistry and GenropyPageRow: the core registry carrying the site's page row.

The core registry knows three rows and a store factory; what a GenroPy page
queues, subscribes and watches is the bridge's to add (genro-asgi #59, block
4). This module adds exactly that:

:class:`GenropyPageRow` is the core ``PageRow`` with the fields of a hosted
page:

- ``datachanges`` / ``datachanges_idx``: the row's own queue of changes bound
  for the browser and the index stamped on the last one;
- ``user_view``: ``None`` until the first ``subscribe_store_path``, then the
  collector that watches the owner user's store for the page — a live object
  bound to a Bag of this process, so the parcel leaves it behind and the wake
  rebuilds it from ``store_subscriptions``;
- ``subscribed_paths``: the prefixes of the page's OWN store the capture
  queues writes under;
- ``store_subscriptions``: the prefixes of the USER store the view watches;
- ``table_subscriptions``: the tables the page subscribes — what the birth
  announces to the vertex, which rebuilds its table index from it.

The three sets travel in the parcel and are replayed on the woken row, so a
page wakes capturing what it captured before it went to the deposit. The
row has no ``dbevents`` field: nothing writes one — a db event reaches the
page through the vertex's desk and the request slot, never through the row.

:class:`GenropyRegistry` names the row (``page_row_class``) and overrides the
core seams that touch a Bag's API, and nothing else: ``new_store()`` returns a
legacy ``gnr.core.gnrbag.Bag``, ``new_collector(store, paths)`` returns a
``LegacyBagCollector``, and ``subscribe_page_store``/``detach_page`` attach and
remove the row-queue capture with the legacy ``subscribe(id, any=...)`` (no
``transaction`` kind exists there) — every row of the chain (user stores,
page stores, the views) is a legacy Bag, so legacy values ride the registers
untranslated (cemented rule B1). A legacy store travels a move pickled whole:
the legacy Bag drops its subscribers at ``__getstate__``, and the registry
re-attaches the capture on arrival.

The queue and the view are the registry's too: ``append_page_datachange`` is
the ONE append to a page row's queue (the store capture and the addressed
write share it, so the row has one list and one index);
``subscribe_store_path`` opens or widens the page's ``user_view``; and
``change_connection_user`` re-attaches every view of the moved connection's
pages on the NEW owner's store after the core has re-labelled the connection —
the old collector is detached, a new one is created with the very same
``store_subscriptions`` prefixes and re-deposited with everything the old one
still held, never drained. The guest-transfer path of the core keeps the very
same store object, so its views need no re-attaching and get none.

This module imports ``gnr.*`` at the top BY DESIGN (via ``legacy_bag``): it is
loaded through the worker only where GenroPy is installed.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from genro_asgi.spa import PageRow, RegisterRegistry
from gnr.core.gnrbag import Bag

from .legacy_bag import LegacyBagCollector

__all__ = ["GenropyPageRow", "GenropyRegistry"]


class GenropyPageRow(PageRow):
    """The page's row with the fields of a hosted GenroPy page (see the module docstring)."""

    fields_left_behind = PageRow.fields_left_behind | {"user_view"}
    fields_replayed = ("subscribed_paths", "store_subscriptions", "table_subscriptions")

    def default_fields(self) -> dict[str, Any]:
        return {
            **super().default_fields(),
            "datachanges": [],
            "datachanges_idx": 0,
            "user_view": None,
            "subscribed_paths": set(),
            "store_subscriptions": set(),
            "table_subscriptions": set(),
        }

    def replay_fields(self, registry: Any, fields: dict[str, Any]) -> None:
        """Subscribe again what the parcel carried: tables, store prefixes, user-store prefixes.

        Args:
            registry: the registry the row lives in — the user-store prefixes
                go through its ``subscribe_store_path``, which builds the view.
            fields: the replayed fields as the parcel carried them.
        """
        for table in fields.get("table_subscriptions", ()):
            self["table_subscriptions"].add(table)
        for prefix in fields.get("subscribed_paths", ()):
            self["subscribed_paths"].add(prefix)
        for prefix in fields.get("store_subscriptions", ()):
            registry.subscribe_store_path(self["register_item_id"], prefix)

    def announcement_fields(self) -> dict[str, Any]:
        """The tables this page subscribes: what the vertex rebuilds its index from."""
        return {"table_subscriptions": sorted(self["table_subscriptions"])}


class GenropyRegistry(RegisterRegistry):
    """The core registry with the site's page row and legacy stores: every row's Bag is a gnr Bag."""

    page_row_class = GenropyPageRow

    def new_store(self) -> Any:
        """The store factory: a legacy ``gnr.core.gnrbag.Bag``."""
        return Bag()

    def new_collector(self, store: Any, paths: set[str] | None = None) -> Any:
        """The capture factory: a ``LegacyBagCollector`` on a legacy store."""
        return LegacyBagCollector(store, paths=paths)

    def subscribe_page_store(self, page: dict[str, Any]) -> None:
        """Attach to a page's legacy store the capture that fills the row's queue.

        The legacy Bag has one ``any=`` callback and no transactions, so the
        capture reads the legacy events: ``ins``/``del`` carry the parent's
        pathlist and the path is completed with the node's label, ``upd_*``
        carry it whole. The rest is the contract the core's capture kept:
        ``autocreate`` skipped, prefixes read from ``subscribed_paths`` at event
        time and matched on segment boundaries, reason ``serverChange``, the
        transient ``_fired`` popped from a copy of the attributes. The append
        runs under the row's ``item_lock``: ``append_page_datachange`` expects
        its caller to hold it, and the writer here is the site's ServerStore,
        which holds no lock.
        """
        store = page["store"]

        def on_event(
            node: Any = None,
            pathlist: Any = None,
            evt: str | None = None,
            oldvalue: Any = None,
            ind: Any = None,
            reason: Any = None,
        ) -> None:
            if reason == "autocreate":
                return
            if evt in ("ins", "del"):
                path = ".".join(list(pathlist or []) + [node.label])
            else:
                path = ".".join(list(pathlist or []))
            paths = page["subscribed_paths"]
            if not any(path == p or path.startswith(f"{p}.") for p in paths):
                return
            delete = evt == "del"
            attributes = dict(node.attr)
            fired = bool(attributes.pop("_fired", False))
            change = {
                "key": {"path": path, "reason": "serverChange", "fired": fired},
                "value": None if delete else node.staticvalue,
                "attributes": attributes or None,
                "delete": delete,
                "change_ts": datetime.now(UTC),
            }
            with page["item_lock"]:
                self.append_page_datachange(page, change)

        store.subscribe(f"page_store:{page['register_item_id']}", any=on_event)

    def detach_page(self, page: dict[str, Any]) -> None:
        """Stop a page row's capture and its view: on a legacy Bag ``any=True`` is the whole subscription.

        The core calls this with the live row before a drop, and with the
        parcel's fields before the copy for a move — a dict without the fields
        the parcel leaves behind, ``user_view`` among them.
        """
        page["store"].unsubscribe(f"page_store:{page['register_item_id']}", any=True)
        view = page.get("user_view")
        if view is not None:
            view.detach()

    def append_page_datachange(
        self, page: dict[str, Any], change: dict[str, Any], *, replace: bool = False
    ) -> None:
        """Append one change to a page row's queue, stamping the next index.

        Args:
            page: the page row whose ``datachanges``/``datachanges_idx`` grow.
            change: the change dict; its ``change_idx`` and ``arrival_ts`` are
                stamped here.
            replace: drop the pending change of the same ``key`` first — same
                path, same reason, same fired — so a value written over and
                over reaches the browser once.

        The caller holds the row's ``item_lock``. ``arrival_ts`` is the
        wall-clock instant of the append — the instant the daemon's single list
        would have received it — and it is what ``collect_page`` merges the row
        with the desk's queue on; the writer's own ``change_ts`` is left as it came.
        """
        if replace:
            page["datachanges"][:] = [
                pending for pending in page["datachanges"] if pending["key"] != change["key"]
            ]
        page["datachanges_idx"] += 1
        change["change_idx"] = page["datachanges_idx"]
        change["arrival_ts"] = time.time()
        page["datachanges"].append(change)

    def subscribe_store_path(self, page_id: str, prefix: str) -> dict[str, Any]:
        """Subscribe a page to a prefix of its user's store, returning its row.

        The first subscription creates ``user_view`` — a collector on the
        owner user's store Bag filtered on that prefix; the next ones widen it
        through ``subscribe_path``. Raises ``KeyError`` if the page is unknown.
        """
        page = self.page_items.get(page_id)
        if page is None:
            raise KeyError(f"subscribe_store_path: unknown page {page_id!r}")
        page["store_subscriptions"].add(prefix)
        view = page["user_view"]
        if view is None:
            user_store = self.user_items.get(self.page_user(page_id))["store"]
            page["user_view"] = self.new_collector(user_store, paths={prefix})
        else:
            view.subscribe_path(prefix)
        return page

    def change_connection_user(
        self, connection_id: str, user: str, **fields: Any
    ) -> dict[str, Any]:
        """The core re-labelling, then every view of the moved pages follows the new owner.

        A view still on another store than the new owner's is detached, rebuilt
        on the new owner's Bag with the same ``store_subscriptions`` prefixes
        and re-deposited with what it held — re-attached, never drained: a
        change captured before the login is still pending after it. A view
        already on that store (the guest-transfer path conserves the Bag) is
        left alone.
        """
        connection = super().change_connection_user(connection_id, user, **fields)
        user_store = self.user_items.get(user)["store"]
        for page_id in connection["pages"]:
            page = self.page_items.get(page_id)
            view = page["user_view"]
            if view is None or view.bag is user_store:
                continue
            view.detach()
            fresh = self.new_collector(user_store, paths=set(page["store_subscriptions"]))
            for change in view.changes:
                fresh.append(change)
            page["user_view"] = fresh
        return connection
