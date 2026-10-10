"""RecordingController: provisional rows, insertion at stop, errors."""

from __future__ import annotations

import pytest

from macro_recorder.engine.recorder import Recorder
from macro_recorder.gui.recording import RecordingCallbacks, RecordingController
from macro_recorder.gui.settings import Settings
from macro_recorder.model.catalogue import make
from macro_recorder.model.document import MacroDocument, make_row
from macro_recorder.model.protocols import RawEvent
from macro_recorder.platform.session import OwnWindows
from tests.conftest import FakeListener, FakeWindows, window

XED = window(0x100, "xed", 100, 80, 900, 600)


class Clock:
    def __init__(self) -> None:
        self.t = 50.0

    def __call__(self) -> float:
        return self.t


class Shell:
    """Records what the controller asks of the GUI."""

    def __init__(self) -> None:
        self.provisional: list[list] = []
        self.cleared = 0
        self.modes: list[str] = []
        self.errors: list[str] = []

    def callbacks(self) -> RecordingCallbacks:
        return RecordingCallbacks(
            show_provisional=lambda rows: self.provisional.append(list(rows)),
            clear_provisional=self._clear,
            set_mode=self.modes.append,
            report_error=self.errors.append,
        )

    def _clear(self) -> None:
        self.cleared += 1


class Rig:
    def __init__(self, rows=()) -> None:
        self.document = MacroDocument(rows)
        self.listener = FakeListener()
        self.windows = FakeWindows([XED])
        self.clock = Clock()
        self.shell = Shell()
        self.recorders: list[Recorder] = []
        self.controller = RecordingController(self.document, self._make_recorder, Settings(),
                                              self.shell.callbacks())

    def _make_recorder(self) -> Recorder:
        r = Recorder(self.listener, self.windows, OwnWindows(), clock=self.clock)
        self.recorders.append(r)
        return r

    def feed(self, *events: RawEvent) -> None:
        self.listener.feed(*events)
        self.clock.t = max(self.clock.t, max(e.time for e in events))

    def ev(self, kind, t, **kw) -> RawEvent:
        return RawEvent(kind, 50.0 + t, **kw)

    def tap(self, t, key):
        self.feed(self.ev("key_press", t, key=key), self.ev("key_release", t + 0.05, key=key))


def wait_row(ms):
    return make_row(make("wait", duration_ms=ms))


def types(rows):
    return [r.type for r in rows]


def test_stop_inserts_after_the_given_row_as_one_undo_step():
    rig = Rig([wait_row(1), wait_row(2), wait_row(3)])
    first = rig.document.rows[0].id
    before = rig.document.rows
    assert rig.controller.start(first)
    assert rig.shell.modes == ["recording"]
    rig.tap(0.5, "a")
    rig.tap(1.0, "b")
    ids = rig.controller.stop()
    rows = rig.document.rows
    assert types(rows) == ["wait", "window_focus", "wait", "key_combo", "wait", "key_combo", "wait", "wait"]
    assert [r.id for r in rows[1:6]] == list(ids)
    assert rows[0].id == first and rows[6:] == before[1:]
    assert rig.shell.cleared == 1 and rig.shell.modes[-1] == "idle"
    assert rig.document.undo()
    assert rig.document.rows == before
    assert not rig.document.can_undo


def test_no_anchor_appends_at_the_end():
    rig = Rig([wait_row(1)])
    rig.controller.start(None)
    rig.tap(0.05, "a")
    rig.controller.stop()
    assert types(rig.document.rows) == ["wait", "window_focus", "key_combo"]


def test_deleted_anchor_appends_at_the_end():
    rig = Rig([wait_row(1), wait_row(2)])
    anchor = rig.document.rows[0].id
    rig.controller.start(anchor)
    rig.document.delete([anchor])
    rig.tap(0.05, "a")
    rig.controller.stop()
    assert types(rig.document.rows) == ["wait", "window_focus", "key_combo"]


def test_empty_recording_inserts_nothing():
    rig = Rig([wait_row(1)])
    rig.windows.active_id = None
    rig.controller.start(None)
    assert rig.controller.stop() == ()
    assert not rig.document.can_undo
    assert rig.shell.modes == ["recording", "idle"]


def test_poll_shows_provisional_rows_incrementally():
    rig = Rig()
    rig.controller.start(None)
    rig.controller.poll()
    assert types(rig.shell.provisional[-1]) == ["window_focus"]
    rig.tap(0.05, "a")
    rig.controller.poll()
    first = rig.shell.provisional[-1]
    assert types(first) == ["window_focus", "key_combo"]
    shown = len(rig.shell.provisional)
    rig.controller.poll()                       # nothing new: no redraw
    assert len(rig.shell.provisional) == shown
    rig.tap(0.5, "b")
    rig.controller.poll()
    second = rig.shell.provisional[-1]
    assert types(second) == ["window_focus", "key_combo", "wait", "key_combo"]
    rig.tap(0.6, "c")
    rig.controller.poll()
    third = rig.shell.provisional[-1]
    assert third[0] is second[0]                # settled rows keep their identity
    assert rig.document.rows == ()              # provisional rows are not in the document


def test_provisional_click_appears_once_marker_window_passes():
    rig = Rig()
    rig.controller.start(None)
    rig.feed(rig.ev("button_press", 1.0, x=300, y=300, button="left"),
             rig.ev("button_release", 1.05, x=300, y=300, button="left"))
    rig.controller.poll()
    assert types(rig.shell.provisional[-1]) == ["window_focus"]
    rig.clock.t = 51.6
    rig.controller.poll()
    assert types(rig.shell.provisional[-1]) == ["window_focus", "wait", "mouse_click"]


def test_stop_key_ends_recording_on_next_poll():
    rig = Rig()
    rig.controller.start(None)
    rig.tap(0.05, "a")
    rig.feed(rig.ev("key_press", 0.2, key="F6"))
    rig.controller.poll()
    assert not rig.controller.recording
    assert types(rig.document.rows) == ["window_focus", "key_combo"]
    assert rig.shell.modes == ["recording", "idle"]


def test_listener_death_keeps_rows_and_reports_error():
    rig = Rig()
    rig.controller.start(None)
    rig.tap(0.05, "a")
    rig.listener.alive = False
    rig.controller.poll()
    assert not rig.controller.recording
    assert types(rig.document.rows) == ["window_focus", "key_combo"]
    assert rig.shell.errors == ["The input listener stopped unexpectedly"]
    assert rig.shell.modes == ["recording", "idle"]


def test_start_failure_reports_and_stays_idle():
    rig = Rig()

    def refuse(callback):
        raise OSError("no XRecord")
    rig.listener.start = refuse
    assert not rig.controller.start(None)
    assert not rig.controller.recording
    assert rig.shell.modes == []
    assert "no XRecord" in rig.shell.errors[0]


def test_second_start_while_recording_is_refused():
    rig = Rig()
    assert rig.controller.start(None)
    assert not rig.controller.start(None)
    assert len(rig.recorders) == 1


def test_stop_and_poll_when_idle_do_nothing():
    rig = Rig()
    assert rig.controller.stop() == ()
    rig.controller.poll()
    assert rig.shell.modes == [] and rig.shell.cleared == 0


@pytest.mark.parametrize("record_paths", [True, False])
def test_recording_settings_are_read_at_start(record_paths):
    rig = Rig()
    rig.controller._settings.recording.record_paths = record_paths
    rig.controller.start(None)
    rig.feed(rig.ev("motion", 0.05, x=10, y=10), rig.ev("motion", 0.07, x=40, y=10))
    rig.controller.stop()
    assert ("mouse_move" in types(rig.document.rows)) is record_paths
