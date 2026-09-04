"""Tests for the OCR screen-read action (read_region is mocked — no real screen)."""

import unittest
from unittest import mock

from macro_recorder.event_types import EventType
from macro_recorder.macro import MacroEvent, MacroGroup
from macro_recorder.player import MacroExecutionError, Player
from tests.playback_test_utils import start_player_io_patches, stop_player_io_patches


def setUpModule():
    start_player_io_patches()   # playback must not drive the real mouse/keyboard


def tearDownModule():
    stop_player_io_patches()


def _ocr(var, x, y, dx, dy, ts=0.0):
    return MacroEvent(type=EventType.OCR_READ, ts=ts, var_name=var, x=x, y=y, dx=dx, dy=dy)


def _play(events):
    player = Player(repeat=1)
    player.play([MacroGroup(window=None, recorded_rect=None, events=events)])
    return player


class TestOcrAction(unittest.TestCase):
    def test_assigns_ocr_text_to_variable(self):
        with mock.patch("macro_recorder.player.read_region", return_value="captured text") as rr:
            player = _play([_ocr("caption", 10, 20, 110, 70)])
        self.assertEqual(player._variables["caption"], "captured text")
        rr.assert_called_once_with(10, 20, 100, 50)   # normalized left, top, width, height

    def test_region_normalized_regardless_of_corner_order(self):
        with mock.patch("macro_recorder.player.read_region", return_value="x") as rr:
            _play([_ocr("v", 110, 70, 10, 20)])        # bottom-right given first
        rr.assert_called_once_with(10, 20, 100, 50)

    def test_pipeline_ocr_then_type(self):
        events = [
            _ocr("name", 0, 0, 50, 20, ts=0.0),
            MacroEvent(type=EventType.TYPE_TEXT, ts=0.1, expr="Hello {name}"),
        ]
        player = Player(repeat=1)
        player._kb_ctrl.type.reset_mock()   # controller mock is shared module-wide
        with mock.patch("macro_recorder.player.read_region", return_value="Sam"):
            player.play([MacroGroup(window=None, recorded_rect=None, events=events)])
        player._kb_ctrl.type.assert_called_with("Hello Sam")

    def test_invalid_variable_name_aborts(self):
        with mock.patch("macro_recorder.player.read_region", return_value="x"):
            with self.assertRaises(MacroExecutionError):
                _play([_ocr("1bad", 0, 0, 10, 10)])

    def test_empty_region_aborts(self):
        with mock.patch("macro_recorder.player.read_region", return_value="x"):
            with self.assertRaises(MacroExecutionError):
                _play([_ocr("v", 50, 50, 50, 50)])     # zero-size region

    def test_ocr_failure_aborts(self):
        from macro_recorder.ocr import OcrError
        with mock.patch("macro_recorder.player.read_region", side_effect=OcrError("no tesseract")):
            with self.assertRaises(MacroExecutionError):
                _play([_ocr("v", 0, 0, 10, 10)])


if __name__ == "__main__":
    unittest.main()
