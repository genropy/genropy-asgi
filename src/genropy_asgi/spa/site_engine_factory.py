# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""GenropySiteEngineFactory: the one construction of the ``GnrWsgiSite``.

The bridge's side of the fork contract (core, 2026-08-24). The core lets a
group build ONE expensive object — the ``group_engine`` — in a template
process, and forks its workers from there; the engine the bridge hands over
is the ``GnrWsgiSite``. The core never opens it: it resolves this class from
the template payload's ``engine_factory``, instantiates it with the
payload's ``kwargs``, and calls ``build_group_engine()``.

The same construction serves both births: ``GenropyWorker`` builds its own
site through this factory when it is spawned, and receives an already-built
one when it is forked. One code path, so the two workers cannot diverge.

Two duties belong to this class and to no one else:

- **the db connection is closed before the fork** (``site.db.closeConnection()``,
  which closes the connections of the calling thread). A socket inherited by
  every child is a socket every child would speak on;
- **one thread must be alive when the engine is returned**. The template
  refuses to fork when ``threading.active_count() != 1``, so anything the
  site starts during construction — a package hook, a storage service —
  stops the whole group from growing. The count is not enforced here; it is
  verified by the template, and this is where a violation originates.

The ``gnr`` imports stay inside the method, transcribed from the worker this
code was moved out of: the module must stay importable where the site
machinery cannot load.
"""

from __future__ import annotations

import atexit
import logging
import os
from types import SimpleNamespace
from typing import Any

log = logging.getLogger("genropy_asgi.spa")

__all__ = ["GenropySiteEngineFactory"]


class GenropySiteEngineFactory:
    """Builds the ``GnrWsgiSite`` — the group engine of the fork contract."""

    def __init__(self, *, source: str, debug: bool = False) -> None:
        """Args:
        source: the GenroPy site — a site name or a site directory path.
        debug: True builds the site in debug mode. The Werkzeug debugger
            wrapper is NOT applied here: it wraps per worker, not per group.
        """
        self.source = source
        self.debug = debug

    def build_site(self) -> Any:
        """Create the ``GnrWsgiSite`` from a site name or a site directory path.

        ``GnrWsgiSite`` wants the site NAME — every path it needs it re-resolves
        from the name through genropy's own two routes (``*/sites/<name>/``,
        then ``*/instances/<name>/`` with the ``root.py`` marker) — so a path
        is turned into its name deliberately (genropy-asgi#4): the folder's
        basename, or the instance's name when the folder is the ``site/`` of
        the instances layout. Handing the path itself through would ride the
        resolver's join accident into ``get_instanceconfig``, which then reads
        ``instanceconfig.xml`` INSIDE the site folder and fails. No file is
        required in the site folder: the site is configuration, not code.
        """
        from gnr.core.gnrconfig import getGnrConfig
        from gnr.web.gnrwsgisite import GnrWsgiSite

        gnr_config = getGnrConfig(set_environment=True)
        site = self.source
        if os.path.isdir(self.source):
            path = os.path.abspath(self.source)
            site = os.path.basename(path)
            if site == "site":
                site = os.path.basename(os.path.dirname(path))
        options = SimpleNamespace(
            debug=self.debug,
            noclean=False,
            reload=False,
            remote_edit=None,
            source_instance=None,
            restore=None,
        )

        log.info("Creating GnrWsgiSite for %r", site)
        gnr_site = GnrWsgiSite(site, _gnrconfig=gnr_config, options=options)
        gnr_site._local_mode = True
        atexit.register(gnr_site.on_site_stop)
        log.info("GnrWsgiSite %r ready", gnr_site.site_name)
        return gnr_site

    def build_group_engine(self) -> Any:
        """The template's one call: the site, with no db connection open."""
        gnr_site = self.build_site()
        gnr_site.db.closeConnection()
        return gnr_site
