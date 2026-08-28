"""The bench recipe: the bridge's own server recipe, with the recorders installed.

The install rides the RECIPE. The pool names its worker class and its engine
factory as import strings that the template and worker processes resolve for
themselves, so naming the bench's two classes here installs both recorders —
nothing is patched, nothing is monkey-hooked, and neither genro-asgi nor
genropy-asgi is modified. The CLI takes this file with its own ``--config``
option, which exists for exactly this: a recipe that carries the pool shape
while the instance named on the command line still wins.

TWO lines, because the workers are born by fork and the two recorders therefore
install in two different processes: the register recorder in the TEMPLATE, where
the site — and with it its register client — is built, and the HTTP recorder in
each forked child, where ``wsgi_app`` is assigned. ``engine_factory`` names the
first, ``worker_class`` the second.

``main`` is the shipped recipe's, transcribed, with those two lines changed.
``bridge_coverage_check.py`` builds both recipes through the runtime's own read
door and fails if they come to differ in anything else, so the copy cannot drift
in silence.

The recipe DECLARES and does nothing else: building it must stay free of
consequences, or the drift check could not build it. The run archive is minted
by ``serve_bridge.py``, which is this stack's counterpart of
``serve_legacy.py`` — one owner of the run per stack.
"""

import os
import tempfile
from typing import Any

from genro_asgi.applications.spa_console import SpaConsoleMcpApplication
from genro_bag.resolvers import EnvResolver

from genropy_asgi.spa.config import DEBUG_OFF_WORDS
from genropy_asgi.spa.config import ServerConfiguration as BridgeConfiguration
from genropy_asgi.spa.genropy_spa_application import GenropySpaApplication

# The two lines by which this recipe differs from the one the package ships.
RECORDING_WORKER = "recording_worker:RecordingGenropyWorker"
RECORDING_ENGINE_FACTORY = "recording_engine_factory:RecordingSiteEngineFactory"


class ServerConfiguration(BridgeConfiguration):
    """The shipped recipe, with the recording worker and factory in the pool."""

    def main(self, root: Any) -> None:
        """The shipped document, transcribed, with the two recording classes."""
        cfg = root.configuration()
        cfg.server(
            host=EnvResolver("GNR_ASGI_HOST", default="127.0.0.1"),
            port=EnvResolver("GNR_ASGI_PORT", default=8000, dtype="L"),
        )
        cfg.middleware()
        source = os.environ.get("GNR_ASGI_PATH") or ""
        debug_env = os.environ.get("GNR_ASGI_DEBUG")
        debug = True if debug_env is None else debug_env.strip().lower() not in DEBUG_OFF_WORDS
        # The werkzeug debugger is NOT part of debug: its error page evaluates
        # Python in the process, so it comes on only when asked for by name.
        debugger = bool(os.environ.get("GNR_ASGI_DEBUGGER"))
        site_key = os.path.basename(os.path.normpath(source)) or "site"
        frozen_users_path = os.environ.get("GNR_ASGI_FROZEN_USERS_PATH") or os.path.join(
            source, "data", "_frozen_users"
        )
        instance_dir = os.environ.get("GNR_ASGI_INSTANCE_DIR") or os.path.join(
            tempfile.gettempdir(), f"gnrasgi_{site_key}"
        )
        applications = cfg.applications()
        front = applications.application(
            code="site",
            mount="",
            app_class=GenropySpaApplication,
        )
        if os.environ.get("GNR_ASGI_CONSOLE"):
            applications.application(
                code="console",
                mount="_console",
                app_class=SpaConsoleMcpApplication,
            )
        # genro-asgi 0.36: the pool hangs off the front's ``orchestration``
        # node, and that node is mandatory for a spa front. Only the shape
        # moves — the group below, its parameters and what this bench measures
        # are untouched.
        orchestration = front.orchestration()
        commander = orchestration.commander(
            frozen_users_path=frozen_users_path,
            instance_dir=instance_dir,
        )
        group_kwargs: dict[str, Any] = {
            "name": "pool",
            "entry_module": "genro_asgi.spa.orchestration.worker_entry",
            "worker_class": RECORDING_WORKER,
            "worker_kwargs": {"source": source, "debug": debug, "debugger": debugger},
            "engine_factory": RECORDING_ENGINE_FACTORY,
            "engine_kwargs": {"source": source, "debug": debug},
        }
        idle_minutes = os.environ.get("GNR_ASGI_IDLE_FREEZE_MINUTES")
        if idle_minutes:
            group_kwargs["user_idle_freeze_minutes"] = float(idle_minutes)
        # How many users one worker may hold before it refuses the next. Unset,
        # the core's own default governs and a worker takes everybody, so the
        # pool never grows on a small site. Set to 1 the bench puts each user on
        # a worker of his own, which is the only way the cross-worker paths —
        # the register population, the stores, the datachanges between users —
        # are exercised at all.
        max_users = os.environ.get("GNR_ASGI_WORKER_MAX_USERS")
        if max_users:
            group_kwargs["worker_max_users"] = int(max_users)
        commander.groups().group(**group_kwargs)
