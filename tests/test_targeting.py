"""TargetingSession with a fake capture function."""

import threading

from macro_recorder.gui.targeting import Capture, TargetingSession
from macro_recorder.platform.input import TargetingError


def run(capture, cancel=False):
    results = []
    session = TargetingSession(lambda c, e: results.append((c, e)), timeout_s=5.0,
                               capture=capture)
    session.start()
    if cancel:
        session.cancel()
    session.join(5.0)
    assert not session.is_running()
    return results


def test_capture_is_delivered():
    seen = {}

    def capture(timeout_s, cancelled):
        seen["timeout"] = timeout_s
        return (10, 20, "#ff0000")

    assert run(capture) == [(Capture(10, 20, "#ff0000"), None)]
    assert seen["timeout"] == 5.0


def test_user_cancel_delivers_none():
    assert run(lambda t, c: None) == [(None, None)]


def test_cancel_stops_the_capture():
    started = threading.Event()

    def capture(timeout_s, cancelled):
        started.set()
        while not cancelled():
            threading.Event().wait(0.01)
        return (1, 2, "#000000")

    results = []
    session = TargetingSession(lambda c, e: results.append((c, e)), capture=capture)
    session.start()
    assert started.wait(5.0)
    session.cancel()
    session.join(5.0)
    assert results == [(None, None)]


def test_grab_failure_is_reported():
    def capture(timeout_s, cancelled):
        raise TargetingError("Another application holds the pointer; targeting cancelled")

    assert run(capture) == [(None, "Another application holds the pointer; targeting cancelled")]


def test_display_failure_is_reported():
    def capture(timeout_s, cancelled):
        raise OSError("Cannot open X display")

    assert run(capture) == [(None, "Targeting failed: Cannot open X display")]
