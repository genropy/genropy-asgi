"""Start the legacy stack with both bench recorders installed, recording into a
freshly minted run archive.

The register recorder cannot be installed from a gunicorn hook: `main()` builds
`GnrWsgiSite` before it reads the `-c` configuration file, and the site's
`__init__` forces the register into existence, so the client already exists in
the master process before any hook runs and before the fork. This launcher is
the install point — the assignment below, then genropy's own entry point.

The HTTP recorder keeps its own install point, `post_worker_init` in
`gunicorn_recorders.conf.py`, which runs late on purpose: it needs the loaded
application to wrap. Two recorders, two install points, one command.

This launcher also owns the RUN: it mints the archive with its declared
conditions and publishes the path in `GNR_BENCH_RUN` before the fork, so the
HTTP recorder in the worker attaches to the same file. The register recorder is
handed the archive object directly, through a `partial` — genropy builds its
client as `SiteRegisterClient(site)`, with no room for a second argument.

The conditions are read where they are true and never assumed: the workers, the
threads, the bind and the debug flag from this very command line, the database
from the instance's own `instanceconfig.xml`, the versions from the installed
distributions, the bench commit from git. `GENRO_GNRFOLDER` points genropy at
the bench's own configuration folder, which names the pinned trees — without it
the run reads the developer's `~/.gnr` and is not comparable.

Run, from the repository root:

  GENRO_GNRFOLDER=$PWD/temp/gnr PGGSSENCMODE=disable temp/legacy_venv/bin/python \
      benchmarks/compare/serve_legacy.py test_invoice_pg_legacy \
      -b 127.0.0.1:8099 -w 1 -k gthread --threads 16 \
      -c benchmarks/compare/gunicorn_recorders.conf.py

Every argument after the script is genropy's own `serveprod` command line: this
launcher adds nothing to it and takes nothing away. The archive lands in
`GNR_BENCH_ARCHIVE_DIR`, or in `~/genro_bench/runs/` when that is unset.
"""

import functools
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime

import gunicorn
from gnr.app.gnrdeploy import PathResolver
from gnr.core.gnrbag import Bag
from gnr.web import gnrwsgisite
from gnr.web.cli.gnrserveprod import main

from register_recorder import RegisterRecorder
from run_archive import ARCHIVE_DIR_ENV, DEFAULT_ARCHIVE_DIR, RUN_ENV, RunArchive

BENCH_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class RunConditions:
    """The declared conditions of this run, read from where each one is true."""

    def __init__(self, argv):
        self.argv = argv
        self.run_id = f"legacy-{datetime.now().strftime('%Y%m%dT%H%M%S')}"

    @property
    def sitename(self):
        return self.argv[1]

    def get_option(self, *names):
        for name in names:
            if name in self.argv:
                return self.argv[self.argv.index(name) + 1]
        return None

    @property
    def database(self):
        """The db the instance actually points at, not the one we remember."""
        path = PathResolver().instance_name_to_path(self.sitename)
        return dict(Bag(os.path.join(path, "instanceconfig.xml")).getAttr("db"))

    @property
    def declared(self):
        return {"stack": "legacy",
                "sitename": self.sitename,
                "bind": self.get_option("-b", "--bind"),
                "workers": self.get_option("-w", "--workers"),
                "threads": self.get_option("--threads"),
                "worker_class": self.get_option("-k", "--worker-class"),
                "debug": "--debug" in self.argv,
                "recorders": ["http", "register"],
                "database": self.database,
                "genropy": importlib.metadata.version("genropy"),
                "genropy_source": self.genropy_source,
                "genropy_commit": self.genropy_commit,
                "gunicorn": gunicorn.__version__,
                "python": platform.python_version(),
                "bench_commit": self.bench_commit}

    @property
    def genropy_source(self):
        """The tree this frozen copy was built from — the bench's pinned worktree.

        The installed version string records the moment of installation, never
        the code, and a frozen copy is no working tree of its own. What can be
        asked is where it came from, which the installer writes down.
        """
        url = json.loads(importlib.metadata.distribution("genropy")
                         .read_text("direct_url.json"))["url"]
        return urllib.request.url2pathname(urllib.parse.urlparse(url).path)

    @property
    def genropy_commit(self):
        """The commit of that tree: what the bridge run declares for its own."""
        return subprocess.run(["git", "-C", self.genropy_source,
                               "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()

    @property
    def bench_commit(self):
        return subprocess.run(["git", "-C", BENCH_ROOT, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()


if __name__ == "__main__":
    conditions = RunConditions(sys.argv)
    archive_dir = os.environ.get(ARCHIVE_DIR_ENV) or DEFAULT_ARCHIVE_DIR
    archive = RunArchive(os.path.join(archive_dir, f"{conditions.run_id}.sqlite"),
                         run_id=conditions.run_id, conditions=conditions.declared)
    os.environ[RUN_ENV] = archive.path
    print(f"recording run {conditions.run_id} into {archive.path}")
    gnrwsgisite.SiteRegisterClient = functools.partial(RegisterRecorder,
                                                      archive=archive)
    main()
