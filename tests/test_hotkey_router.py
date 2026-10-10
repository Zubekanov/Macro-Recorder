"""HotkeyRouter: grabs from settings, routing by mode, stop keys, failures."""

from enum import Enum

import pytest

from macro_recorder.gui.hotkeys import HotkeyActions, HotkeyRouter, mode_name
from macro_recorder.gui.settings import Settings
from tests.conftest import FakeHotkeys, FakeStopKey

RECORD = "ctrl+shift+F9"
PLAY = "ctrl+shift+F5"


class Mode(Enum):
    IDLE = "idle"
    PAUSED = "paused"


class Harness:
    def __init__(self, refuse=None, blocked=False):
        self.settings = Settings()
        self.grabber = FakeHotkeys(refuse=refuse)
        self.stop_key = FakeStopKey()
        self.mode = "idle"
        self.blocked = blocked
        self.calls = []
        actions = HotkeyActions(
            start_record=lambda: self.calls.append("start_record"),
            stop_record=lambda trigger: self.calls.append(("stop_record", trigger)),
            play=lambda: self.calls.append("play"),
            resume=lambda: self.calls.append("resume"),
            stop_play=lambda: self.calls.append("stop_play"),
        )
        self.router = HotkeyRouter(self.grabber, self.stop_key, self.settings,
                                   lambda: self.mode, actions, is_blocked=lambda: self.blocked)

    def enter(self, mode):
        self.mode = mode
        self.router.mode_changed(mode)


@pytest.fixture
def h():
    harness = Harness()
    assert harness.router.start() == {}
    return harness


def test_start_grabs_both_combos_from_settings(h):
    assert set(h.grabber.grabbed) == {RECORD, PLAY}
    assert h.router.failures == {}


@pytest.mark.parametrize("mode, combo, expected", [
    ("idle", RECORD, ["start_record"]),
    ("idle", PLAY, ["play"]),
    ("recording", RECORD, [("stop_record", "hotkey")]),
    ("recording", PLAY, []),
    ("playing", PLAY, []),
    ("playing", RECORD, []),
    ("paused", PLAY, ["resume"]),
    ("paused", RECORD, []),
    ("targeting", RECORD, []),
    ("targeting", PLAY, []),
])
def test_combos_route_by_mode(h, mode, combo, expected):
    h.mode = mode
    h.grabber.press(combo)
    assert h.calls == expected


def test_enum_modes_are_accepted(h):
    h.mode = Mode.PAUSED
    h.grabber.press(PLAY)
    h.mode = Mode.IDLE
    h.grabber.press(PLAY)
    assert h.calls == ["resume", "play"]
    assert mode_name(Mode.IDLE) == "idle"


def test_everything_is_ignored_while_a_modal_grab_is_active(h):
    h.blocked = True
    h.grabber.press(RECORD)
    h.grabber.press(PLAY)
    h.enter("playing")
    h.stop_key.press()
    assert h.calls == []


def test_stop_key_follows_the_mode(h):
    assert h.stop_key.key is None
    h.enter("recording")
    assert h.stop_key.key == "F6"
    h.stop_key.press()
    assert h.calls == [("stop_record", "stop_key")]
    h.enter("playing")
    assert h.stop_key.key == "Escape"
    h.stop_key.press()
    h.enter("paused")
    assert h.stop_key.key == "Escape"
    h.stop_key.press()
    assert h.calls[1:] == ["stop_play", "stop_play"]
    h.enter("idle")
    assert h.stop_key.key is None
    h.enter("targeting")
    assert h.stop_key.key is None


def test_stop_key_rechecks_the_mode_when_pressed(h):
    h.enter("playing")
    h.mode = "idle"              # finished before the shell reported it
    h.stop_key.press()
    assert h.calls == []


def test_stop_key_failure_is_logged_not_raised(h, caplog):
    class Broken(FakeStopKey):
        def start(self, key, callback):
            raise OSError("no XInput")

    h.router._stop_watcher = Broken()
    h.enter("playing")
    assert "Stop key Escape unavailable" in caplog.text


def test_refused_grab_is_reported():
    h = Harness(refuse={PLAY})
    failures = h.router.start()
    assert set(failures) == {"play"}
    assert "Ctrl+Shift+F5 is in use" in failures["play"]
    assert set(h.grabber.grabbed) == {RECORD}


def test_reserved_combo_is_rejected_without_grabbing():
    h = Harness()
    h.settings.hotkeys.record = "ctrl+alt+F2"
    h.settings.hotkeys.play = "ctrl+alt+Delete"
    failures = h.router.start()
    assert set(failures) == {"record", "play"}
    assert all("reserved" in m for m in failures.values())
    assert h.grabber.grabbed == {}


def test_unparseable_combo_is_reported():
    h = Harness()
    h.settings.hotkeys.record = "a+ctrl"
    assert "precede" in h.router.start()["record"]


def test_check_reports_the_reject_rules():
    assert HotkeyRouter.check("ctrl+alt+F1")
    assert HotkeyRouter.check("ctrl+alt+BackSpace")
    assert HotkeyRouter.check("ctrl+shift+F9") is None


def test_start_again_rebinds_after_a_settings_change(h):
    h.settings.hotkeys.record = "ctrl+shift+r"
    assert h.router.start() == {}
    assert set(h.grabber.grabbed) == {"ctrl+shift+r", PLAY}
    h.grabber.press("ctrl+shift+r")
    assert h.calls == ["start_record"]


def test_suspend_and_resume(h):
    h.router.suspend()
    h.grabber.press(PLAY)
    assert h.calls == [] and h.grabber.suspended
    h.router.resume()
    h.grabber.press(PLAY)
    assert h.calls == ["play"]


def test_stop_releases_grabs_and_stop_key(h):
    h.enter("recording")
    h.router.stop()
    assert h.grabber.grabbed == {} and h.stop_key.key is None
