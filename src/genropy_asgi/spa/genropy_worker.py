# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyWorker: the core ``SpaWorker`` hosting a ``GnrWsgiSite``.

The execution unit of the GenroPy bridge. The core worker owns the site
verbs, the registers and the lane to its commander; this subclass adds
exactly the legacy site:

- the constructor takes ``source`` (a site name or a site directory path)
  and ``debug``, builds the ``GnrWsgiSite`` (the Werkzeug debugger wrapper
  when ``debugger``, ``site._local_mode = True``, atexit ``on_site_stop``) and
  assigns the possibly-wrapped site to ``self.wsgi_app`` — the core's
  consumer seam for the http CALL form;
- the site's lazy per-process state is settled right after creation,
  single-threaded (``site.resources_dirs``; ``site.storage("gnr")`` —
  genropy#984): the first concurrent request must not race the resource
  scan;
- the idle valve follows the site: unless the caller named
  ``user_idle_freeze_minutes``, it is read from the site's ``<cleanup>``
  section as ``connection_max_age`` seconds (the one legacy age with an
  equivalent — the silence past which the legacy register dropped a logged
  connection, and this worker parks the user in the freezer instead);
  ``page_max_age`` and ``guest_max_age`` have no equivalent on this base: a
  silent tab's row lives until the site drops it or its user freezes, and a
  guest is distinguished only at the commander's frozen expiry;
- ``site.spa_worker = self`` (the ratified name) is how the in-process
  register client reaches the worker's site verbs, and the client itself is
  captured at construction (the site's lazy ``register`` property does
  db-touching work that must not run on the event loop);
- the client reads ``user_items``/``connection_items``/``page_items``:
  translating properties over the core's ``*_register`` names (decision
  §7a, 2026-08-20 — the core keeps its names, the bridge translates);
- ``drop_page`` keeps the legacy ``cascade`` flag and absorbs it (decision
  D7, 2026-08-20): the core demolition — drop the page, and the connection
  and user its departure empties — is the sanctioned semantics;
- the three drop verbs remove the connection folders under
  ``data/_connections`` together with the rows. Freeze and transfer use the
  registry's internal removers and are deliberately not hooked: a frozen or
  moved user's folders must survive for the wake. A frozen user the
  commander expires leaves folders behind — the declared debt that replaces
  the retired orphan sweep;
- ``build_registry()`` returns :class:`GenropyRegistry`, whose stores are
  legacy Bags under :class:`~genropy_asgi.spa.legacy_bag.LegacyBagCollector`
  capture;
- ``exit_process()`` stops the site (``on_site_stop``) before the core
  teardown.

:class:`GenropyRegistry` overrides the two core seams and nothing else:
``new_store()`` returns a legacy ``gnr.core.gnrbag.Bag`` and
``new_collector(store, paths)`` returns a ``LegacyBagCollector`` — every
row of the chain (user stores, page stores, the views) is a legacy Bag, so
legacy values ride the registers untranslated (cemented rule B1). A legacy
store travels a move pickled whole: the legacy Bag drops its subscribers at
``__getstate__``, and the registry re-attaches collectors on arrival.

This module imports ``gnr.*`` at the top BY DESIGN (via ``legacy_bag``):
it is loaded through the ``worker_class`` dotted path only where GenroPy
is installed.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any

from genro_asgi.spa import RegisterRegistry
from genro_asgi.spa.orchestration import SpaWorker
from gnr.core.gnrbag import Bag

from .legacy_bag import LegacyBagCollector
from .site_engine_factory import GenropySiteEngineFactory

log = logging.getLogger("genropy_asgi.spa")

__all__ = ["GenropyRegistry", "GenropyWorker"]

# The one legacy <cleanup> age with an equivalent on this base: the seconds of
# silence past which the legacy register dropped a logged connection become
# the minutes past which the core's valve parks the user in the freezer.
# 7200s (the daemon-parity connection_max_age) -> 120 minutes.
IDLE_FREEZE_LEGACY_KEY = "connection_max_age"
IDLE_FREEZE_DEFAULT_SECONDS = 7200


class GenropyRegistry(RegisterRegistry):
    """The core registry with legacy stores: every row's Bag is a gnr Bag."""

    def new_store(self) -> Any:
        """The store factory: a legacy ``gnr.core.gnrbag.Bag``."""
        return Bag()

    def new_collector(self, store: Any, paths: set[str] | None = None) -> Any:
        """The capture factory: a ``LegacyBagCollector`` on a legacy store."""
        return LegacyBagCollector(store, paths=paths)


class GenropyWorker(SpaWorker):
    """A ``SpaWorker`` whose hosted WSGI app is a ``GnrWsgiSite``."""

    def __init__(
        self,
        name: str,
        *,
        source: str,
        debug: bool = False,
        debugger: bool = False,
        group_engine: Any = None,
        **kwargs: Any,
    ) -> None:
        """Args:
        name: the worker's name (the one its handler minted).
        source: the GenroPy site — a site name or a site directory path.
            Ignored when ``group_engine`` arrives: the site is already built.
        debug: True builds the site in debug mode — the SQL time counters,
            ``pageModule`` in the page's own bootstrap, the developer's extras.
        debugger: True wraps the site in the Werkzeug debugger middleware, whose
            error page carries a traceback AND a console that evaluates Python
            inside the process. Its own switch since 2026-08-26, and off by
            default: it must never come on as a side effect of a flag somebody
            set to get the SQL counters. GenroPy keeps the two separate too —
            ``site.debug`` comes from the configuration, the werkzeug wrapper
            only from ``serveprod --debug``.
        group_engine: the ``GnrWsgiSite`` the group's template built, handed
            to a worker born by fork (fork contract §8, 2026-08-24). When it
            is None the worker was spawned and builds its own site.
        kwargs: forwarded to ``SpaWorker`` — the spawn grammar
            (``freeze_handler``, ``group``, the pools) plus the policies.
            ``user_idle_freeze_minutes`` not named here is read from the
            site's ``<cleanup>`` section (``connection_max_age`` seconds,
            7200 where the site is silent): a caller's value always wins
            over the site's.
        """
        idle_from_site = "user_idle_freeze_minutes" not in kwargs
        super().__init__(name, **kwargs)
        # The forked worker receives the site its template built; the spawned
        # one builds its own. Nothing below this line differs between them.
        self._gnr_site = group_engine
        if self._gnr_site is None:
            self._gnr_site = self._create_site(source, debug)
        if idle_from_site:
            cleanup = self._gnr_site.custom_config.getAttr("cleanup") or {}
            seconds = int(cleanup.get(IDLE_FREEZE_LEGACY_KEY) or IDLE_FREEZE_DEFAULT_SECONDS)
            self.user_idle_freeze_minutes = seconds / 60.0
        # The register client, materialized HERE on the init thread: the legacy
        # ``site.register`` property builds it lazily, unlocked, down to db work
        # (``checkPendingConnection``), which must not happen on the event loop
        # while the first request is already running on a pool thread.
        self._register_client = self._gnr_site.register
        self.wsgi_app = self._gnr_site
        if debugger:
            # Deferred import, transcribed from the pre-rebase host: importing
            # gnr.web at module top would drag the register client (and with it
            # the whole site machinery) into every import of this module.
            from gnr.web.serverwsgi import GnrDebuggedApplication

            self.wsgi_app = GnrDebuggedApplication(self._gnr_site, evalex=True, pin_security=False)
        # Settle the site's lazy per-process state HERE, single-threaded:
        # ``resources_dirs`` is published and only then reversed in place, and
        # the uncached service scan it drives would let a first concurrent
        # request iterate a torn list (genropy#984). ``storage('gnr')`` and
        # ``storage('dojo')`` are exactly what the first request resolves in
        # ``build_arg_dict``.
        #
        # `dojo` joined the list on 2026-08-26, measured by the twin proxy: a
        # worker born for one user was still instantiating that service inside
        # its first page — six register calls the legacy did not make there, and
        # 291 ms against 100. The legacy makes them too, at the startup of its
        # one long-lived process. Settling them here puts them where the legacy
        # has them: outside the request, in the birth.
        self._gnr_site.resources_dirs
        self._gnr_site.storage("gnr")
        self._gnr_site.storage("dojo")
        self._gnr_site.spa_worker = self

    def build_registry(self) -> RegisterRegistry:
        """The registry factory: legacy stores under legacy capture."""
        return GenropyRegistry()

    @property
    def gnr_site(self) -> Any:
        """The hosted ``GnrWsgiSite`` instance (unwrapped)."""
        return self._gnr_site

    @property
    def user_items(self) -> Any:
        """The user register under the name the legacy client reads (§7a)."""
        return self.user_register

    @property
    def connection_items(self) -> Any:
        """The connection register under the name the legacy client reads (§7a)."""
        return self.connection_register

    @property
    def page_items(self) -> Any:
        """The page register under the name the legacy client reads (§7a)."""
        return self.page_register

    def apply_forwarded(self, bag: Any, change: dict[str, Any]) -> None:
        """Apply a change born elsewhere to a LEGACY Bag (STATE delivery).

        The core writes with the new Bag's API (``set_item``/``_fired``); the
        bridge stores are legacy Bags, so the write is translated: ``setItem``
        with the attributes and the reason, ``pop(path, _reason=...)`` for a
        delete (the legacy signature carries it). A fired change rides the
        transient ``_fired`` attribute for the capture (the legacy ``setItem``
        has no ``_fired`` parameter — the collectors pop it from their local
        copy, see ``legacy_bag``), then the node is cleaned: the attribute
        removed and the static value reset — the same one-shot semantics
        ``set_item(_fired=True)`` has on the core Bag.
        """
        path = change["key"]["path"]
        reason = change["key"]["reason"]
        if change["delete"]:
            bag.pop(path, _reason=reason)
            return
        attributes = dict(change["attributes"] or {})
        attributes["_original_ts"] = change["change_ts"]
        fired = change["key"]["fired"]
        if fired:
            attributes["_fired"] = True
        bag.setItem(path, change["value"], _attributes=attributes, _reason=reason)
        if fired:
            node = bag.getNode(path)
            node.attr.pop("_fired", None)
            node.staticvalue = None

    @property
    def connections_folder(self) -> str:
        """The site's per-connection disk root (``data/_connections``)."""
        return self._gnr_site.allConnectionsFolder

    @property
    def pool_member(self) -> bool:
        """Whether this worker serves in a pool — a spawned child on a real wire.

        A worker with no wire attached is a bare test construction: it holds
        every row it ever made, so the per-table commit gate may answer from
        its own subscription cache. A child on the wire holds only its share.
        """
        return self.stream is not None

    def drop_page(self, identity: str, page_id: str, cascade: bool = True) -> None:
        """Drop a page with the legacy ``cascade`` flag, then its disk with the row.

        The flag stays on the bridge and is absorbed here (decision D7,
        2026-08-20). The DEFAULT the site's own close paths pass is the legacy
        page semantics: ``cascade=False`` drops the page ALONE, leaving an
        emptied connection row alive — a closed tab must not take its
        browser's connection with it, its cookie still routes (Must not
        break: site-facing semantics). The core drop has no cascade-less
        form — its climb is unconditional — so this branch composes it from
        the same pieces: the registry drop and the announcement.
        ``cascade=True`` is the core drop itself: the page, and the
        connection and user its departure empties. Either way the disk
        follows the rows: the whole connection folder when the connection
        fell, the page's subfolder otherwise.
        """
        with self.dispatch_lock:
            page = self.page_register.get(page_id)
            if page is None:
                return
            connection_id = page["connection_id"]
            if cascade:
                super().drop_page(identity, page_id)
            else:
                user = self.registry.page_user(page_id)
                self.registry.drop_page(page_id, cascade=False)
                self.add_worker_event("drop_page", user=user, page_id=page_id)
            if self.connection_register.get(connection_id) is None:
                shutil.rmtree(
                    os.path.join(self.connections_folder, connection_id), ignore_errors=True
                )
            else:
                shutil.rmtree(
                    os.path.join(self.connections_folder, connection_id, page_id),
                    ignore_errors=True,
                )

    def drop_connection(self, identity: str, connection_id: str) -> None:
        """The core drop, then the connection's disk folder goes with the row.

        The cascaded pages' subfolders live under the connection folder
        removed here, so nothing is left behind.
        """
        super().drop_connection(identity, connection_id)
        shutil.rmtree(os.path.join(self.connections_folder, connection_id), ignore_errors=True)

    def drop_user(self, user: str) -> None:
        """The core drop, then every connection folder of the user goes too."""
        with self.dispatch_lock:
            connection_ids = [
                connection_id
                for connection_id in self.connection_register.keys()
                if (item := self.connection_register.get(connection_id))
                and item["user"] == user
            ]
            super().drop_user(user)
        for connection_id in connection_ids:
            shutil.rmtree(os.path.join(self.connections_folder, connection_id), ignore_errors=True)

    def exit_process(self) -> None:
        """Stop the site first, then the core teardown (wire, pools)."""
        self._gnr_site.on_site_stop()
        super().exit_process()

    def _create_site(self, source: str, debug: bool) -> Any:
        """The spawned worker's site, built through the group's own factory.

        One construction for both births (fork contract §8, 2026-08-24): the
        forked worker receives a site the template built with this very
        factory. ``build_site`` and not ``build_group_engine`` — closing the
        db connection is what a template owes its children, and a worker
        building for itself has none to close.
        """
        return GenropySiteEngineFactory(source=source, debug=debug).build_site()
