# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropyWorker: the core ``UserStickyWorker`` hosting a ``GnrWsgiSite``.

The execution unit of the GenroPy bridge. The core worker owns the op
vocabulary, the registers, the pools and the channel; this subclass adds
exactly the legacy site:

- the constructor takes ``source`` (a site name or path) and ``debug``,
  builds the ``GnrWsgiSite`` (PathResolver, ``root.py`` fallback in the
  parent directory, the Werkzeug debugger wrapper when ``debug``,
  ``site._local_mode = True``, atexit ``on_site_stop``) and assigns the
  possibly-wrapped site to ``self.wsgi_app`` — the core's consumer seam for
  the http CALL form;
- the site's lazy per-process state is settled right after creation,
  single-threaded (``site.resources_dirs``; ``site.storage("gnr")`` —
  genropy#984): the first concurrent request must not race the resource
  scan;
- ``site.spa_worker = self`` (the ratified name) is how the in-process
  register client reaches the worker's op methods;
- ``build_registry()`` returns :class:`GenropyRegistry`, whose stores are
  legacy Bags under :class:`~genropy_asgi.spa.legacy_bag.LegacyBagCollector`
  capture;
- ``shutdown()`` stops the site (``on_site_stop``) before the core teardown.

:class:`GenropyRegistry` overrides the two core seams and nothing else:
``new_store()`` returns a legacy ``gnr.core.gnrbag.Bag`` and
``new_collector(store, paths)`` returns a ``LegacyBagCollector`` — every
row of the chain (user stores, page stores, the views) is a legacy Bag, so
legacy values ride the registers untranslated (cemented rule B1). A legacy
store travels a move pickled whole: the legacy Bag drops its subscribers at
``__getstate__``, and the registry re-attaches collectors on arrival.

This module imports ``gnr.*`` at the top BY DESIGN (via ``legacy_bag``
and the site factory): it is loaded through the ``worker_class`` dotted
path only where GenroPy is installed.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import time
from types import SimpleNamespace
from typing import Any, Callable

from genro_bag import Bag as CoreBag
from genro_tytx import from_tytx

from genro_asgi.channel.hub import EVENT_METHOD
from genro_asgi.spa import RegisterRegistry, UserStickyWorker
from genro_asgi.spa.global_store import GLOBAL_CHANGES_PATH, GLOBAL_SNAPSHOT_PATH
from gnr.core.gnrbag import Bag

from .legacy_bag import LegacyBagCollector

log = logging.getLogger("genropy_asgi.spa")

__all__ = ["GenropyRegistry", "GenropyWorker"]

# The expiry knobs of the bridge (ratified names and values, seconds): a page
# idle past PAGE_MAX_AGE is swept, an anonymous page or connection past
# GUEST_MAX_AGE, a logged connection past CONNECTION_MAX_AGE. The ping stamps
# ``refresh_chain``, so an idle-but-alive page is refreshed by its own polling.
PAGE_MAX_AGE = 600
GUEST_MAX_AGE = 1800
CONNECTION_MAX_AGE = 86400

# The sweep cadence: ARMED by default on the bridge (the browser rail is the
# legacy polling, which already stamps the chain).
SWEEP_INTERVAL = 60


class GenropyRegistry(RegisterRegistry):
    """The core registry with legacy stores: every row's Bag is a gnr Bag."""

    def new_store(self) -> Any:
        """The store factory: a legacy ``gnr.core.gnrbag.Bag``."""
        return Bag()

    def new_collector(self, store: Any, paths: set[str] | None = None) -> Any:
        """The capture factory: a ``LegacyBagCollector`` on a legacy store."""
        return LegacyBagCollector(store, paths=paths)


class GenropyWorker(UserStickyWorker):
    """A ``UserStickyWorker`` whose hosted WSGI app is a ``GnrWsgiSite``."""

    def __init__(
        self,
        name: str,
        *,
        source: str,
        debug: bool = False,
        **kwargs: Any,
    ) -> None:
        """Args:
        name: the worker's channel name (already typed, e.g. ``W:w1``).
        source: the GenroPy site — a site name or a path to it.
        debug: True wraps the site in the Werkzeug debugger middleware.
        kwargs: forwarded to ``UserStickyWorker``; the expiry knobs and the
            sweep cadence default to the bridge's ratified values (the sweep
            is ARMED here — the legacy polling stamps the chain, so an
            idle-but-alive page refreshes itself).
        """
        kwargs.setdefault("sweep_interval", SWEEP_INTERVAL)
        kwargs.setdefault("page_max_age", PAGE_MAX_AGE)
        kwargs.setdefault("guest_max_age", GUEST_MAX_AGE)
        kwargs.setdefault("connection_max_age", CONNECTION_MAX_AGE)
        super().__init__(name, **kwargs)
        self._gnr_site = self._create_site(source, debug)
        self.wsgi_app = self._gnr_site
        if debug:
            # Deferred import, transcribed from the pre-rebase host: importing
            # gnr.web at module top would drag the register client (and with it
            # the whole site machinery) into every import of this module.
            from gnr.web.serverwsgi import GnrDebuggedApplication

            self.wsgi_app = GnrDebuggedApplication(self._gnr_site, evalex=True, pin_security=False)
        # Settle the site's lazy per-process state HERE, single-threaded:
        # ``resources_dirs`` is published and only then reversed in place, and
        # the uncached service scan it drives would let a first concurrent
        # request iterate a torn list (genropy#984). ``storage('gnr')`` is
        # exactly what the first request resolves in ``build_arg_dict``.
        self._gnr_site.resources_dirs
        self._gnr_site.storage("gnr")
        self._gnr_site.spa_worker = self

    def build_registry(self) -> RegisterRegistry:
        """The registry factory: legacy stores under legacy capture."""
        return GenropyRegistry()

    @property
    def gnr_site(self) -> Any:
        """The hosted ``GnrWsgiSite`` instance (unwrapped)."""
        return self._gnr_site

    def apply_forwarded(self, bag: Any, change: dict[str, Any]) -> None:
        """Apply a change born elsewhere to a LEGACY Bag (STATE delivery).

        The core writes with the new Bag's API (``set_item``/``_fired``); the
        bridge stores are legacy Bags, so the write is translated: ``setItem``
        with the attributes and the reason, ``pop(path, _reason=...)`` for a
        delete (the legacy signature carries it). A fired change resets the
        node's static value silently after the write — the same one-shot
        semantics ``set_item(_fired=True)`` has on the core Bag.
        """
        path = change["key"]["path"]
        reason = change["key"]["reason"]
        if change["delete"]:
            bag.pop(path, _reason=reason)
            return
        attributes = dict(change["attributes"] or {})
        attributes["_original_ts"] = change["change_ts"]
        bag.setItem(path, change["value"], _attributes=attributes, _reason=reason)
        if change["key"]["fired"]:
            bag.getNode(path).staticvalue = None

    async def handle_frame(self, frame: Any) -> None:
        """The core wire handling, then the legacy materialization of the global rail.

        The core applies the descending global pushes INLINE on its replica
        (snapshot before the changes that follow it — the receive loop is the
        only place that order still exists); the same content is then poured
        into the legacy ``global_bag`` through the register, so the legacy
        reads stay local and coherent with the commander's master. The snapshot
        is read back from the replica (already decoded once); the changes are
        the master collector's own dicts, translated to the write/delete pair
        the register materializes (``applying`` flag, aware->naive decode —
        all of it the register's, unchanged).
        """
        await super().handle_frame(frame)
        if frame.method != EVENT_METHOD:
            return
        if frame.path == GLOBAL_SNAPSHOT_PATH:
            self._gnr_site.register._materialize_global_snapshot(self._replica_global_leaves())
        elif frame.path == GLOBAL_CHANGES_PATH:
            register = self._gnr_site.register
            for change in from_tytx(frame.data, "json"):
                if not change["delete"] and isinstance(change["value"], CoreBag):
                    # an autocreated parent on the master: structure, not a
                    # leaf — the legacy Bag autocreates its own parents and
                    # the leaves travel as changes of their own
                    continue
                op = "store_del" if change["delete"] else "store_set"
                register._materialize_global(op, change["key"]["path"], change["value"])

    def _replica_global_leaves(self) -> dict[str, Any]:
        """The replica's ``{full_path: value}`` leaves, already DECODED.

        The legacy ascent ships TYTX-suffixed text, but every descending hop
        crosses ``to_tytx``/``from_tytx`` — and the suffix grammar being the
        shared historical one, the hop decodes the text back to the original
        value. A core-Bag node is structure, never a leaf.
        """
        return {
            path: node.value
            for path, node in self.global_store.walk()
            if not isinstance(node.value, CoreBag)
        }

    @property
    def connections_folder(self) -> str:
        """The site's per-connection disk root (``data/_connections``)."""
        return self._gnr_site.allConnectionsFolder

    def demolish_page(self, page_id: str, announce: Callable[..., Any]) -> dict[str, Any]:
        """The core demolition, then the page's disk folder goes with the row.

        Expiry, logout and cascades all pass here, so they all clean the same
        way. When the drop took the connection with it (the last page of it),
        the whole connection folder goes too.
        """
        entry = super().demolish_page(page_id, announce)
        connection_id = entry["connection_id"]
        if connection_id in self.connection_items:
            shutil.rmtree(
                os.path.join(self.connections_folder, connection_id, page_id),
                ignore_errors=True,
            )
        else:
            shutil.rmtree(
                os.path.join(self.connections_folder, connection_id), ignore_errors=True
            )
        return entry

    def demolish_connection(self, connection_id: str, announce: Callable[..., Any]) -> dict[str, Any]:
        """The core demolition, then the connection's disk folder goes with the row.

        The cascaded pages are dropped by the core registry internally (never
        through ``demolish_page``); their subfolders live under the connection
        folder removed here, so nothing is left behind.
        """
        entry = super().demolish_connection(connection_id, announce)
        shutil.rmtree(os.path.join(self.connections_folder, connection_id), ignore_errors=True)
        return entry

    def sweep_expired(self) -> dict[str, list[str]]:
        """The core sweep, then the ORPHAN disk pass.

        A folder under ``data/_connections`` whose connection this worker does
        not hold — a previous run's leftovers — and older than
        ``connection_max_age`` by mtime is removed. Sufficient in the single,
        where the worker sees every connection.
        """
        dropped = super().sweep_expired()
        self.sweep_orphan_folders()
        return dropped

    def sweep_orphan_folders(self) -> None:
        """Remove the stale per-connection folders nobody's row explains."""
        folder = self.connections_folder
        if not os.path.isdir(folder):
            return
        now = time.time()
        for entry in os.listdir(folder):
            path = os.path.join(folder, entry)
            if not os.path.isdir(path) or entry in self.connection_items:
                continue
            try:
                age = now - os.stat(path).st_mtime
            except OSError:
                continue
            if age > self.connection_max_age:
                shutil.rmtree(path, ignore_errors=True)

    async def shutdown(self) -> None:
        """Stop the site first, then the core teardown (sender, CALLs, channel)."""
        self._gnr_site.on_site_stop()
        await super().shutdown()

    def _create_site(self, source: str, debug: bool) -> Any:
        """Create the ``GnrWsgiSite`` from a site name or path.

        ``source`` may be a path: PathResolver returns it resolved, and the
        ``root.py`` lookup works from whatever directory it points to, with
        the parent-directory fallback of the legacy server.

        The ``gnr`` imports are deferred, transcribed from the pre-rebase
        host: this module must stay importable (for ``GenropyRegistry`` and
        the capture) even while the site machinery cannot load.
        """
        from gnr.app.pathresolver import PathResolver
        from gnr.core.gnrconfig import getGnrConfig
        from gnr.web.gnrwsgisite import GnrWsgiSite

        gnr_config = getGnrConfig(set_environment=True)
        site_path = PathResolver().site_name_to_path(source)
        script_path = os.path.join(site_path, "root.py")
        if not os.path.isfile(script_path):
            script_path = os.path.join(site_path, "..", "root.py")
            if not os.path.isfile(script_path):
                raise FileNotFoundError(
                    f"no root.py found for site {source!r} in {site_path} or its parent"
                )

        options = SimpleNamespace(
            debug=debug,
            noclean=False,
            reload=False,
            remote_edit=None,
            source_instance=None,
            restore=None,
        )

        log.info("Creating GnrWsgiSite for '%s' at %s", source, site_path)
        site = GnrWsgiSite(
            script_path,
            site_name=source,
            _gnrconfig=gnr_config,
            options=options,
        )
        site._local_mode = True
        atexit.register(site.on_site_stop)
        log.info("GnrWsgiSite '%s' ready", source)
        return site
