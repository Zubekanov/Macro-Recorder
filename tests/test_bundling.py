"""Unit tests for the pure event-bundling logic in src/bundling.py."""

import unittest

from src.bundling import bundle_events
from src.event_types import EventType
from src.macro import MacroEvent

MOVE_GAP = 0.5
CLICK_GAP = 0.25


def move(ts, x, y):
    return MacroEvent(type=EventType.MOUSE_MOVE, ts=ts, x=x, y=y)


def click(ts, pressed, button="left"):
    return MacroEvent(type=EventType.MOUSE_CLICK, ts=ts, x=0, y=0,
                      button=button, pressed=pressed)


def scroll(ts, dx, dy):
    return MacroEvent(type=EventType.MOUSE_SCROLL, ts=ts, x=0, y=0, dx=dx, dy=dy)


class TestBundleEvents(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(bundle_events([], MOVE_GAP, CLICK_GAP), [])

    def test_click_pair_within_threshold_bundles(self):
        evs = [click(0.0, True), click(0.1, False)]
        rows = bundle_events(evs, MOVE_GAP, CLICK_GAP)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 2)

    def test_click_pair_over_threshold_does_not_bundle(self):
        evs = [click(0.0, True), click(0.5, False)]  # 500ms > 250ms
        rows = bundle_events(evs, MOVE_GAP, CLICK_GAP)
        self.assertEqual(len(rows), 2)

    def test_consecutive_moves_within_gap_bundle(self):
        evs = [move(0.0, 0, 0), move(0.1, 5, 5), move(0.2, 10, 10)]
        rows = bundle_events(evs, MOVE_GAP, CLICK_GAP)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 3)

    def test_move_gap_inserts_synthetic_wait(self):
        evs = [move(0.0, 0, 0), move(2.0, 9, 9)]  # 2s gap > 0.5s
        rows = bundle_events(evs, MOVE_GAP, CLICK_GAP)
        # move row, wait row, move row
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0].type, EventType.MOUSE_MOVE)
        self.assertEqual(rows[1][0].type, EventType.WAIT)
        self.assertAlmostEqual(rows[1][0].duration, 2.0)
        self.assertEqual(rows[2][0].type, EventType.MOUSE_MOVE)

    def test_same_direction_scrolls_bundle(self):
        evs = [scroll(0.0, 0, -1), scroll(0.1, 0, -1), scroll(0.2, 0, -1)]
        rows = bundle_events(evs, MOVE_GAP, CLICK_GAP)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 3)

    def test_opposite_direction_scrolls_split(self):
        evs = [scroll(0.0, 0, -1), scroll(0.1, 0, 1)]
        rows = bundle_events(evs, MOVE_GAP, CLICK_GAP)
        self.assertEqual(len(rows), 2)

    def test_does_not_mutate_input(self):
        evs = [move(0.0, 0, 0), move(2.0, 9, 9)]
        before = [(e.type, e.ts) for e in evs]
        bundle_events(evs, MOVE_GAP, CLICK_GAP)
        after = [(e.type, e.ts) for e in evs]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
