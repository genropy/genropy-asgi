# Copyright 2025-2026 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0
"""Serve a prebuilt documentation snapshot from an explicit read-only directory."""
import asyncio
import mimetypes
from pathlib import Path

from genro_asgi.application import BaseApplication
from genro_asgi.response import Response


class DocumentationApplication(BaseApplication):
    def __init__(self, *, directory, **kwargs):
        super().__init__(**kwargs)
        self.directory = Path(directory).resolve()

    def read_document(self, path):
        relative = Path(path.lstrip('/'))
        if '..' in relative.parts:
            return None
        target = (self.directory / relative).resolve()
        if not target.is_relative_to(self.directory):
            return None
        if target.is_dir():
            target = (target / 'index.html').resolve()
        if not target.is_relative_to(self.directory) or not target.is_file():
            return None
        return target.read_bytes(), mimetypes.guess_type(target.name)[0] or 'application/octet-stream'

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return
        if scope['method'] not in ('GET', 'HEAD'):
            response = Response(content='Read only', status_code=405)
        else:
            result = await asyncio.to_thread(self.read_document, scope.get('path', '/'))
            if result is None:
                response = Response(content='Not found', status_code=404)
            else:
                content, media_type = result
                response = Response(content=b'' if scope['method'] == 'HEAD' else content,
                                    media_type=media_type,
                                    headers={'x-content-type-options': 'nosniff'})
        await response(scope, receive, send)
