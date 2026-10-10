"""Recorded drags become press, one timed move, release."""

import unittest

from macro_recorder.event_types import EventType
from macro_recorder.macro import MacroEvent
from macro_recorder.recorder import collapse_drags


def click(ts, x, y, pressed, button="left"):
    return MacroEvent(type=EventType.MOUSE_CLICK, ts=ts, x=x, y=y, button=button, pressed=pressed)


def move(ts, x, y):
    return MacroEvent(type=EventType.MOUSE_MOVE, ts=ts, x=x, y=y)


class TestCollapseDrags(unittest.TestCase):
    def test_drag_becomes_press_timed_move_release(self):
        events = [
            click(1.0, 10, 10, True),
            move(1.1, 20, 15),
            move(1.2, 40, 25),
            move(1.5, 60, 30),
            click(1.7, 60, 30, False),
        ]
        out = collapse_drags(events)
        self.assertEqual([e.type for e in out],
                         [EventType.MOUSE_CLICK, EventType.MOUSE_MOVE_TIMED, EventType.MOUSE_CLICK])
        press, timed, release = out
        self.assertIs(press, events[0])
        self.assertIs(release, events[-1])
        self.assertEqual((timed.x, timed.y, timed.dx, timed.dy), (10, 10, 60, 30))
        self.assertEqual(timed.ts, 1.1)
        self.assertAlmostEqual(timed.duration, 0.4)

    def test_click_without_movement_is_untouched(self):
        events = [click(0.0, 5, 5, True), click(0.1, 5, 5, False)]
        self.assertEqual(collapse_drags(events), events)

    def test_press_release_same_spot_with_jitter_is_untouched(self):
        events = [click(0.0, 5, 5, True), move(0.05, 6, 5), click(0.1, 5, 5, False)]
        self.assertEqual(collapse_drags(events), events)

    def test_other_events_between_break_the_drag(self):
        events = [
            click(0.0, 0, 0, True),
            move(0.1, 5, 5),
            MacroEvent(type=EventType.KEY_PRESS, ts=0.2, key="Key.shift"),
            move(0.3, 10, 10),
            click(0.4, 10, 10, False),
        ]
        self.assertEqual(collapse_drags(events), events)

    def test_different_button_release_is_not_a_drag(self):
        events = [click(0.0, 0, 0, True, "left"), move(0.1, 9, 9), click(0.2, 9, 9, False, "right")]
        self.assertEqual(collapse_drags(events), events)

    def test_free_moves_and_surrounding_events_pass_through(self):
        before = [move(0.0, 1, 1), move(0.1, 2, 2)]
        drag = [click(0.5, 2, 2, True), move(0.6, 8, 8), click(0.7, 8, 8, False)]
        after = [move(1.0, 9, 9), MacroEvent(type=EventType.KEY_PRESS, ts=1.1, key="a")]
        out = collapse_drags(before + drag + after)
        self.assertEqual(out[:2], before)
        self.assertEqual(out[-2:], after)
        self.assertEqual(len(out), 2 + 3 + 2)

    def test_single_move_drag_gets_minimum_duration(self):
        out = collapse_drags([click(0.0, 0, 0, True), move(0.3, 50, 50), click(0.4, 50, 50, False)])
        self.assertEqual(out[1].type, EventType.MOUSE_MOVE_TIMED)
        self.assertGreater(out[1].duration, 0)

    def test_two_consecutive_drags(self):
        events = [
            click(0.0, 0, 0, True), move(0.1, 5, 5), click(0.2, 5, 5, False),
            click(1.0, 5, 5, True), move(1.1, 9, 9), click(1.2, 9, 9, False),
        ]
        out = collapse_drags(events)
        self.assertEqual([e.type for e in out], [EventType.MOUSE_CLICK, EventType.MOUSE_MOVE_TIMED,
                                                  EventType.MOUSE_CLICK] * 2)

    def test_input_not_mutated(self):
        events = [click(0.0, 0, 0, True), move(0.1, 5, 5), click(0.2, 5, 5, False)]
        snapshot = [MacroEvent(**e.__dict__) for e in events]
        collapse_drags(events)
        self.assertEqual(events, snapshot)


if __name__ == "__main__":
    unittest.main()
