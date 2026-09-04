# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""One live lane for the tests: a GenropyWorker, its real handler, a real desk.

The new core routes every ``collect_page`` (and every store call) through the
commander's delivery desk, so a bare worker cannot serve the register pull:
the tests that drive the client need the FULL protocol. This helper is the
bridge's transcription of the core's own test convention
(genro-asgi ``tests/orchestration/conftest.py``, ``XT_DeskLane``): a real
``GenropySpaCommander`` (the desk is the bridge's, #59) with one ``GroupHandler``, a real ``WorkerHandler`` bound on
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
import time
from pathlib import Path
from typing import Any

from genro_asgi.channel.frame import FrameStream
from genro_asgi.spa.orchestration import FreezeHandler, GroupHandler
from genro_asgi.spa.orchestration.worker_handler import WorkerHandler

from genropy_asgi.spa.genropy_spa_commander import GenropySpaCommander

#: The name the lane's handler and worker share: short, because a UDS path is.
WORKER_NAME = "pool_0001"


class SiteLane:
    """A GenropyWorker hosting ``source``, wired to a real desk, ready to serve.

    Args:
        source: the GenroPy site (name or path) the worker hosts.

    Built and torn down through :func:`start_site_lane` / :meth:`stop`; every
    coroutine of the protocol runs on the lane's own background loop.
    """

    def __init__(
        self, source: str, sibling: SiteLane | None = None, worker_name: str = WORKER_NAME
    ) -> None:
        self.source = source
        self.worker_name = worker_name
        self.root = Path(tempfile.mkdtemp(prefix="gnrlane_"))
        if sibling is None:
            self.commander = GenropySpaCommander(self.root / "frozen_users")
            self.group = GroupHandler(
                self.commander,
                "pool",
                memory_concession_bytes=8 * 1024 * 1024 * 1024,
                instance_dir=self.root / "i",
                frozen_users_path=self.root / "frozen_users",
                entry_module="never.launched",
            )
        else:
            # A SECOND worker under the sibling's commander and group: what the
            # push of the source filter and the cross-worker delivery need.
            self.commander = sibling.commander
            self.group = sibling.group
        self.worker_handler = WorkerHandler(self.group, worker_name, **self.group.worker_settings)
        # start_worker hangs the handler in the group's map; the lane stands in
        # for it, so it hangs the handler itself — placement reads this map.
        self.group.worker_handler_map[worker_name] = self.worker_handler
        self.worker: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._reader_task: asyncio.Task[None] | None = None

    @property
    def desk(self) -> Any:
        """The desk the calls of this lane land on."""
        return self.commander.delivery_desk

    async def _open(self) -> None:
        """Build the worker, bind the socket, present, put the read loop on the air."""
        from genropy_asgi.spa.genropy_worker import GenropyWorker

        self.worker = GenropyWorker(
            self.worker_name,
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
        """Send the desk what the verbs announced: the events of the pytest thread's slot.

        The commander learns its rows (pages, connections, users) from the
        ``worker_events`` a worker collects in the request slot of the CALL it
        serves, and sends on that CALL's REPLY. A verb called straight from the
        pytest thread answers no CALL: its events sit in the slot
        :func:`start_site_lane` opened on that thread, and no REPLY will ever
        carry them. This takes them off the slot and sends them up the worker's
        own channel (``announce_worker_events``, the one the transfer cycle
        uses) — the REPLY of that CALL is the desk's acknowledgement.
        """
        assert self.loop is not None
        events = list(self.worker.worker_events)
        self.worker.worker_events.clear()
        if not events:
            return
        asyncio.run_coroutine_threadsafe(
            self.worker.announce_worker_events(events), self.loop
        ).result(30)

    def run(self, coro: Any, timeout: float = 30.0) -> Any:
        """Run one coroutine of the protocol on the lane's loop, from the pytest thread."""
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def on_loop(self, action: Any, *args: Any, **kwargs: Any) -> Any:
        """Run one sync mutation of the vertex on the lane's loop, where the commander lives.

        A desk or commander method that moves the global table set schedules
        the push as a task on the running loop: called from the pytest thread
        it would find none. The tests that act on the vertex directly — as the
        fold would — go through here.
        """

        async def run_action() -> Any:
            return action(*args, **kwargs)

        return self.run(run_action())

    def verb(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Call one of the worker's site verbs from this thread, then send up what it announced.

        The site calls its verbs on a WSGI thread inside a CALL, whose REPLY
        carries the events; the pytest thread holds a slot of its own (opened by
        :func:`start_site_lane`) and no REPLY answers it, so the lane sends the
        events up itself — the vertex must know the rows the verbs made before
        the next verb addresses them.
        """
        answer = getattr(self.worker, name)(*args, **kwargs)
        self.deliver_worker_events()
        return answer

    def open_request(self) -> None:
        """Start a fresh request on this thread: a new slot, as the stitching opens one."""
        self.worker.open_request_slot()

    def wait_filter_synced(self, timeout: float = 10.0) -> None:
        """Wait until the worker's source filter equals the desk's set.

        The commander pushes the set as a task on its loop, so a test that reads
        the worker's ``subscribed_tables`` right after a subscription waits here.
        """
        wait_until(lambda: self.worker.subscribed_tables == set(self.desk.subscribed_tables), timeout)

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
        self.group.worker_handler_map.pop(self.worker_name, None)
        shutil.rmtree(self.root, ignore_errors=True)


def wait_until(condition: Any, timeout: float = 10.0) -> None:
    """Poll from the pytest thread until the condition holds, or give up loudly."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise TimeoutError("the machine never reached the awaited state")
        time.sleep(0.01)


def start_site_lane(
    source: str, sibling: SiteLane | None = None, worker_name: str = WORKER_NAME
) -> SiteLane:
    """One running lane on its own background loop; raises if the site cannot build.

    Args:
        source: the GenroPy site the worker hosts.
        sibling: another running lane whose commander and group this one joins,
            as a second worker of the same pool; None builds its own vertex.
        worker_name: the name of the handler and the worker (short: a UDS path is).
    """
    lane = SiteLane(source, sibling=sibling, worker_name=worker_name)
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
    # The tests call the site verbs from this thread, and a verb announces into
    # the slot of the CALL it serves: give the thread one.
    lane.worker.open_request_slot()
    return lane
