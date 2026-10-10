"""Tk-free parts of the Settings dialog and of key capture."""

from __future__ import annotations

import pytest

from macro_recorder.gui.keycapture import key_from_event
from macro_recorder.gui.settings import (
    RECORDING_NUMBERS, WINDOW_NUMBERS, Settings, apply_settings, copy_settings,
    parse_hotkey, parse_hotkeys, parse_number_setting,
)

SHIFT, CONTROL, ALT, SUPER = 0x1, 0x4, 0x8, 0x40


@pytest.mark.parametrize("keysym, state, single, expected", [
    ("a", 0, True, "a"),
    ("Control_L", CONTROL, True, "Control_L"),
    ("A", SHIFT, True, "A"),
    ("Control_L", 0, False, None),
    ("S", CONTROL | SHIFT, False, "ctrl+shift+s"),
    ("F9", CONTROL | ALT | SUPER, False, "ctrl+alt+super+F9"),
    ("exclam", SHIFT, False, "shift+exclam"),
    ("Return", 0, False, "Return"),
    ("??", 0, False, None),
    ("", 0, True, None),
])
def test_key_from_event(keysym, state, single, expected):
    assert key_from_event(keysym, state, single) == expected


def test_number_settings_are_whole_numbers_in_range():
    gap = next(s for s in RECORDING_NUMBERS if s.key == "click_gap_ms")
    assert parse_number_setting(" 300 ", gap) == 300
    for text in ("0", "5001", "1.5", "-3", "", "fast"):
        with pytest.raises(ValueError, match="Click gap"):
            parse_number_setting(text, gap)
    timeout = next(s for s in WINDOW_NUMBERS if s.key == "window_timeout_ms")
    assert parse_number_setting("0", timeout) == 0


def test_parse_hotkey():
    assert parse_hotkey("Ctrl+Shift+F9") == "ctrl+shift+F9"
    assert parse_hotkey("ctrl+alt+R") == "ctrl+alt+r"
    for text, problem in (("", "Empty"), ("ctrl+alt+F2", "reserved"), ("shift", "modifier"),
                          ("ctrl+nosuchkey", "Unknown key"), ("a+b", "Only modifiers")):
        with pytest.raises(ValueError, match=problem):
            parse_hotkey(text)


def test_parse_hotkeys_rejects_the_same_combo_twice():
    combos, problems = parse_hotkeys({"record": "ctrl+shift+F9", "play": "Shift+Ctrl+F9"})
    assert problems == {"play": "Record and Play need different keys"}
    combos, problems = parse_hotkeys({"record": "F8", "play": "ctrl+alt+Delete"})
    assert combos == {"record": "F8"} and "Play or continue" in problems["play"]


def test_apply_settings_copies_only_the_dialog_sections():
    target = Settings()
    target.ui.recent = ["/a.json"]
    source = copy_settings(target)
    assert source == target and source.recording is not target.recording
    source.recording.min_wait_ms = 7
    source.playback.window_settle_ms = 9
    source.playback.speed = 3.0
    source.hotkeys.play = "F7"
    source.ui.recent = []
    recording = target.recording
    apply_settings(target, source)
    assert target.recording is recording and recording.min_wait_ms == 7
    assert target.playback.window_settle_ms == 9 and target.playback.speed == 1.0
    assert target.hotkeys.play == "F7" and target.ui.recent == ["/a.json"]
