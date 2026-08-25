"""The bench's engine factory: the shipped one, with the register recorder installed.

The shipped bridge forks its workers out of a template process that builds the
``GnrWsgiSite`` once for the whole group. That moves the register recorder's
install point: ``GnrWsgiSite.__init__`` forces ``site.register`` into existence
(``gnrwsgisite.py:510``), so the client already exists in the TEMPLATE, before
any worker constructor runs. An assignment made in the worker would patch a name
the site has already read — the same reason ``serve_legacy.py`` installs before
``gnrserveprod.main()`` rather than from a gunicorn hook.

So the assignment moves here, into the one place that builds the site, and every
forked worker inherits the recording client with the site it is given. The HTTP
recorder does NOT move: ``wsgi_app`` is assigned by the worker constructor, in
the child, and ``recording_worker.py`` keeps wrapping it there.

**The template writes nothing, and that is a hard requirement.** It is the
process every worker is forked from, and on the sqlite the bridge runs
(3.51.0, pyenv python 3.12.9) a forked child dies of SIGSEGV as soon as its
parent has opened a connection of its own — no exception to catch, no line
written, and INTERMITTENTLY, two runs in three. Not WAL, not the same file, and
closing before the fork does not help: what poisons the child is the library
having been initialised at all (measured 2026-08-25; the legacy venv's sqlite
3.50.4 does the same thing cleanly, which is why the gunicorn stack is
unaffected). Confirmed on the bench: the first fork recipe let the template
write, and its worker was killed after never presenting itself.

So the client the template installs is handed a :class:`TemplateArchive`, which
swallows its lines, and each forked worker replaces it with the run's own
archive as its first act (``recording_worker.py``). What is dropped is the
handful of register calls the SITE makes while it is being built — four on the
bridge, two on legacy, already different because the two stacks build the site
in different processes. They carry no ``exchange_id``, so no comparison ever
read them. Declared here, because a silent drop is a divergence nobody can see
afterwards.

Nothing here is a bench-only variant of the protocol: the class the pool builds
in the template is the shipped ``GenropySiteEngineFactory``, subclassed, so the
bench bridge forks exactly as the shipped bridge does.
"""

import functools

from genropy_asgi.spa.site_engine_factory import GenropySiteEngineFactory
from gnr.web import gnrwsgisite

from register_recorder_mixin import RecordingRegisterClient


class TemplateArchive:
    """The archive of a process that must not touch sqlite: it swallows its lines.

    Not a null object for convenience: it is what keeps the template able to
    fork workers that can write. It answers the one call a recorder makes on an
    archive, and nothing else.
    """

    def append_record(self, kind, record):  # wf:phase-4:new
        """Drop the line. The template has no run to write into."""


class RecordingSiteEngineFactory(GenropySiteEngineFactory):
    """The shipped factory, building a site whose register client records."""

    def build_site(self):  # wf:phase-4:new
        """Install the recording client, then build the site as the shipped one does.

        Before, never after: the constructor reads ``site.register`` itself, so
        an assignment made afterwards patches a name that has already been read.
        The archive is bound here through a ``partial`` — the site builds its
        client as ``SiteRegisterClient(site)``, with no room for a second
        argument — exactly as ``serve_legacy.py`` binds the run's archive.
        """
        gnrwsgisite.SiteRegisterClient = functools.partial(RecordingRegisterClient,
                                                           archive=TemplateArchive())
        return super().build_site()
