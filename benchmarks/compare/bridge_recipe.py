"""Compatibility recipe; the canonical definition is in execution/."""

import sys
from pathlib import Path

_REPOSITORY_ROOT = str(Path(__file__).resolve().parents[2])
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from benchmarks.execution.bridge_recipe import (  # noqa: E402,F401
    RECORDING_ENGINE_FACTORY, RECORDING_WORKER,
    ServerConfiguration as RecordingConfiguration,
)


class ServerConfiguration(RecordingConfiguration):
    """Expose the canonical recipe to file-based configuration loaders."""
