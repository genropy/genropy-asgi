"""Start the legacy stack with both bench recorders installed.

The register recorder cannot be installed from a gunicorn hook: `main()` builds
`GnrWsgiSite` before it reads the `-c` configuration file, and the site's
`__init__` forces the register into existence, so the client already exists in
the master process before any hook runs and before the fork. This launcher is
the install point — the assignment below, then genropy's own entry point.

The HTTP recorder keeps its own install point, `post_worker_init` in
`gunicorn_recorders.conf.py`, which runs late on purpose: it needs the loaded
application to wrap. Two recorders, two install points, one command.

Run, from the repository root:

  PGGSSENCMODE=disable temp/legacy_venv/bin/python \
      benchmarks/compare/serve_legacy.py test_invoice_pg_legacy \
      -b 127.0.0.1:8099 -w 1 -k gthread --threads 16 \
      -c benchmarks/compare/gunicorn_recorders.conf.py

Every argument after the script is genropy's own `serveprod` command line: this
launcher adds nothing to it and takes nothing away.
"""

from gnr.web import gnrwsgisite
from gnr.web.cli.gnrserveprod import main

from register_recorder import RegisterRecorder

if __name__ == "__main__":
    gnrwsgisite.SiteRegisterClient = RegisterRecorder
    main()
