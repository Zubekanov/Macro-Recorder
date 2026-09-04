"""End-to-end tests for variable actions during playback."""

import unittest

from src.event_types import EventType
from src.macro import MacroEvent, MacroGroup
from src.player import Player
from tests.playback_test_utils import start_player_io_patches, stop_player_io_patches


def setUpModule():
    start_player_io_patches()   # playback must not drive the real mouse/keyboard


def tearDownModule():
    stop_player_io_patches()


def _var(name, expr, ts):
    return MacroEvent(type=EventType.VAR_SET, ts=ts, var_name=name, expr=expr)


def _play(events, repeat=1):
    """Play a single group of var_set events and return the final variable store."""
    player = Player(repeat=repeat)
    player.play([MacroGroup(window=None, recorded_rect=None, events=events)])
    return player._variables


class TestVariablePlayback(unittest.TestCase):
    def test_initialise_and_modify(self):
        # x = 0 ; x = x + 3
        variables = _play([_var("x", "0", 0.0), _var("x", "x + 3", 0.1)])
        self.assertEqual(variables["x"], 3)

    def test_expression_between_variables(self):
        # a = 3 ; b = 4 ; c = a * b
        variables = _play([
            _var("a", "3", 0.0),
            _var("b", "4", 0.1),
            _var("c", "a * b", 0.2),
        ])
        self.assertEqual(variables["c"], 12)

    def test_repeat_reruns_program(self):
        # Each repeat re-runs the whole program, so an initialised counter
        # resets to its start value rather than accumulating across repeats.
        variables = _play([_var("x", "0", 0.0), _var("x", "x + 1", 0.1)], repeat=3)
        self.assertEqual(variables["x"], 1)

    def test_bad_expression_aborts_playback(self):
        from src.player import MacroExecutionError
        with self.assertRaises(MacroExecutionError):
            _play([_var("x", "1 +", 0.0)])

    def test_uninitialised_variable_aborts_playback(self):
        from src.player import MacroExecutionError
        with self.assertRaises(MacroExecutionError):
            _play([_var("x", "y + 1", 0.0)])   # y never initialised


class TestStringsAndTyping(unittest.TestCase):
    def test_string_variable_assignment(self):
        variables = _play([_var("s", '"hello"', 0.0)])
        self.assertEqual(variables["s"], "hello")

    def test_type_text_renders_template(self):
        events = [
            _var("name", '"Sam"', 0.0),
            MacroEvent(type=EventType.TYPE_TEXT, ts=0.1, expr="hi {name}!"),
        ]
        player = Player(repeat=1)
        player.play([MacroGroup(window=None, recorded_rect=None, events=events)])
        # Keyboard controller is mocked by the module patch; check what was typed.
        player._kb_ctrl.type.assert_called_with("hi Sam!")

    def test_type_text_undefined_var_aborts(self):
        from src.player import MacroExecutionError
        with self.assertRaises(MacroExecutionError):
            _play([MacroEvent(type=EventType.TYPE_TEXT, ts=0.0, expr="{missing}")])

    def test_type_text_instant_types_whole_string(self):
        events = [MacroEvent(type=EventType.TYPE_TEXT, ts=0.0, expr="abc")]
        player = Player(repeat=1)
        player._kb_ctrl.type.reset_mock()   # controller mock is shared module-wide
        player.play([MacroGroup(window=None, recorded_rect=None, events=events)])
        player._kb_ctrl.type.assert_called_once_with("abc")

    def test_type_text_per_char_delay_types_each_char(self):
        events = [MacroEvent(type=EventType.TYPE_TEXT, ts=0.0, expr="abc", char_delay=0.001)]
        player = Player(repeat=1)
        player._kb_ctrl.type.reset_mock()
        player.play([MacroGroup(window=None, recorded_rect=None, events=events)])
        self.assertEqual(player._kb_ctrl.type.call_count, 3)  # one call per character

    def test_type_text_over_duration_types_each_char(self):
        events = [MacroEvent(type=EventType.TYPE_TEXT, ts=0.0, expr="abcd", duration=0.004)]
        player = Player(repeat=1)
        player._kb_ctrl.type.reset_mock()
        player.play([MacroGroup(window=None, recorded_rect=None, events=events)])
        self.assertEqual(player._kb_ctrl.type.call_count, 4)


if __name__ == "__main__":
    unittest.main()
