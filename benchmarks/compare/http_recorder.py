"""Compatibility import for benchmarks.recording.http_recorder.

PROVISIONAL: retained while comparison launchers migrate. The canonical module
is aliased, not copied, so class identity and module-level overrides are shared.
"""

import sys
from pathlib import Path

_REPOSITORY_ROOT = str(Path(__file__).resolve().parents[2])
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from benchmarks.recording import http_recorder as _implementation  # noqa: E402

sys.modules[__name__] = _implementation
