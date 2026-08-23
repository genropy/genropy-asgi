"""Gunicorn config installing the bench recorders in each worker.

Usage, from the repository root:

  PGGSSENCMODE=disable temp/legacy_venv/bin/gnr web serveprod test_invoice_pg_legacy \
      -b 127.0.0.1:8099 -w 1 -k gthread --threads 16 \
      -c benchmarks/compare/gunicorn_recorders.conf.py

`post_worker_init` runs right after gunicorn's own `load_wsgi()`, so
`worker.wsgi` is already the site application and wrapping it here is enough
(verified on gunicorn 26.1.0, `Worker.init_process`).

The hook holds no logic of its own: installing a recorder is one call, the same
call the bridge makes in macro-phase 2, where there is no gunicorn at all. The
import lives inside the hook because this directory has to reach `sys.path`
first — the same shape as `benchmarks/gunicorn_count.conf.py`.
"""

import inspect
import os
import sys

# `__file__` is not defined here: genropy's own `load_config_file`
# (gnr/web/cli/gnrserveprod.py) execs this file into a bare dict, unlike
# gunicorn's loader. The path genropy compiled is on the frame instead, which
# works for any `-c` argument, relative or absolute, from any working directory.
_RECORDERS_DIR = os.path.dirname(
    os.path.abspath(inspect.currentframe().f_code.co_filename))


def post_worker_init(worker):
    if _RECORDERS_DIR not in sys.path:
        sys.path.insert(0, _RECORDERS_DIR)
    from http_recorder import HttpRecorder
    worker.wsgi = HttpRecorder(worker.wsgi)
