"""Start the bridge with both bench recorders installed, recording into a freshly
minted run archive. The counterpart of ``serve_legacy.py``, on the other stack.

What it owns is the RUN, not the install: the recorders are installed by the
bench recipe naming the recording worker (``bridge_recipe.py``), which is what
"the install rides the recipe" means. This launcher exists because building a
recipe must stay free of consequences — a recipe that minted an archive could
not be built by the drift check — and because one place has to read the
declared conditions and publish the archive before the first worker is spawned.

Three things happen here, in order, and all three must precede the spawn:

- ``benchmarks/compare`` goes on the import path, in this process and in the
  environment the workers inherit: the recipe names the recording worker as a
  plain module, and the worker process resolves that name for itself;
- the archive is minted with the conditions of this run and its path published
  in ``GNR_BENCH_RUN`` — the environment is the only channel that reaches a
  worker the pool starts fresh, since no object crosses a spawn;
- the CLI is handed the command line, with the bench recipe appended to it.

The conditions are read where each one is true and never assumed: the site from
the path the CLI resolves, the database from the instance's own
``instanceconfig.xml``, the versions from the installed distributions, and the
commits from the working trees behind them. On this side genropy, genro-asgi
and genropy-asgi are all installed EDITABLE, so a version string records when
the package was installed while the commit records the code that actually ran —
the legacy stack runs a frozen copy of the same genropy tree, and only the
commits make that visible.

Run, from the repository root:

  temp/../ python benchmarks/compare/serve_bridge.py test_invoice_pg \
      -p 8098 --nodebug

Every argument is the ``gnrasgiserve`` command line: this launcher adds only
``--config``, and refuses to override one the caller named. The archive lands
in ``GNR_BENCH_ARCHIVE_DIR``, or in ``~/genro_bench/runs/`` when that is unset —
the same place the legacy runs go, so the two references sit side by side.
"""

import importlib
import importlib.metadata
import os
import platform
import subprocess
import sys
from datetime import datetime

from genropy_asgi.spa.cli import cmd_serve, resolve_instance_path
from gnr.app.gnrdeploy import PathResolver
from gnr.core.gnrbag import Bag

from bridge_recipe import RECORDING_WORKER
from run_archive import ARCHIVE_DIR_ENV, DEFAULT_ARCHIVE_DIR, RUN_ENV, RunArchive

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
BENCH_ROOT = os.path.dirname(os.path.dirname(BENCH_DIR))
RECIPE = os.path.join(BENCH_DIR, "bridge_recipe.py")

# The pool's ceiling, which the recipe leaves to the core: read from the core so
# the run row says what actually governed instead of what we remember.
WORKER_MAX_NUMBER_SOURCE = "genro_asgi.spa.orchestration.group_handler"


class RunConditions:  # wf:phase-5:new
    """The declared conditions of a bridge run, read from where each one is true."""

    def __init__(self, argv, path):  # wf:phase-5:new
        self.argv = argv
        self.path = path
        self.run_id = f"bridge-{datetime.now().strftime('%Y%m%dT%H%M%S')}"

    @property
    def sitename(self):
        """The name the worker resolves the site by — derived as the worker derives it."""
        name = os.path.basename(os.path.abspath(self.path))
        if name == "site":
            return os.path.basename(os.path.dirname(os.path.abspath(self.path)))
        return name

    def get_option(self, *names):
        for name in names:
            if name in self.argv:
                return self.argv[self.argv.index(name) + 1]
        return None

    @property
    def database(self):
        """The db the instance actually points at, not the one we remember."""
        instance = PathResolver().instance_name_to_path(self.sitename)
        return dict(Bag(os.path.join(instance, "instanceconfig.xml")).getAttr("db"))

    @property
    def worker_max_number(self):
        """The pool's ceiling: the core's own default, since the recipe declares none."""
        return importlib.import_module(WORKER_MAX_NUMBER_SOURCE).WORKER_MAX_NUMBER

    @property
    def declared(self):
        """The run row: the keys the legacy run declares, plus the ones only this stack has."""
        host = self.get_option("-H", "--host") or "127.0.0.1"
        port = self.get_option("-p", "--port") or "8000"
        return {"stack": "bridge",
                "sitename": self.sitename,
                "bind": f"{host}:{port}",
                "workers": self.worker_max_number,
                "threads": None,
                "worker_class": RECORDING_WORKER,
                "debug": "--nodebug" not in self.argv,
                "recorders": ["http", "register"],
                "database": self.database,
                "genropy": importlib.metadata.version("genropy"),
                "genropy_commit": self.get_commit("gnr"),
                "genro_asgi": importlib.metadata.version("genro-asgi"),
                "genro_asgi_commit": self.get_commit("genro_asgi"),
                "genropy_asgi": importlib.metadata.version("genropy-asgi"),
                "python": platform.python_version(),
                "bench_commit": self.get_commit()}

    def get_commit(self, package=None):
        """The short commit of the working tree behind ``package``, or of this bench.

        Every package on this side is installed editable, so the distribution
        version records the moment of installation and the commit records the
        code. An installation that is not a working tree answers with the empty
        string, which is the honest answer and not an error.
        """
        if package is None:
            path = BENCH_ROOT
        else:
            path = os.path.dirname(importlib.import_module(package).__file__)
        return subprocess.run(["git", "-C", path, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()


class BridgeLauncher:  # wf:phase-5:new
    """Puts the bench on the import path, mints the run, then hands over to the CLI."""

    def __init__(self, argv):  # wf:phase-5:new
        self.argv = list(argv)

    @property
    def command_line(self):
        """The CLI's own arguments, with the bench recipe appended when free."""
        if "--config" in self.argv:
            return self.argv
        return self.argv + ["--config", RECIPE]

    def publish_import_path(self):  # wf:phase-5:new
        """The bench directory, importable here and in every worker the pool spawns."""
        entries = [BENCH_DIR] + [p for p in (os.environ.get("PYTHONPATH") or "").split(os.pathsep) if p]
        os.environ["PYTHONPATH"] = os.pathsep.join(entries)

    def start_run(self, path):  # wf:phase-5:new
        """Mint the archive of this run and publish it to the workers to come."""
        conditions = RunConditions(self.argv, path)
        archive_dir = os.environ.get(ARCHIVE_DIR_ENV) or DEFAULT_ARCHIVE_DIR
        archive = RunArchive(os.path.join(archive_dir, f"{conditions.run_id}.sqlite"),
                             run_id=conditions.run_id, conditions=conditions.declared)
        os.environ[RUN_ENV] = archive.path
        print(f"recording run {conditions.run_id} into {archive.path}")

    def serve(self):  # wf:phase-5:new
        """The whole launch: import path, run, then the CLI's own command."""
        self.publish_import_path()
        self.start_run(resolve_instance_path(self.argv[0]))
        return cmd_serve(self.command_line)


if __name__ == "__main__":
    sys.exit(BridgeLauncher(sys.argv[1:]).serve())
