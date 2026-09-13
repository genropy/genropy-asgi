# Copyright 2025-2026 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0
"""Small read-only portal; benchmark workloads run in a separate Compose project."""
import asyncio
import html
import json
import os
import re

from genro_asgi import AsgiServer
from genro_asgi.application import BaseApplication
from genro_asgi.response import Response

from benchmarks.analysis.cycle_report import completeness, cell
from benchmarks.portal import store
from benchmarks.portal.documentation import DocumentationApplication


def page(title, body):
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>'
            'body{font:16px system-ui;margin:2rem;max-width:1200px;color:#182331}'
            'table{border-collapse:collapse;width:100%}td,th{padding:.6rem;border-bottom:1px solid #ddd;'
            'text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}'
            'a{color:#165dad}.scroll{overflow:auto}</style>'
            f'<body><nav><a href="/">Genro ASGI</a> · <a href="/benchmarks/">Benchmarks</a></nav>'
            f'<h1>{html.escape(title)}</h1>{body}</body></html>')


class PortalApplication(BaseApplication):
    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return
        status, kind = 200, 'text/html'
        path = scope.get('path', '/').strip('/')
        if self.code == 'api':
            if path == 'benchmarks' or path.startswith('benchmarks/'):
                path = path[len('benchmarks'):].strip('/')
            else:
                path = '!not-found'
        if scope['method'] not in ('GET', 'HEAD'):
            content, status = 'Read-only portal', 405
        elif self.code == 'home':
            if path == 'health':
                try:
                    await asyncio.to_thread(store.list_runs)
                    content, kind = json.dumps({'status': 'ok'}), 'application/json'
                except Exception:
                    content, status, kind = '{"status":"unavailable"}', 503, 'application/json'
            elif not path:
                content = page('Genro ASGI', '<p>Applications</p><ul><li><a href="/docs/">Genro ASGI — user guide and API reference</a></li><li><a href="/internals/">Genro ASGI — technical documentation</a></li><li><a href="/benchmarks/">Benchmark results</a></li></ul>')
            else:
                content, status = 'Not found', 404
        elif not path:
            rows = await asyncio.to_thread(store.list_runs)
            if self.code == 'api':
                content, kind = json.dumps(rows), 'application/json'
            else:
                body = '<p>Latest 200 imports. Functional timings are not capacity measurements.</p>'
                if not rows:
                    body += '<p>No results imported yet.</p>'
                body += '<div class="scroll"><table><tr>' + ''.join(
                    f'<th>{label}</th>' for label in ['Run', 'Campaign', 'Environment', 'Purpose', 'Stack', 'Outcome']) + '</tr>'
                for row in rows:
                    body += f'<tr><td><a href="/benchmarks/{row["id"]}">{cell(row["run_name"])}</a></td>'
                    body += ''.join(f'<td>{cell(row[key])}</td>' for key in
                                    ['campaign', 'environment', 'purpose', 'stack', 'result']) + '</tr>'
                content = page('Benchmark results', body + '</table></div>')
        elif re.fullmatch('[0-9a-f]{64}', path):
            row = await asyncio.to_thread(store.get_run, path)
            if row is None:
                content, status = 'Not found', 404
            elif self.code == 'api':
                # Raw input is intentionally CLI-only; API exposes selected fields.
                content, kind = json.dumps({k: row[k] for k in
                    ('id', 'campaign', 'environment', 'purpose', 'revision', 'run_name',
                     'stack', 'windows', 'provenance')}), 'application/json'
            else:
                issues = completeness({'outcome': row['outcome'], 'windows': row['windows']})
                state = '; '.join(issues) if issues else 'Complete'
                body = f'<p>{cell(row["environment"])} · {cell(row["purpose"])} · {cell(state)}</p>'
                assessment = row['provenance'].get('assessment')
                if assessment:
                    body += '<h2>Historical assessment</h2><pre>' + html.escape(
                        json.dumps(assessment, indent=2, ensure_ascii=False)) + '</pre>'
                body += f'<p>Revision: {cell(row["revision"])}</p>'
                body += '<pre>' + html.escape(json.dumps(row['windows'], indent=2)) + '</pre>'
                content = page(row['run_name'], body)
        else:
            content, status = 'Not found', 404
        response = Response(content='' if scope['method'] == 'HEAD' else content,
                            status_code=status, media_type=kind,
                            headers={'cache-control': 'no-store', 'x-content-type-options': 'nosniff'})
        await response(scope, receive, send)


def create_server():
    return AsgiServer(applications=[
        DocumentationApplication(code='docs', mount='docs',
                                 directory=os.environ.get('PORTAL_USER_DOCS_DIR', '/user-docs')),
        DocumentationApplication(code='internals', mount='internals',
                                 directory=os.environ.get('PORTAL_DOCS_DIR', '/docs')),
        PortalApplication(code='home', mount=''),
                                   PortalApplication(code='benchmarks', mount='benchmarks'),
                                   PortalApplication(code='api', mount='api')])


if __name__ == '__main__':
    create_server().serve(host=os.environ.get('HOST', '127.0.0.1'),
                          port=int(os.environ.get('PORT', '8080')))
