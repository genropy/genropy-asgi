# Copyright 2025-2026 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Contract: a recipe-born bridge worker records a served page and its ping.

Opt in with GNR_BENCH_SMOKE_SITE naming a configured demo site. The real CLI,
template, forked worker and HTTP path are exercised; output and sockets are
isolated, and the server process group is stopped even on failure.
"""

import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx
import pytest


@pytest.mark.skipif(not os.environ.get('GNR_BENCH_SMOKE_SITE'),
                    reason='set GNR_BENCH_SMOKE_SITE to a configured demo site')
def test_recorded_bridge_serves_and_joins_register_calls(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='brec_') as runtime:
        env = dict(os.environ, GNR_DAEMON_PROVIDER='genropy-asgi', PGGSSENCMODE='disable',
                   GNR_BENCH_ARCHIVE_DIR=str(output), GNR_ASGI_INSTANCE_DIR=runtime,
                   GNR_ASGI_FROZEN_USERS_PATH=str(Path(runtime) / 'frozen'),
                   PYTHONPATH=os.pathsep.join([str(root / 'src'), str(root), os.environ.get('PYTHONPATH', '')]))
        with (output / 'server.log').open('w') as log:
            process = subprocess.Popen([sys.executable, '-m', 'benchmarks.execution.serve_bridge',
                                        os.environ['GNR_BENCH_SMOKE_SITE'], '-p', str(port), '--nodebug'],
                                       env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=5) as client:
                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise RuntimeError(f'launcher exited with {process.returncode}; see server.log')
                        try:
                            if client.get('/metrics').status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(.25)
                    else:
                        raise RuntimeError('server readiness timeout')
                    response = client.get('/', timeout=25)
                    assert response.status_code == 200, response.status_code
                    page = re.search(r"page_id:'([\w-]+)'", response.text)
                    assert page, 'missing page bootstrap'
                    assert 'spa_connection_id' in client.cookies, 'missing routing cookie'
                    ping = client.get('/_ping', params={'page_id': page.group(1)}, timeout=15)
                    assert ping.status_code == 200, ping.status_code
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        archives = sorted(output.glob('bridge-*.sqlite'), key=lambda p: p.stat().st_mtime)
                        if archives:
                            with sqlite3.connect(archives[-1]) as connection:
                                counts = dict(connection.execute('SELECT kind, count(*) FROM record GROUP BY kind'))
                                joined = connection.execute("SELECT count(*) FROM record r WHERE r.kind='register' AND EXISTS (SELECT 1 FROM record h WHERE h.kind='http' AND h.exchange_id=r.exchange_id)").fetchone()[0]
                            if counts.get('http', 0) >= 2 and joined:
                                assert not counts.get('recorder_error'), counts
                                break
                        time.sleep(.1)
                    else:
                        raise RuntimeError('recordings did not arrive')
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
