"""Contracts for load accounting: in-flight work and chronological delay drift."""

import threading
import time
from types import SimpleNamespace

from benchmarks.execution.load_engine import LoadEngine, UserRunner
from benchmarks.measurement.stop_guard import StopFlag


def test_draining_waits_for_a_response_already_removed_from_the_queue():
    entered, release = threading.Event(), threading.Event()

    class Connection:
        def request(self, *args, **kwargs):
            entered.set()

        def getresponse(self):
            assert release.wait(5)
            return SimpleNamespace(status=200, read=lambda: b"ok")

    engine = LoadEngine([], lambda *args: b"", ["lookup"], StopFlag())
    user = SimpleNamespace(connection=Connection(), headers={})
    runner = UserRunner(engine, "user_1", user)
    engine.runners["user_1"] = runner
    runner.start()
    try:
        runner.inbox.put((time.time(), "measure"))
        assert entered.wait(2)
        assert not engine.drain(timeout=0.05), "an in-flight response is still pending"
        release.set()
        assert engine.drain(timeout=2)
        assert engine.state["completed"] == 1
    finally:
        release.set()
        assert engine.stop_runners() == []


def test_delay_drift_uses_scheduled_order_not_magnitude_or_completion_order():
    engine = LoadEngine([], None, [], StopFlag())
    # Late requests offered early can complete after later, faster requests.
    for offered_at, delay in [(3, 1), (4, 1), (1, 9), (2, 9)]:
        engine.record_call({"phase": "measure", "scheduled_at": offered_at,
                            "lateness_s": delay, "latency_ms": 10, "status": 200,
                            "app_error": "", "transport_error": ""})
    rows = [{"t_rel": index, "rate": 1} for index in range(4)]
    window = engine.close_window("measure", rows, 4, time.time() - 4)
    assert window["late_first_half_s"] == 9
    assert window["late_second_half_s"] == 1
    assert window["late_drift_s"] == -8
