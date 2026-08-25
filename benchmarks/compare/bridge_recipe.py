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
        commander = front.commander(
            frozen_users_path=frozen_users_path,
            instance_dir=instance_dir,
        )
        group_kwargs: dict[str, Any] = {
            "name": "pool",
            "entry_module": "genro_asgi.spa.orchestration.worker_entry",
            "worker_class": RECORDING_WORKER,
            "worker_kwargs": {"source": source, "debug": debug},
            "engine_factory": RECORDING_ENGINE_FACTORY,
            "engine_kwargs": {"source": source, "debug": debug},
        }
        idle_minutes = os.environ.get("GNR_ASGI_IDLE_FREEZE_MINUTES")
        if idle_minutes:
            group_kwargs["user_idle_freeze_minutes"] = float(idle_minutes)
        commander.groups().group(**group_kwargs)
