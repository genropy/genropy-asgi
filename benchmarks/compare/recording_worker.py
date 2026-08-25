"""The bench's worker: a ``GenropyWorker`` recording into the run archive.

The pool names its worker class as an import string and the worker process
resolves it itself, so a bench recipe naming THIS class arms both recorders in
every worker — no environment switch, no ``sitecustomize``, and no seam added to
genro-asgi or genropy-asgi.

Two plain calls, both AFTER ``super().__init__``, because both need what the
constructor produced — and both run in the forked CHILD, which is the process
that may open a sqlite connection:

- the register recorder, installed in the template by
  ``recording_engine_factory.py``, is given the run's archive in place of the
  ``TemplateArchive`` that swallowed the template's own lines. The recorder
  itself came with the site, inherited by the fork; only where it writes is
  decided here;
- the HTTP recorder goes around whatever ``wsgi_app`` ended up being — the site
  itself, or the Werkzeug debugger wrapping it when the worker runs with debug.
  Wrapping outermost is what puts the exchange header into the environ before
  anything else reads the request.
"""

import os

from genropy_asgi.spa.genropy_worker import GenropyWorker

from http_recorder import HttpRecorder
from run_archive import RUN_ENV, RunArchive


class RecordingGenropyWorker(GenropyWorker):
    """A bridge worker whose register and HTTP lines both reach the run archive."""

    def __init__(self, name, **kwargs):
        super().__init__(name, **kwargs)
        self.gnr_site.register.recording.archive = RunArchive(os.environ[RUN_ENV])
        self.wsgi_app = HttpRecorder(self.wsgi_app)
