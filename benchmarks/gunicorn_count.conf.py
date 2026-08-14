"""Gunicorn config that installs the register-call counter in each worker.

Usage:
  gnr web serveprod test_invoice_pg -b 127.0.0.1:8099 -w 1 -k gthread \
      --threads 8 -c temp/benchmark/assets/gunicorn_count.conf.py
"""
import sys

_COUNTER_DIR = ("/Users/gporcari/Sviluppo/genro_ng/meta-genro-modules/"
                "sub-projects/genropy-asgi/temp/benchmark/assets")


def post_worker_init(worker):
    # genropy + genro_daemon are importable here (after fork, app loaded)
    if _COUNTER_DIR not in sys.path:
        sys.path.insert(0, _COUNTER_DIR)
    import sr_counter  # noqa: F401  (its install() runs on import)
