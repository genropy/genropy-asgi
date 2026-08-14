"""Gunicorn config: install the raw global register probe in each worker."""
import sys


def post_worker_init(worker):
    if "/tmp" not in sys.path:
        sys.path.insert(0, "/tmp")
    import probe_reg  # noqa: F401
