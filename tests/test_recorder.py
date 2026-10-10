"""Recorder filtering: own windows, stop keys, markers, listener death."""

from __future__ import annotations

import threading

import pytest

from macro_recorder.engine.recorder import Recorder, RecorderError
from macro_recorder.model.protocols import RawEvent
from macro_recorder.platform.session import OwnWindows
from tests.conftest import FakeListener, FakeWindows, window

APP = window(0x900, "Macro Recorder", 1500, 0, 400, 300)
OVERLAY = window(0x901, "overlay", 0, 0, 1920, 1080)
XED = window(0x100, "xed", 100, 80, 900, 600)
FIREFOX = window(0x200, "Firefox", 1000, 0, 400, 800)


class Clock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def rig():
    """A recorder over fake windows: the app window first so it wins hit tests."""
    windows = FakeWindows([APP, XED, FIREFOX, OVERLAY])
    windows.active_id = XED.id
    own = OwnWindows()
    own.add(APP.id)
    own.add(OVERLAY.id, overlay=True)
    listener = FakeListener()
    clock = Clock()
    recorder = Recorder(listener, windows, own, record_combo="ctrl+shift+F9", clock=clock)
    return recorder, listener, windows, clock


def ev(kind, t, **kw):
    return RawEvent(kind, 100.0 + t, **kw)


def kinds(events):
    return [(e.kind, e.key or e.button or (e.window.title if e.window else "")) for e in events]


def test_start_emits_initial_marker_at_time_zero(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    assert listener.is_alive()
    events, cursor = recorder.events_since(0)
    assert kinds(events) == [("window_focus", "xed")]
    assert events[0].time == 0.0
    assert cursor == 1


def test_no_initial_marker_for_own_window(rig):
    recorder, listener, windows, clock = rig
    windows.active_id = APP.id
    recorder.start()
    assert recorder.events_since(0)[0] == []


def test_times_are_relative_to_start(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    listener.feed(ev("motion", 1.5, x=300, y=300))
    events, _ = recorder.events_since(1)
    assert events[0].time == pytest.approx(1.5)
    clock.t = 102.0
    assert recorder.now() == pytest.approx(2.0)


def test_events_since_returns_increments(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    _, cursor = recorder.events_since(0)
    listener.feed(ev("motion", 0.1, x=300, y=300), ev("motion", 0.2, x=310, y=300))
    events, cursor = recorder.events_since(cursor)
    assert len(events) == 2
    listener.feed(ev("motion", 0.3, x=320, y=300))
    events, cursor = recorder.events_since(cursor)
    assert len(events) == 1 and cursor == 4


def test_click_on_own_window_drops_press_moves_and_release(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    listener.feed(
        ev("button_press", 0.1, x=1600, y=100, button="left"),
        ev("motion", 0.2, x=1610, y=100),
        ev("button_release", 0.3, x=200, y=200, button="left"),
        ev("motion", 0.4, x=300, y=300),
        ev("button_press", 0.5, x=300, y=300, button="left"),
        ev("button_release", 0.6, x=300, y=300, button="left"),
    )
    recorder.stop()
    assert kinds(recorder.result()) == [("window_focus", "xed"), ("motion", ""),
                                        ("button_press", "left"), ("button_release", "left")]


def test_clicks_on_the_overlay_are_recorded(rig):
    recorder, listener, windows, clock = rig
    windows.windows = [XED, OVERLAY]       # the overlay is the only window under the point
    recorder.start()
    listener.feed(ev("button_press", 0.1, x=50, y=50, button="left"),
                  ev("button_release", 0.2, x=50, y=50, button="left"))
    recorder.stop()
    assert [e.kind for e in recorder.result()][1:] == ["button_press", "button_release"]


def test_scroll_over_own_window_is_dropped(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    listener.feed(ev("scroll", 0.1, x=1600, y=100, dy=1), ev("scroll", 0.2, x=300, y=300, dy=1))
    recorder.stop()
    assert [(e.kind, e.x) for e in recorder.result()][1:] == [("scroll", 300)]


def test_keys_dropped_while_own_window_is_active(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    listener.feed(ev("key_press", 0.1, key="a"), ev("key_release", 0.2, key="a"))
    windows.active_id = APP.id
    listener.feed(ev("key_press", 0.3, key="b"), ev("key_release", 0.4, key="b"))
    recorder.stop()
    assert kinds(recorder.result())[1:] == [("key_press", "a"), ("key_release", "a")]


def test_release_follows_its_press_across_a_focus_change(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    listener.feed(ev("key_press", 0.1, key="a"))
    windows.active_id = APP.id
    listener.feed(ev("key_press", 0.15, key="a"), ev("key_release", 0.2, key="a"),
                  ev("key_press", 0.3, key="b"))
    windows.active_id = XED.id
    listener.feed(ev("key_release", 0.4, key="b"))
    recorder.stop()
    assert kinds(recorder.result())[1:] == [("key_press", "a"), ("key_press", "a"), ("key_release", "a")]


def test_releases_of_keys_pressed_before_start_are_dropped(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    listener.feed(ev("key_release", 0.05, key="F9"), ev("button_release", 0.06, button="left"))
    recorder.stop()
    assert kinds(recorder.result()) == [("window_focus", "xed")]


def test_watcher_markers_skip_own_and_untitled_windows(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    clock.t = 101.0
    windows.activate(FIREFOX)
    windows.activate(APP)
    windows.activate(window(0x300, ""))
    recorder.stop()
    windows.activate(XED)                   # after stop: no watcher
    events = recorder.result()
    assert kinds(events) == [("window_focus", "xed"), ("window_focus", "Firefox")]
    assert events[1].time == pytest.approx(1.0)


def test_stop_key_halts_and_is_never_recorded(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    listener.feed(ev("key_press", 0.1, key="a"), ev("key_release", 0.2, key="a"),
                  ev("key_press", 0.3, key="F6"))
    assert recorder.stop_requested
    assert not recorder.recording
    listener.feed(ev("key_release", 0.4, key="F6"), ev("key_press", 0.5, key="z"))
    recorder.stop()
    assert kinds(recorder.result())[1:] == [("key_press", "a"), ("key_release", "a")]


def test_record_combo_halts_and_cleans_trailing_modifiers(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    listener.feed(
        ev("key_press", 0.1, key="Shift_L"), ev("key_press", 0.12, key="A"),
        ev("key_release", 0.15, key="A"), ev("key_release", 0.2, key="Shift_L"),
        ev("key_press", 1.0, key="Control_R"), ev("motion", 1.05, x=300, y=300),
        ev("key_press", 1.1, key="Shift_L"), ev("key_press", 1.2, key="F9"),
        ev("key_release", 1.3, key="F9"), ev("key_release", 1.4, key="Shift_L"),
        ev("key_release", 1.5, key="Control_R"),
    )
    assert recorder.stop_requested
    recorder.stop()
    assert kinds(recorder.result())[1:] == [("key_press", "Shift_L"), ("key_press", "A"),
                                            ("key_release", "A"), ("key_release", "Shift_L"),
                                            ("motion", "")]


def test_held_start_hotkey_autorepeat_is_dropped_until_released(rig):
    recorder, listener, windows, clock = rig
    recorder.start(start_key="F9")
    listener.feed(ev("key_press", 0.5, key="F9"), ev("key_press", 0.55, key="F9"),
                  ev("key_release", 0.6, key="F9"), ev("key_release", 0.7, key="Shift_L"),
                  ev("key_release", 0.75, key="Control_L"), ev("key_press", 1.0, key="F9"),
                  ev("key_release", 1.1, key="F9"))
    assert recorder.recording
    recorder.stop()
    assert kinds(recorder.result())[1:] == [("key_press", "F9"), ("key_release", "F9")]


def test_record_combo_still_stops_after_the_start_key_is_released(rig):
    recorder, listener, windows, clock = rig
    recorder.start(start_key="F9")
    listener.feed(ev("key_release", 0.1, key="F9"), ev("key_press", 1.0, key="Control_L"),
                  ev("key_press", 1.1, key="Shift_L"), ev("key_press", 1.2, key="F9"))
    assert recorder.stop_requested


def test_failed_hit_test_treats_the_window_as_foreign(rig, caplog):
    recorder, listener, windows, clock = rig

    def broken(x, y):
        raise OSError("BadWindow")
    windows.window_at = broken
    recorder.start()
    listener.feed(ev("button_press", 0.1, x=1600, y=100, button="left"),
                  ev("button_release", 0.2, x=1600, y=100, button="left"))
    recorder.stop()
    assert kinds(recorder.result())[1:] == [("button_press", "left"), ("button_release", "left")]
    assert "hit test" in caplog.text


def test_failed_active_query_while_recording_keeps_the_key(rig, caplog):
    recorder, listener, windows, clock = rig
    recorder.start()

    def broken():
        raise OSError("connection closed")
    windows.active = broken
    listener.feed(ev("key_press", 0.1, key="a"), ev("key_release", 0.2, key="a"))
    recorder.stop()
    assert kinds(recorder.result())[1:] == [("key_press", "a"), ("key_release", "a")]
    assert "active window query failed" in caplog.text


def test_failed_active_query_at_start_raises_recorder_error(rig):
    recorder, listener, windows, clock = rig

    def broken():
        raise OSError("connection closed")
    windows.active = broken
    with pytest.raises(RecorderError, match="connection closed"):
        recorder.start()
    assert not listener.is_alive()
    assert not recorder.recording


def test_combo_key_without_its_modifiers_is_recorded(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    listener.feed(ev("key_press", 0.1, key="F9"), ev("key_release", 0.2, key="F9"))
    assert not recorder.stop_requested
    recorder.stop()
    assert kinds(recorder.result())[1:] == [("key_press", "F9"), ("key_release", "F9")]


def test_stop_from_the_gui_before_the_combo_arrives(rig):
    """The hotkey grab fires first; the combo's press arrives after stop and is discarded."""
    recorder, listener, windows, clock = rig
    recorder.start()
    listener.feed(ev("key_press", 0.1, key="Control_L"), ev("key_press", 0.2, key="Shift_L"))
    recorder.stop()
    listener.feed(ev("key_press", 0.3, key="F9"), ev("key_release", 0.4, key="F9"))
    assert kinds(recorder.result()) == [("window_focus", "xed")]
    assert not listener.is_alive()


def test_releases_after_stop_are_discarded(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    listener.feed(ev("button_press", 0.1, x=300, y=300, button="left"))
    callback = listener._callback
    recorder.stop()
    callback(ev("button_release", 0.2, x=300, y=300, button="left"))
    assert kinds(recorder.result())[1:] == [("button_press", "left")]


def test_listener_death_stops_with_error(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    listener.feed(ev("key_press", 0.1, key="a"))
    listener.alive = False
    with pytest.raises(RecorderError, match="stopped unexpectedly"):
        recorder.events_since(0)
    assert not recorder.recording
    assert recorder.error
    recorder.stop()
    assert kinds(recorder.result())[1:] == [("key_press", "a")]


def test_listener_start_failure_raises_recorder_error(rig):
    recorder, listener, windows, clock = rig

    def refuse(callback):
        raise OSError("XRecord unavailable")
    listener.start = refuse
    with pytest.raises(RecorderError, match="XRecord unavailable"):
        recorder.start()
    assert windows._watchers == []


def test_stop_is_idempotent_and_unwatches(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    assert len(windows._watchers) == 1
    recorder.stop()
    recorder.stop()
    assert windows._watchers == []


def test_listener_thread_and_poller_interleave_safely(rig):
    recorder, listener, windows, clock = rig
    recorder.start()
    n = 2000

    def produce():
        for i in range(n):
            listener.feed(ev("motion", i * 0.001, x=200 + i % 50, y=200))
    thread = threading.Thread(target=produce)
    thread.start()
    got = []
    cursor = 1
    while thread.is_alive() or cursor < n + 1:
        events, cursor = recorder.events_since(cursor)
        got.extend(events)
    thread.join()
    assert len(got) == n
    assert [e.time for e in got] == sorted(e.time for e in got)
