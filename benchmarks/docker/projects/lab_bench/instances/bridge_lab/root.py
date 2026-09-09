#!/usr/bin/env python
import sys
sys.stdout = sys.stderr
from gnr.web.gnrwsgisite import GnrWsgiSite  # noqa: E402 - redirect stdout before site imports
site = GnrWsgiSite(__file__)


def application(environ, start_response):
    return site(environ, start_response)
