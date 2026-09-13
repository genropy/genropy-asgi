"""Compatibility entry point for benchmarks.analysis.structural_diff.

PROVISIONAL: preserve historical imports while callers migrate.
"""

import sys
from pathlib import Path

_REPOSITORY_ROOT = str(Path(__file__).resolve().parents[2])
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from benchmarks.analysis import structural_diff as _implementation  # noqa: E402

sys.modules[__name__] = _implementation
