"""Tests for the event-driven playback highlighting signal.

The player reports the row it is about to execute/dwell on via on_active_event.
The UI throttles these into row highlights (throttling is UI-side and not
covered here); these tests verify the player emits the right events, in order,
for both linear and control-flow playback.
"""

import unittest

from tests.playback_test_utils import start_player_io_patches, stop_player_io_patches
from src.event_types import EventType
from src.macro import MacroEvent, MacroGroup
from src.player import GOTO_START, Player


def setUpModule():
    start_player_io_patches()   # playback must not drive the real mouse/keyboard


def tearDownModule():
    stop_player_io_patches()


def _capture(events, repeat=1):
    """Play a group and return the list of events reported as active, in order."""
    reported = []
    player = Player(speed=1.0, repeat=repeat, on_active_event=reported.append)
    player.play([MacroGroup(window=None, recorded_rect=None, events=events)])
    return reported


class TestActiveEventLinear(unittest.TestCase):
    def test_reports_each_event_in_order(self):
        events = [
            MacroEvent(type=EventType.MOUSE_MOVE, ts=0.0, x=10, y=10),
            MacroEvent(type=EventType.WAIT, ts=0.05, duration=0.05),
            MacroEvent(type=EventType.MOUSE_MOVE, ts=0.10, x=20, y=20),
        ]
        reported = _capture(events)
        # Every event is reported once, in order (no events skipped).
        self.assertEqual([e.type for e in reported],
                         [EventType.MOUSE_MOVE, EventType.WAIT, EventType.MOUSE_MOVE])

    def test_wait_is_reported(self):
        events = [MacroEvent(type=EventType.WAIT, ts=0.0, duration=0.05)]
        reported = _capture(events)
        self.assertEqual([e.type for e in reported], [EventType.WAIT])


class TestActiveEventControlFlow(unittest.TestCase):
    def test_instant_goto_is_reported_but_loop_dwells_on_wait(self):
        # 1. wait  2. goto start  → reported sequence is wait, goto, wait, goto...
        # The throttle (UI-side) drops the instant goto; here we just confirm the
        # engine keeps reporting and the wait appears each cycle.
        events = [
            MacroEvent(type=EventType.WAIT, ts=0.0, duration=0.02, label="top"),
            MacroEvent(type=EventType.GOTO, ts=0.02, target=GOTO_START),
        ]
        # Cap the otherwise-infinite loop by stopping after a few reports.
        reported = []
        player = Player(speed=1.0, repeat=1)

        def capped(ev):
            reported.append(ev)
            if len(reported) >= 6:
                player.stop()
        player._on_active_event = capped
        player.play([MacroGroup(window=None, recorded_rect=None, events=events)])

        types = [e.type for e in reported]
        # Should alternate wait, goto, wait, goto, ... and include both.
        self.assertIn(EventType.WAIT, types)
        self.assertIn(EventType.GOTO, types)
        # First reported instruction is the wait (program counter starts at 0).
        self.assertEqual(types[0], EventType.WAIT)

    def test_conditional_loop_reports_until_exit(self):
        # x=0 ; [top] x=x+1 ; if x<3 goto top
        events = [
            MacroEvent(type=EventType.VAR_SET, ts=0.0, var_name="x", expr="0"),
            MacroEvent(type=EventType.VAR_SET, ts=0.01, var_name="x", expr="x + 1", label="top"),
            MacroEvent(type=EventType.GOTO_IF, ts=0.02, expr="x < 3", target="top"),
        ]
        reported = _capture(events)
        # var_set(top) executes 3 times; goto_if evaluated 3 times.
        top_reports = [e for e in reported if e.label == "top"]
        self.assertEqual(len(top_reports), 3)


if __name__ == "__main__":
    unittest.main()
