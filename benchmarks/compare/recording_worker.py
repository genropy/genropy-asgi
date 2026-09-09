"""Compatibility entry point for benchmarks.recording.recording_worker."""

import sys
from pathlib import Path

_REPOSITORY_ROOT = str(Path(__file__).resolve().parents[2])
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from benchmarks.recording import recording_worker as _implementation  # noqa: E402

sys.modules[__name__] = _implementation
