"""Compatibility entry point for benchmarks.execution.serve_bridge."""

import sys
from pathlib import Path

_REPOSITORY_ROOT = str(Path(__file__).resolve().parents[2])
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from benchmarks.execution import serve_bridge as _implementation  # noqa: E402

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
