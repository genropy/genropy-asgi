"""The bench's worker: a ``GenropyWorker`` with both recorders installed.

The pool names its worker class as an import string and the worker process
resolves it itself when it is spawned, so a bench recipe naming THIS class
installs both recorders in every worker — no environment switch, no
``sitecustomize``, and no seam added to genro-asgi or genropy-asgi.

Two installs, two moments, both plain calls:

- the register recorder goes in FIRST, before ``super().__init__`` builds the
  site: ``GnrWsgiSite.__init__`` forces ``site.register`` into existence, so an
  assignment made afterwards would patch a name the site has already read;
- the HTTP recorder goes in LAST, around whatever ``wsgi_app`` ended up being —
  the site itself, or the Werkzeug debugger wrapping it when the worker runs
  with debug. Wrapping outermost is what puts the exchange header into the
  environ before anything else reads the request.

The site's own module is imported at the top of this file on purpose: the
worker's ``_create_site`` defers that import so ``genropy_worker`` stays
importable where the site machinery cannot load, but this module lives only in
the process that hosts a site, and the patch point has to exist before the
constructor runs.
"""

from genropy_asgi.spa.genropy_worker import GenropyWorker
from gnr.web import gnrwsgisite
from http_recorder import HttpRecorder
from register_recorder_mixin import RecordingRegisterClient


class RecordingGenropyWorker(GenropyWorker):  # wf:phase-5:new
    """A bridge worker whose site and register both write into the run archive."""

    def __init__(self, name, **kwargs):  # wf:phase-5:new
        gnrwsgisite.SiteRegisterClient = RecordingRegisterClient
        super().__init__(name, **kwargs)
        self.wsgi_app = HttpRecorder(self.wsgi_app)
