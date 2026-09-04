"""Tests for goto / conditional-goto playback and runtime value resolution."""

import unittest

from src.event_types import EventType
from src.macro import MacroEvent, MacroGroup
from src.player import GOTO_END, GOTO_START, Player
from tests.playback_test_utils import start_player_io_patches, stop_player_io_patches


def setUpModule():
    start_player_io_patches()   # playback must not drive the real mouse/keyboard


def tearDownModule():
    stop_player_io_patches()


def _var(name, expr, ts, label=None):
    return MacroEvent(type=EventType.VAR_SET, ts=ts, var_name=name, expr=expr, label=label)


def _goto(target, ts, label=None):
    return MacroEvent(type=EventType.GOTO, ts=ts, target=target, label=label)


def _goto_if(expr, target, ts, label=None):
    return MacroEvent(type=EventType.GOTO_IF, ts=ts, expr=expr, target=target, label=label)


def _goto_idx(index, ts, label=None):
    return MacroEvent(type=EventType.GOTO, ts=ts, target_index=index, label=label)


def _goto_if_idx(expr, index, ts):
    return MacroEvent(type=EventType.GOTO_IF, ts=ts, expr=expr, target_index=index)


def _play(events, repeat=1):
    player = Player(repeat=repeat)
    player.play([MacroGroup(window=None, recorded_rect=None, events=events)])
    return player._variables


class TestControlFlow(unittest.TestCase):
    def test_conditional_loop_increments_until_done(self):
        # x = 0 ; [loop] x = x + 1 ; if x < 3 goto loop
        variables = _play([
            _var("x", "0", 0.0),
            _var("x", "x + 1", 0.1, label="loop"),
            _goto_if("x < 3", "loop", 0.2),
        ])
        self.assertEqual(variables["x"], 3)

    def test_goto_end_skips_remaining(self):
        variables = _play([
            _var("x", "1", 0.0),
            _goto(GOTO_END, 0.1),
            _var("x", "99", 0.2),  # unreachable
        ])
        self.assertEqual(variables["x"], 1)

    def test_conditional_false_does_not_jump(self):
        variables = _play([
            _var("x", "0", 0.0),
            _goto_if("x > 5", GOTO_END, 0.1),  # false → fall through
            _var("x", "7", 0.2),
        ])
        self.assertEqual(variables["x"], 7)

    def test_unknown_label_continues(self):
        variables = _play([
            _var("x", "5", 0.0),
            _goto("does_not_exist", 0.1),  # unknown → continue, no jump
            _var("x", "9", 0.2),
        ])
        self.assertEqual(variables["x"], 9)

    def test_goto_by_instruction_number_loops(self):
        # Without table markers, an instruction number is a 1-based flat index.
        #   #1 x = 0 ; #2 x = x + 1 ; #3 if x < 3 goto #2
        variables = _play([
            _var("x", "0", 0.0),
            _var("x", "x + 1", 0.1),
            _goto_if_idx("x < 3", 2, 0.2),
        ])
        self.assertEqual(variables["x"], 3)

    def test_goto_instruction_out_of_range_continues(self):
        variables = _play([
            _var("x", "5", 0.0),
            _goto_idx(99, 0.1),     # no such instruction → continue, no jump
            _var("x", "9", 0.2),
        ])
        self.assertEqual(variables["x"], 9)

    def test_goto_by_instruction_number_respects_row_markers(self):
        # When events carry table row markers (set by TableModel.rows_to_groups),
        # an instruction number resolves to that row's first event even though an
        # earlier row bundles several events into one instruction.
        from src.table_model import TableModel
        model = TableModel()
        model.row_events = {
            "r1": [_var("x", "0", 0.0)],                                   # #1
            "r2": [MacroEvent(type=EventType.MOUSE_MOVE, ts=0.1, x=1, y=1),
                   MacroEvent(type=EventType.MOUSE_MOVE, ts=0.11, x=2, y=2)],  # #2 (bundle)
            "r3": [_var("x", "x + 1", 0.2)],                              # #3
            "r4": [_goto_if_idx("x < 3", 3, 0.3)],                        # #4 → #3
        }
        order = ["r1", "r2", "r3", "r4"]
        groups = model.rows_to_groups(order, {i: None for i in order})
        player = Player(repeat=1)
        player.play(groups)
        self.assertEqual(player._variables["x"], 3)

    def test_goto_start_loops_to_top(self):
        # Start = index 0.  This loops forever; stop it after a few reports and
        # confirm execution kept returning to the top (n is re-set each pass).
        events = [
            _var("n", "0", 0.0),       # index 0 = Start
            _goto(GOTO_START, 0.1),    # jump back to index 0
        ]
        player = Player(repeat=1)
        count = [0]

        def stop_after(ev):
            count[0] += 1
            if count[0] >= 4:
                player.stop()
        player._on_active_event = stop_after
        player.play([MacroGroup(window=None, recorded_rect=None, events=events)])

        self.assertGreaterEqual(count[0], 4)   # looped back to the top repeatedly
        self.assertEqual(player._variables["n"], 0)


class TestRuntimeResolution(unittest.TestCase):
    def test_resolve_expression_field(self):
        p = Player()
        p._variables = {"x": 10}
        self.assertEqual(p._resolve("x + 5"), 15)

    def test_resolve_plain_number(self):
        self.assertEqual(Player()._resolve(100), 100)

    def test_resolve_none_uses_default(self):
        self.assertEqual(Player()._resolve(None, default=7), 7)

    def test_resolve_bad_expression_aborts(self):
        from src.player import MacroExecutionError
        with self.assertRaises(MacroExecutionError):
            Player()._resolve("1 +")

    def test_resolve_uninitialised_variable_aborts(self):
        from src.player import MacroExecutionError
        with self.assertRaises(MacroExecutionError):
            Player()._resolve("missing + 1")


if __name__ == "__main__":
    unittest.main()
