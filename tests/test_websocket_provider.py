"""Exercise provider discovery in fresh interpreters with real legacy imports."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest


class WebSocketProviderTest(unittest.TestCase):
    def run_import(self, provider, code):
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / 'pyproject.toml').read_text())
        entry = project['project']['entry-points']['gnr.web']['websockethandler']
        with tempfile.TemporaryDirectory() as temporary:
            metadata = Path(temporary) / 'genropy_asgi-0.0.0.dist-info'
            metadata.mkdir()
            (metadata / 'METADATA').write_text(
                'Metadata-Version: 2.1\nName: genropy-asgi\nVersion: 0.0.0\n')
            (metadata / 'entry_points.txt').write_text(
                f'[gnr.web]\nwebsockethandler = {entry}\n')
            env = os.environ.copy()
            env.pop('GNR_WEBSOCKET_PROVIDER', None)
            if provider:
                env['GNR_WEBSOCKET_PROVIDER'] = provider
            env['PYTHONPATH'] = os.pathsep.join(
                [temporary, str(root / 'src'), env.get('PYTHONPATH', '')])
            return subprocess.run(
                [sys.executable, '-c', code], env=env,
                capture_output=True, text=True, timeout=20)

    def test_bridge_import_does_not_open_socket(self):
        result = self.run_import('genropy-asgi', '''
import gnr
from unittest.mock import patch
with patch('socket.socket', side_effect=AssertionError('unexpected socket')):
    from gnr.web.gnrwsgisite_proxy.gnrwebsockethandler import WsgiWebSocketHandler
    from genropy_asgi.websockethandler import WsgiWebSocketHandler as Expected
    assert WsgiWebSocketHandler is Expected
    handler = WsgiWebSocketHandler(object())
    assert handler.checkSocket() is True
    assert handler.sendCommandToPage('', 'registerNewPage', {}) is None
    assert handler.sendCommandToPage('page', 'publish', {}) is None
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_classic_import_is_unchanged(self):
        result = self.run_import(None, '''
from gnr.web.gnrwsgisite_proxy.gnrwebsockethandler import WsgiWebSocketHandler
assert WsgiWebSocketHandler.__module__ == 'gnr.web.gnrwsgisite_proxy.gnrwebsockethandler'
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_provider_fails_explicitly(self):
        result = self.run_import('missing-provider', '''
from gnr.web.gnrwsgisite_proxy.gnrwebsockethandler import WsgiWebSocketHandler
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('matches 0', result.stderr)


if __name__ == '__main__':
    unittest.main()
