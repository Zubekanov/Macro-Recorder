"""Tests for the per-instruction playback log emitted via the on_log callback."""

import unittest

from src.event_types import EventType
from src.macro import MacroEvent, MacroGroup
from src.player import GOTO_END, Player
from src.table_model import TableModel
from tests.playback_test_utils import start_player_io_patches, stop_player_io_patches


def setUpModule():
    start_player_io_patches()


def tearDownModule():
    stop_player_io_patches()


def _var(name, expr, ts):
    return MacroEvent(type=EventType.VAR_SET, ts=ts, var_name=name, expr=expr)


def _play(events, **kw):
    logs: list[str] = []
    player = Player(repeat=1, on_log=logs.append, **kw)
    player.play([MacroGroup(window=None, recorded_rect=None, events=events)])
    return logs


class TestPlaybackLog(unittest.TestCase):
    def test_var_set_line_format(self):
        logs = _play([
            _var("y", "10", 0.0),
            _var("z", "3", 0.1),
            _var("x", "y + z", 0.2),
        ])
        # [position] description, with the assigned value + source expression.
        self.assertEqual(logs[0], "[1] Assigned y = 10  [10]")
        self.assertEqual(logs[2], "[3] Assigned x = 13  [y + z]")

    def test_goto_and_conditional_are_logged(self):
        logs = _play([
            _var("x", "0", 0.0),
            MacroEvent(type=EventType.GOTO_IF, ts=0.1, expr="x == 0", target=GOTO_END),
            _var("x", "9", 0.2),
        ])
        self.assertEqual(logs[0], "[1] Assigned x = 0  [0]")
        self.assertEqual(logs[1], "[2] If (x == 0) -> goto End of program")
        self.assertEqual(len(logs), 2)   # jumped to end; third row never ran

    def test_position_repeats_across_loop(self):
        # #1 x=0 ; #2 x=x+1 ; #3 if x<2 goto #2  → loops the body twice.
        # Built via the model so rows carry instruction-number markers.
        model = TableModel()
        model.row_events = {
            "r1": [_var("x", "0", 0.0)],
            "r2": [_var("x", "x + 1", 0.1)],
            "r3": [MacroEvent(type=EventType.GOTO_IF, ts=0.2, expr="x < 2", target_index=2)],
        }
        order = ["r1", "r2", "r3"]
        groups = model.rows_to_groups(order, {i: None for i in order})
        logs: list[str] = []
        Player(repeat=1, on_log=logs.append).play(groups)
        # Position revisits row 2 as the loop runs the body a second time.
        self.assertEqual(logs.count("[2] Assigned x = 1  [x + 1]"), 1)
        self.assertEqual(logs.count("[2] Assigned x = 2  [x + 1]"), 1)

    def test_bundled_row_logs_once(self):
        # A row bundling two mouse moves should emit a single log line.
        model = TableModel()
        model.row_events = {
            "r1": [MacroEvent(type=EventType.MOUSE_MOVE, ts=0.0, x=1, y=1),
                   MacroEvent(type=EventType.MOUSE_MOVE, ts=0.05, x=2, y=2)],
            "r2": [_var("x", "1", 0.1)],
        }
        order = ["r1", "r2"]
        groups = model.rows_to_groups(order, {i: None for i in order})
        logs: list[str] = []
        Player(repeat=1, on_log=logs.append).play(groups)
        self.assertEqual(logs[0], "[1] Move to (1, 1)")
        self.assertEqual(logs[1], "[2] Assigned x = 1  [1]")
        self.assertEqual(len(logs), 2)

    def test_no_callback_is_harmless(self):
        # Playing without on_log must not error.
        Player(repeat=1).play(
            [MacroGroup(window=None, recorded_rect=None, events=[_var("x", "1", 0.0)])])


if __name__ == "__main__":
    unittest.main()
