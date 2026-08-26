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

  GENRO_GNRFOLDER=$PWD/temp/gnr \
      PYTHONPATH=$HOME/Sviluppo/Genropy/genropy/worktrees/bench-baseline/gnrpy \
      PGGSSENCMODE=disable python benchmarks/compare/serve_bridge.py test_invoice_pg \
      -p 8098 --nodebug

`PYTHONPATH` is what puts the bench's pinned genropy ahead of the editable one
in the developer's pyenv, and `GENRO_GNRFOLDER` points at the configuration that
names the pinned resources and packages. Both are read at import time, so they
belong on the command line and not in this launcher.

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
from genropy_asgi.spa.config import DEBUG_OFF_WORDS
from gnr.app.pathresolver import PathResolver

from bridge_recipe import RECORDING_WORKER
from run_archive import ARCHIVE_DIR_ENV, DEFAULT_ARCHIVE_DIR, RUN_ENV, RunArchive

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
BENCH_ROOT = os.path.dirname(os.path.dirname(BENCH_DIR))
RECIPE = os.path.join(BENCH_DIR, "bridge_recipe.py")

# The pool's ceiling, which the recipe leaves to the core: read from the core so
# the run row says what actually governed instead of what we remember.
WORKER_MAX_NUMBER_SOURCE = "genro_asgi.spa.orchestration.group_handler"


class RunConditions:
    """The declared conditions of a bridge run, read from where each one is true."""

    def __init__(self, argv, path):
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
        """The db the site will actually open, read as the site reads it.

        `get_instanceconfig` is genropy's own merge of the gnr-folder default,
        the templates and the instance's own file. Opening that last file alone
        would miss a `db` node declared in the default, which is where a
        deployment usually keeps the connection and its password.
        """
        return dict(PathResolver().get_instanceconfig(self.sitename).getAttr("db"))

    @property
    def worker_max_users(self):
        """The placement ceiling the recipe will read, or None when nobody set one.

        A run that cannot say how many users a worker was allowed to hold is not
        comparable to another: with one worker per user the site exercises paths
        that never run when everybody shares a process.
        """
        value = os.environ.get("GNR_ASGI_WORKER_MAX_USERS")
        return int(value) if value else None

    @property
    def worker_max_number(self):
        """The pool's ceiling: the core's own default, since the recipe declares none."""
        return importlib.import_module(WORKER_MAX_NUMBER_SOURCE).WORKER_MAX_NUMBER

    @property
    def debugger(self):
        """Is the werkzeug debugger wrapped around the site?

        Its own condition since 2026-08-26, and not a shade of debug: the error
        page it serves evaluates Python in the process, and a run that had it on
        answered errors differently from one that did not.
        """
        return bool(os.environ.get("GNR_ASGI_DEBUGGER")) or "--fulldebug" in self.argv

    @property
    def debug(self):
        """Debug as the RECIPE will read it, not as the command line looks.

        The two are not the same question. ``--nodebug`` makes the CLI write an
        empty ``GNR_ASGI_DEBUG``, but with the flag absent the recipe reads
        whatever the environment already holds — so a variable exported in the
        shell decides the run while the command line says nothing. Reading the
        flag alone would let the run row declare a debug the worker never had,
        and debug changes what the site measures. The rule is imported from the
        recipe's own module rather than restated, so the two cannot drift.
        """
        if "--fulldebug" in self.argv:
            return True
        if "--nodebug" in self.argv:
            return False
        value = os.environ.get("GNR_ASGI_DEBUG")
        return True if value is None else value.strip().lower() not in DEBUG_OFF_WORDS

    @property
    def declared(self):
        """The run row: the keys the legacy run declares, plus the ones only this stack has."""
        host = self.get_option("-H", "--host") or "127.0.0.1"
        port = self.get_option("-p", "--port") or "8000"
        return {"stack": "bridge",
                "sitename": self.sitename,
                "bind": f"{host}:{port}",
                "workers": self.worker_max_number,
                "worker_max_users": self.worker_max_users,
                "threads": None,
                "worker_class": RECORDING_WORKER,
                "debug": self.debug,
                "debugger": self.debugger,
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


class BridgeLauncher:
    """Puts the bench on the import path, mints the run, then hands over to the CLI."""

    def __init__(self, argv):
        self.argv = list(argv)

    @property
    def command_line(self):
        """The CLI's own arguments, with the bench recipe appended when free."""
        if "--config" in self.argv:
            return self.argv
        return self.argv + ["--config", RECIPE]

    def publish_import_path(self):
        """The bench directory, importable here and in every worker the pool spawns."""
        entries = [BENCH_DIR] + [p for p in (os.environ.get("PYTHONPATH") or "").split(os.pathsep) if p]
        os.environ["PYTHONPATH"] = os.pathsep.join(entries)

    def start_run(self, path):
        """Mint the archive of this run and publish it to the workers to come."""
        conditions = RunConditions(self.argv, path)
        archive_dir = os.environ.get(ARCHIVE_DIR_ENV) or DEFAULT_ARCHIVE_DIR
        archive = RunArchive(os.path.join(archive_dir, f"{conditions.run_id}.sqlite"),
                             run_id=conditions.run_id, conditions=conditions.declared)
        os.environ[RUN_ENV] = archive.path
        print(f"recording run {conditions.run_id} into {archive.path}")

    def serve(self):
        """The whole launch: import path, run, then the CLI's own command."""
        self.publish_import_path()
        self.start_run(resolve_instance_path(self.argv[0]))
        return cmd_serve(self.command_line)


if __name__ == "__main__":
    sys.exit(BridgeLauncher(sys.argv[1:]).serve())
