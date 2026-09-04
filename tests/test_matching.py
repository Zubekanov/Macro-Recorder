"""Tests for the wait-for-match actions (find_image / match_text / monitor_region
are mocked — no real screen, cv2, mss, or OCR engine is touched)."""

import unittest
from unittest import mock

from src.event_types import EventType
from src.macro import MacroEvent, MacroGroup
from src.matching import MatchError, _similarity
from src.player import MacroExecutionError, Player
from tests.playback_test_utils import start_player_io_patches, stop_player_io_patches


def setUpModule():
    start_player_io_patches()   # playback must not drive the real mouse/keyboard


def tearDownModule():
    stop_player_io_patches()


def _img(var, *, x=0, y=0, dx=100, dy=100, image_path="ref.png", tolerance=0.9,
         duration=0.0, monitor=None, capture_var=None, capture_var_y=None, ts=0.0):
    return MacroEvent(type=EventType.MATCH_IMAGE, ts=ts, var_name=var, x=x, y=y,
                      dx=dx, dy=dy, image_path=image_path, tolerance=tolerance,
                      duration=duration, monitor=monitor, capture_var=capture_var,
                      capture_var_y=capture_var_y)


def _txt(var, *, expr="hello", x=0, y=0, dx=100, dy=100, tolerance=0.8,
         duration=0.0, monitor=None, capture_var=None, ts=0.0):
    return MacroEvent(type=EventType.MATCH_TEXT, ts=ts, var_name=var, expr=expr,
                      x=x, y=y, dx=dx, dy=dy, tolerance=tolerance,
                      duration=duration, monitor=monitor, capture_var=capture_var)


def _play(events):
    player = Player(repeat=1)
    player.play([MacroGroup(window=None, recorded_rect=None, events=events)])
    return player


class TestMatchImage(unittest.TestCase):
    def test_found_sets_flag_and_captures_coords(self):
        with mock.patch("src.player.find_image", return_value=(640, 480, 0.97)) as fi:
            player = _play([_img("found", capture_var="cx", capture_var_y="cy")])
        self.assertEqual(player._variables["found"], 1)
        self.assertEqual(player._variables["cx"], 640)
        self.assertEqual(player._variables["cy"], 480)
        fi.assert_called_with((0, 0, 100, 100), "ref.png", 0.9)

    def test_not_found_times_out_sets_zero(self):
        with mock.patch("src.player.find_image", return_value=None):
            player = _play([_img("found", duration=0.0, capture_var="cx")])
        self.assertEqual(player._variables["found"], 0)
        self.assertNotIn("cx", player._variables)   # nothing captured on timeout

    def test_monitor_region_used_when_set(self):
        with mock.patch("src.player.monitor_region", return_value=(0, 0, 1920, 1080)) as mr, \
             mock.patch("src.player.find_image", return_value=(10, 20, 0.99)) as fi:
            _play([_img("found", monitor=1)])
        mr.assert_called_once_with(1)
        fi.assert_called_with((0, 0, 1920, 1080), "ref.png", 0.9)

    def test_match_error_aborts(self):
        with mock.patch("src.player.find_image", side_effect=MatchError("no cv2")):
            with self.assertRaises(MacroExecutionError):
                _play([_img("found")])

    def test_invalid_variable_name_aborts(self):
        with mock.patch("src.player.find_image", return_value=(1, 2, 0.99)):
            with self.assertRaises(MacroExecutionError):
                _play([_img("1bad")])

    def test_empty_region_aborts(self):
        with mock.patch("src.player.find_image", return_value=None):
            with self.assertRaises(MacroExecutionError):
                _play([_img("found", x=50, y=50, dx=50, dy=50)])


class TestMatchText(unittest.TestCase):
    def test_found_sets_flag_and_captures_text(self):
        with mock.patch("src.player.match_text",
                        return_value=("Hello World", 0.95, True)) as mt:
            player = _play([_txt("found", expr="Hello World", capture_var="seen")])
        self.assertEqual(player._variables["found"], 1)
        self.assertEqual(player._variables["seen"], "Hello World")
        mt.assert_called_with((0, 0, 100, 100), "Hello World", 0.8)

    def test_not_matched_times_out_sets_zero_but_captures(self):
        with mock.patch("src.player.match_text",
                        return_value=("garbled", 0.1, False)):
            player = _play([_txt("found", capture_var="seen", duration=0.0)])
        self.assertEqual(player._variables["found"], 0)
        self.assertEqual(player._variables["seen"], "garbled")

    def test_expected_text_template_interpolated(self):
        events = [
            MacroEvent(type=EventType.VAR_SET, ts=0.0, var_name="who", expr='"Sam"'),
            _txt("found", expr="Hi {who}", ts=0.1),
        ]
        with mock.patch("src.player.match_text",
                        return_value=("Hi Sam", 1.0, True)) as mt:
            Player(repeat=1).play([MacroGroup(window=None, recorded_rect=None, events=events)])
        mt.assert_called_with((0, 0, 100, 100), "Hi Sam", 0.8)

    def test_ocr_error_aborts(self):
        from src.ocr import OcrError
        with mock.patch("src.player.match_text", side_effect=OcrError("no engine")):
            with self.assertRaises(MacroExecutionError):
                _play([_txt("found")])


class TestMatchPipeline(unittest.TestCase):
    def test_match_then_conditional_goto(self):
        """found-flag drives a Conditional Go To: skip a step when not found."""
        from src.player import GOTO_END
        events = [
            _txt("found", ts=0.0),
            MacroEvent(type=EventType.GOTO_IF, ts=0.1, expr="found == 0", target=GOTO_END),
            MacroEvent(type=EventType.VAR_SET, ts=0.2, var_name="ran", expr="1"),
        ]
        with mock.patch("src.player.match_text", return_value=("x", 0.0, False)):
            player = Player(repeat=1)
            player.play([MacroGroup(window=None, recorded_rect=None, events=events)])
        self.assertEqual(player._variables["found"], 0)
        self.assertNotIn("ran", player._variables)   # branch skipped the body


class TestSimilarity(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(_similarity("hello", "hello"), 1.0)

    def test_case_and_whitespace_normalized(self):
        self.assertEqual(_similarity("  Hello   World ", "hello world"), 1.0)

    def test_partial(self):
        ratio = _similarity("hello", "hallo")    # one substitution of five
        self.assertAlmostEqual(ratio, 0.8, places=5)

    def test_disjoint_low(self):
        self.assertLess(_similarity("abc", "xyz"), 0.5)

    def test_both_empty(self):
        self.assertEqual(_similarity("", ""), 1.0)

    def test_one_empty(self):
        self.assertEqual(_similarity("abc", ""), 0.0)


if __name__ == "__main__":
    unittest.main()
