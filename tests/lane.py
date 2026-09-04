# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""One live lane for the tests: a GenropyWorker, its real handler, a real desk.

The new core routes every ``collect_page`` (and every store call) through the
commander's delivery desk, so a bare worker cannot serve the register pull:
the tests that drive the client need the FULL protocol. This helper is the
bridge's transcription of the core's own test convention
(genro-asgi ``tests/orchestration/conftest.py``, ``XT_DeskLane``): a real
``SpaCommander`` with one ``GroupHandler``, a real ``WorkerHandler`` bound on
a UDS, and the ``GenropyWorker`` — hosting the real site — presented on it.
Nothing is stubbed; the only thing missing is the commander's beat, which no
register scenario needs.

The lane owns an event loop on a background thread, so plain synchronous
tests keep calling the client from the pytest thread exactly as the site's
WSGI threads do in production: the worker's ``run_on_loop`` hops onto the
lane's loop by itself.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from genro_asgi.channel.frame import FrameStream
from genro_asgi.spa.orchestration import FreezeHandler, GroupHandler, SpaCommander
from genro_asgi.spa.orchestration.worker_handler import PING_OP_PATH, WorkerHandler

#: The name the lane's handler and worker share: short, because a UDS path is.
WORKER_NAME = "pool_0001"


class SiteLane:
    """A GenropyWorker hosting ``source``, wired to a real desk, ready to serve.

    Args:
        source: the GenroPy site (name or path) the worker hosts.

    Built and torn down through :func:`start_site_lane` / :meth:`stop`; every
    coroutine of the protocol runs on the lane's own background loop.
    """

    def __init__(self, source: str) -> None:
        self.source = source
        self.root = Path(tempfile.mkdtemp(prefix="gnrlane_"))
        self.commander = SpaCommander(self.root / "frozen_users")
        self.group = GroupHandler(
            self.commander,
            "pool",
            memory_concession_bytes=8 * 1024 * 1024 * 1024,
            instance_dir=self.root / "i",
            frozen_users_path=self.root / "frozen_users",
            entry_module="never.launched",
        )
        self.worker_handler = WorkerHandler(self.group, WORKER_NAME, **self.group.worker_settings)
        # start_worker hangs the handler in the group's map; the lane stands in
        # for it, so it hangs the handler itself — placement reads this map.
        self.group.worker_handler_map[WORKER_NAME] = self.worker_handler
        self.worker: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._reader_task: asyncio.Task[None] | None = None

    async def _open(self) -> None:
        """Build the worker, bind the socket, present, put the read loop on the air."""
        from genropy_asgi.spa.genropy_worker import GenropyWorker

        self.worker = GenropyWorker(
            WORKER_NAME,
            source=self.source,
            debug=False,
            freeze_handler=FreezeHandler(self.root / "deposit"),
            deposit_lock_retry_interval=0.01,
        )
        connector = self.worker_handler.connector
        await connector.start()
        reader, writer = await asyncio.open_unix_connection(str(connector.socket_path))
        self.worker.attach_stream(FrameStream(reader, writer))
        # The test helpers receive the worker and need the lane back (the way
        # the e2e front is handed the lane): ``call_sink`` delivers through it.
        self.worker.lane = self
        await self.worker.send_presentation({})
        self._reader_task = asyncio.create_task(self.worker.receive_frames())
        await connector.wait_connected()
        # The lane stands in for launch_process, so it performs its state
        # transition too: presented means running, and placement requires it.
        self.worker_handler.state = "running"

    async def _close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self.worker is not None:
            self.worker.exit_process()
        await self.worker_handler.connector.stop()

    def deliver_worker_events(self) -> None:
        """Play the REPLY a request ends with: the worker's queued events reach the desk.

        The commander learns its rows (pages, connections, users) from the
        ``worker_events`` a worker puts on every REPLY it sends. A verb called
        straight from the pytest thread answers no CALL, so its events wait on
        the worker; one ping CALL from the handler makes the worker reply, and
        that REPLY carries them through the handler's envelope chain — the
        production road, with no request body.
        """
        assert self.loop is not None
        asyncio.run_coroutine_threadsafe(
            self.worker_handler.connector.call(PING_OP_PATH, {}), self.loop
        ).result(30)

    def stop(self) -> None:
        """Tear the lane down and stop its loop; the temp root goes with it."""
        loop = self.loop
        if loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._close(), loop).result(30)
        loop.call_soon_threadsafe(loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(30)
        loop.close()
        shutil.rmtree(self.root, ignore_errors=True)


def start_site_lane(source: str) -> SiteLane:
    """One running lane on its own background loop; raises if the site cannot build."""
    lane = SiteLane(source)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="lane-loop")
    thread.start()
    lane.loop = loop
    lane._loop_thread = thread
    try:
        asyncio.run_coroutine_threadsafe(lane._open(), loop).result(120)
    except Exception:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        loop.close()
        shutil.rmtree(lane.root, ignore_errors=True)
        raise
    return lane
