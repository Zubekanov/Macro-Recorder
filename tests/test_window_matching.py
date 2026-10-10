"""Window title matching, the relaunch fallback, and the fields that carry them."""

import unittest
from unittest import mock

from macro_recorder.event_types import EventType
from macro_recorder.macro import MacroEvent, MacroGroup, WindowRect
from macro_recorder.player import MacroExecutionError, Player, WindowNotFoundError
from macro_recorder.table_model import TableModel
from macro_recorder.window_manager import (
    MATCH_REGEX,
    MATCH_SUBSTRING,
    WindowInfo,
    select_window,
    title_matches,
)
from tests.playback_test_utils import start_player_io_patches, stop_player_io_patches


def setUpModule():
    start_player_io_patches()


def tearDownModule():
    stop_player_io_patches()


def win(title, hwnd=1):
    return WindowInfo(title=title, left=0, top=0, width=100, height=100, hwnd=hwnd)


class TestTitleMatches(unittest.TestCase):
    def test_substring_is_case_insensitive(self):
        self.assertTrue(title_matches("report.txt - Notepad", "notepad"))
        self.assertTrue(title_matches("report.txt - Notepad", "REPORT"))
        self.assertFalse(title_matches("report.txt - Notepad", "Word"))

    def test_regex_uses_search(self):
        self.assertTrue(title_matches("report.txt - Notepad", r".*\.txt - Notepad$", MATCH_REGEX))
        self.assertFalse(title_matches("report.txt - Notepad", r"^Notepad", MATCH_REGEX))

    def test_invalid_regex_raises_value_error(self):
        with self.assertRaises(ValueError):
            title_matches("x", "(", MATCH_REGEX)


class TestSelectWindow(unittest.TestCase):
    def test_exact_title_beats_partial_match(self):
        windows = [win("Untitled - Notepad", 1), win("Notepad", 2), win("Notepad++", 3)]
        self.assertEqual(select_window(windows, "notepad").hwnd, 2)

    def test_first_partial_match_when_no_exact(self):
        windows = [win("Calculator", 1), win("a - Notepad", 2), win("b - Notepad", 3)]
        self.assertEqual(select_window(windows, "Notepad").hwnd, 2)

    def test_regex_mode(self):
        windows = [win("Calculator", 1), win("Notepad v2", 2)]
        self.assertEqual(select_window(windows, r"v\d", MATCH_REGEX).hwnd, 2)

    def test_no_match_returns_none(self):
        self.assertIsNone(select_window([win("Calculator")], "Notepad"))


class TestGroupFields(unittest.TestCase):
    def test_defaults_are_omitted_from_dict(self):
        g = MacroGroup(window="App", recorded_rect=None, events=[])
        self.assertNotIn("match_mode", g.to_dict())
        self.assertNotIn("launch", g.to_dict())

    def test_round_trip(self):
        g = MacroGroup(window=r"App v\d", recorded_rect=None, events=[],
                       match_mode=MATCH_REGEX, launch="notepad.exe")
        back = MacroGroup.from_dict(g.to_dict())
        self.assertEqual(back.match_mode, MATCH_REGEX)
        self.assertEqual(back.launch, "notepad.exe")

    def test_old_files_load_with_defaults(self):
        back = MacroGroup.from_dict({"window": "App", "recorded_rect": None, "events": []})
        self.assertEqual(back.match_mode, MATCH_SUBSTRING)
        self.assertIsNone(back.launch)

    def test_table_round_trip_keeps_fields(self):
        g = MacroGroup(window="App", recorded_rect=WindowRect(0, 0, 10, 10),
                       events=[MacroEvent(type=EventType.KEY_PRESS, ts=0.0, key="a")],
                       match_mode=MATCH_REGEX, launch="app.exe")
        model = TableModel()
        flat = model.groups_to_flat_events([g])
        self.assertEqual(flat[0].type, EventType.WINDOW_FOCUS)
        self.assertEqual((flat[0].match_mode, flat[0].launch), (MATCH_REGEX, "app.exe"))
        model.row_events = {"r0": [flat[0]], "r1": [flat[1]]}
        groups = model.rows_to_groups(["r0", "r1"], {"r0": None, "r1": None})
        self.assertEqual((groups[0].match_mode, groups[0].launch), (MATCH_REGEX, "app.exe"))


class _StubWindowManager:
    """Reports no window until ``present`` is set; records every query."""

    def __init__(self):
        self.present = False
        self.queries = []
        self.focused = []

    def find_window(self, pattern, mode=MATCH_SUBSTRING):
        self.queries.append((pattern, mode))
        return win("App") if self.present else None

    def set_foreground(self, window):
        self.focused.append(window.title)
        return True

    def move_resize(self, window, left, top, width, height):
        return True

    def get_foreground(self):
        return None


class TestRelaunch(unittest.TestCase):
    def _group(self, launch=None, mode=MATCH_SUBSTRING):
        return MacroGroup(window="App", recorded_rect=WindowRect(0, 0, 10, 10),
                          events=[MacroEvent(type=EventType.KEY_PRESS, ts=0.0, key="a")],
                          match_mode=mode, launch=launch)

    def _player(self, wm, **kw):
        player = Player(window_timeout=0.0, **kw)
        player._wm = wm
        return player

    def test_launches_once_then_plays_when_window_appears(self):
        wm = _StubWindowManager()
        seen = []
        player = self._player(wm, on_active_event=seen.append)

        def popen(cmd, shell):
            wm.present = True
            return mock.Mock()

        with mock.patch("macro_recorder.player.subprocess.Popen", side_effect=popen) as p:
            player.play([self._group(launch="app.exe")])
        p.assert_called_once_with("app.exe", shell=True)
        self.assertEqual(wm.focused, ["App"])
        self.assertEqual(len(seen), 1)

    def test_no_launch_command_means_no_popen(self):
        wm = _StubWindowManager()
        player = self._player(wm)
        with mock.patch("macro_recorder.player.subprocess.Popen") as p:
            player.play([self._group()])
        p.assert_not_called()

    def test_still_missing_after_launch_halts_when_configured(self):
        wm = _StubWindowManager()
        player = self._player(wm, on_missing_window="halt")
        with mock.patch("macro_recorder.player.subprocess.Popen") as p, \
                self.assertRaises(WindowNotFoundError):
            player.play([self._group(launch="app.exe")])
        p.assert_called_once()

    def test_launch_happens_once_per_play_across_repeats(self):
        wm = _StubWindowManager()
        player = self._player(wm, repeat=3)
        with mock.patch("macro_recorder.player.subprocess.Popen") as p:
            player.play([self._group(launch="app.exe")])
        self.assertEqual(p.call_count, 1)

    def test_match_mode_is_passed_to_window_manager(self):
        wm = _StubWindowManager()
        wm.present = True
        player = self._player(wm)
        player.play([self._group(mode=MATCH_REGEX)])
        self.assertEqual(wm.queries[0], ("App", MATCH_REGEX))

    def test_invalid_regex_aborts_playback(self):
        class BadPattern(_StubWindowManager):
            def find_window(self, pattern, mode=MATCH_SUBSTRING):
                raise ValueError("bad pattern")
        player = self._player(BadPattern())
        with self.assertRaises(MacroExecutionError):
            player.play([self._group(mode=MATCH_REGEX)])


if __name__ == "__main__":
    unittest.main()
