"""Tests for the read-only dynamic playback values exposed to expressions."""

import unittest

from src.event_types import EventType
from src.macro import MacroEvent, MacroGroup
from src.player import DYNAMIC_VALUES, Player
from tests.playback_test_utils import start_player_io_patches, stop_player_io_patches


def setUpModule():
    start_player_io_patches()   # playback must not drive the real mouse/keyboard


def tearDownModule():
    stop_player_io_patches()


def _var(name, expr, ts):
    return MacroEvent(type=EventType.VAR_SET, ts=ts, var_name=name, expr=expr)


def _play(events, repeat=1):
    player = Player(repeat=repeat)
    player.play([MacroGroup(window=None, recorded_rect=None, events=events)])
    return player._variables


class TestDynamicValues(unittest.TestCase):
    def test_ex_num_counts_run_instructions(self):
        # Three Set Variable instructions; the third reads the running count.
        variables = _play([
            _var("a", "1", 0.0),
            _var("b", "2", 0.1),
            _var("n", "ex_num", 0.2),
        ])
        # By the time the third instruction evaluates, three have been reached.
        self.assertEqual(variables["n"], 3)

    def test_iteration_reflects_repeat_pass(self):
        # Capturing iteration on each of two passes ends on the 2nd pass value.
        variables = _play([_var("p", "iteration", 0.0)], repeat=2)
        self.assertEqual(variables["p"], 2)

    def test_rt_ms_is_available_and_numeric(self):
        variables = _play([
            MacroEvent(type=EventType.WAIT, ts=0.0, duration=0.02),
            _var("t", "rt_ms", 0.02),
        ])
        self.assertIsInstance(variables["t"], float)
        self.assertGreaterEqual(variables["t"], 0.0)

    def test_dynamic_value_usable_in_condition(self):
        # op_num drives a conditional jump just like a user variable.
        from src.player import GOTO_END
        variables = _play([
            _var("hit", "0", 0.0),                                   # #1
            MacroEvent(type=EventType.GOTO_IF, ts=0.1,
                       expr="op_num == 2", target=GOTO_END),         # #2 → end
            _var("hit", "1", 0.2),                                   # unreachable
        ])
        self.assertEqual(variables["hit"], 0)

    def test_assigning_to_dynamic_name_aborts(self):
        # Dynamic names are reserved — assigning to one halts playback.
        from src.player import MacroExecutionError
        with self.assertRaises(MacroExecutionError):
            _play([_var("iteration", "42", 0.0)])

    def test_undefined_name_still_raises(self):
        # A non-dynamic, unset variable remains an error (strict model intact).
        from src.player import MacroExecutionError
        with self.assertRaises(MacroExecutionError):
            _play([_var("x", "not_a_real_value", 0.0)])

    def test_all_advertised_values_resolve(self):
        # Every name in DYNAMIC_VALUES is readable during playback.
        names = [name for name, _desc in DYNAMIC_VALUES]
        events = [_var(f"v{i}", name, 0.1 * i) for i, name in enumerate(names)]
        player = Player(repeat=1)
        player._mouse_ctrl.position = (5, 7)   # mouse_x / mouse_y read this
        player.play([MacroGroup(window=None, recorded_rect=None, events=events)])
        for i, _name in enumerate(names):
            self.assertIn(f"v{i}", player._variables)
        self.assertEqual(player._variables[f"v{names.index('mouse_x')}"], 5.0)


if __name__ == "__main__":
    unittest.main()
