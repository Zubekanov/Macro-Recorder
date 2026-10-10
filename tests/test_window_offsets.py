"""Window-relative coordinate conversion for fields that hold a second point.

Timed moves and region actions keep a destination or corner in dx/dy.  Those
must shift with the window origin exactly like x/y when events move between
the absolute table view and the window-relative file form.
"""

import unittest

from macro_recorder.event_types import EventType
from macro_recorder.macro import MacroEvent, MacroGroup, WindowRect, offset_event
from macro_recorder.recorder import Recorder
from macro_recorder.table_model import TableModel

RECT = WindowRect(left=100, top=50, width=800, height=600)


class TestOffsetEvent(unittest.TestCase):
    def test_timed_move_shifts_both_points(self):
        ev = MacroEvent(type=EventType.MOUSE_MOVE_TIMED, ts=0.0, x=10, y=20, dx=30, dy=40, duration=0.5)
        out = offset_event(ev, 100, 50)
        self.assertEqual((out.x, out.y, out.dx, out.dy), (110, 70, 130, 90))

    def test_region_actions_shift_corners(self):
        for t in (EventType.OCR_READ, EventType.MATCH_IMAGE, EventType.MATCH_TEXT):
            ev = MacroEvent(type=t, ts=0.0, x=1, y=2, dx=3, dy=4, var_name="v")
            out = offset_event(ev, -1, -2)
            self.assertEqual((out.x, out.y, out.dx, out.dy), (0, 0, 2, 2), t)

    def test_scroll_delta_is_not_shifted(self):
        ev = MacroEvent(type=EventType.MOUSE_SCROLL, ts=0.0, x=10, y=10, dx=0, dy=3)
        out = offset_event(ev, 5, 5)
        self.assertEqual((out.x, out.y, out.dx, out.dy), (15, 15, 0, 3))

    def test_plain_string_type_is_recognised(self):
        ev = MacroEvent(type="mouse_move_timed", ts=0.0, x=0, y=0, dx=1, dy=1)
        out = offset_event(ev, 7, 7)
        self.assertEqual((out.dx, out.dy), (8, 8))

    def test_expression_coordinates_are_left_alone(self):
        ev = MacroEvent(type=EventType.MOUSE_MOVE_TIMED, ts=0.0, x="a", y=5, dx="b + 1", dy=6)
        out = offset_event(ev, 10, 10)
        self.assertEqual((out.x, out.y, out.dx, out.dy), ("a", 15, "b + 1", 16))

    def test_no_coordinates_returns_same_object(self):
        ev = MacroEvent(type=EventType.KEY_PRESS, ts=0.0, key="a")
        self.assertIs(offset_event(ev, 3, 3), ev)


class TestTableModelRoundTrip(unittest.TestCase):
    def test_timed_move_round_trips_through_table(self):
        relative = MacroEvent(type=EventType.MOUSE_MOVE_TIMED, ts=0.0, x=10, y=20, dx=30, dy=40, duration=0.5)
        group = MacroGroup(window="App", recorded_rect=RECT, events=[relative])

        model = TableModel()
        flat = model.groups_to_flat_events([group])
        timed = [e for e in flat if e.type == EventType.MOUSE_MOVE_TIMED][0]
        self.assertEqual((timed.x, timed.y, timed.dx, timed.dy), (110, 70, 130, 90))

        model.row_events = {"r0": [flat[0]], "r1": [timed]}
        groups = model.rows_to_groups(["r0", "r1"], {"r0": None, "r1": None})
        back = groups[0].events[0]
        self.assertEqual((back.x, back.y, back.dx, back.dy), (10, 20, 30, 40))

    def test_expression_coordinate_in_windowed_group_survives(self):
        ev = MacroEvent(type=EventType.MOUSE_MOVE, ts=0.0, x="px", y=5)
        group = MacroGroup(window="App", recorded_rect=RECT, events=[ev])
        model = TableModel()
        flat = model.groups_to_flat_events([group])
        self.assertEqual((flat[1].x, flat[1].y), ("px", 55))
        model.row_events = {"r0": [flat[0]], "r1": [flat[1]]}
        back = model.rows_to_groups(["r0", "r1"], {"r0": None, "r1": None})[0].events[0]
        self.assertEqual((back.x, back.y), ("px", 5))


class TestRecorderGrouping(unittest.TestCase):
    def test_build_groups_makes_corner_fields_relative(self):
        events = [
            MacroEvent(type=EventType.WINDOW_FOCUS, ts=0.0, window="App", rect=[100, 50, 800, 600]),
            MacroEvent(type=EventType.MOUSE_MOVE_TIMED, ts=0.1, x=110, y=70, dx=130, dy=90, duration=0.2),
            MacroEvent(type=EventType.OCR_READ, ts=0.5, x=200, y=100, dx=300, dy=150, var_name="t"),
        ]
        groups = Recorder._build_groups(events)
        self.assertEqual(len(groups), 1)
        timed, ocr = groups[0].events
        self.assertEqual((timed.x, timed.y, timed.dx, timed.dy), (10, 20, 30, 40))
        self.assertEqual((ocr.x, ocr.y, ocr.dx, ocr.dy), (100, 50, 200, 100))


if __name__ == "__main__":
    unittest.main()
