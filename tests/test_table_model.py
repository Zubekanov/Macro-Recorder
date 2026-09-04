"""Unit tests for the UI-free TableModel (timing + group conversion)."""

import unittest

from macro_recorder.event_types import EventType
from macro_recorder.macro import MacroEvent, MacroGroup, WindowRect
from macro_recorder.table_model import TableModel


class TestNormalize(unittest.TestCase):
    def test_normalize_anchors_first_row_at_zero(self):
        model = TableModel()
        model.row_events = {
            "r0": [MacroEvent(type=EventType.MOUSE_MOVE, ts=5.0, x=0, y=0)],
        }
        model.normalize_timestamps(["r0"])
        self.assertEqual(model.row_events["r0"][0].ts, 0.0)

    def test_normalize_advances_by_wait_duration(self):
        model = TableModel()
        model.row_events = {
            "r0": [MacroEvent(type=EventType.WAIT, ts=2.0, duration=0.5)],
            "r1": [MacroEvent(type=EventType.MOUSE_MOVE, ts=9.0, x=0, y=0)],
        }
        model.normalize_timestamps(["r0", "r1"])
        self.assertEqual(model.row_events["r0"][0].ts, 0.0)
        # next row starts after the wait's duration
        self.assertEqual(model.row_events["r1"][0].ts, 0.5)


class TestEventToIidMap(unittest.TestCase):
    def test_rows_to_groups_fills_identity_map(self):
        model = TableModel()
        ev = MacroEvent(type=EventType.MOUSE_MOVE, ts=0.0, x=10, y=20)
        model.row_events = {"r0": [ev]}
        id_map: dict[int, str] = {}
        groups = model.rows_to_groups(["r0"], {"r0": None}, id_map)
        # The event placed in the group maps back to its row id.
        placed = groups[0].events[0]
        self.assertEqual(id_map[id(placed)], "r0")


class TestInstructionNumbering(unittest.TestCase):
    def test_first_event_of_each_row_tagged_with_row_number(self):
        model = TableModel()
        model.row_events = {
            "r1": [MacroEvent(type=EventType.MOUSE_MOVE, ts=0.0, x=1, y=1),
                   MacroEvent(type=EventType.MOUSE_MOVE, ts=0.1, x=2, y=2)],
            "r2": [MacroEvent(type=EventType.VAR_SET, ts=0.2, var_name="x", expr="1")],
        }
        order = ["r1", "r2"]
        groups = model.rows_to_groups(order, {i: None for i in order})
        events = groups[0].events
        # Row 1's first event is instruction #1; its bundled sibling is unmarked.
        self.assertEqual(events[0].instr, 1)
        self.assertIsNone(events[1].instr)
        # Row 2's single event is instruction #2.
        self.assertEqual(events[2].instr, 2)

    def test_window_focus_rows_count_toward_numbers(self):
        rect = WindowRect(left=0, top=0, width=10, height=10)
        model = TableModel()
        model.row_events = {
            "r1": [MacroEvent(type=EventType.VAR_SET, ts=0.0, var_name="x", expr="1")],
            "wf": [MacroEvent(type=EventType.WINDOW_FOCUS, ts=0.0, window="W",
                              rect=[rect.left, rect.top, rect.width, rect.height])],
            "r3": [MacroEvent(type=EventType.VAR_SET, ts=0.1, var_name="y", expr="2")],
        }
        order = ["r1", "wf", "r3"]
        groups = model.rows_to_groups(order, {i: None for i in order})
        # wf is instruction #2 (counted, like the table), so r3 is #3.
        leading = groups[0].events
        windowed = groups[1].events
        self.assertEqual(leading[0].instr, 1)
        self.assertEqual(windowed[0].instr, 3)


class TestGroupConversion(unittest.TestCase):
    def test_flatten_windowed_group_makes_absolute(self):
        rect = WindowRect(left=100, top=200, width=800, height=600)
        group = MacroGroup(window="App", recorded_rect=rect, events=[
            MacroEvent(type=EventType.MOUSE_MOVE, ts=0.0, x=50, y=50),
        ])
        flat = TableModel.groups_to_flat_events([group])

        self.assertEqual(flat[0].type, EventType.WINDOW_FOCUS)
        self.assertEqual(flat[0].window, "App")
        self.assertEqual(flat[0].rect, [100, 200, 800, 600])
        # relative (50,50) → absolute (150,250)
        self.assertEqual((flat[1].x, flat[1].y), (150, 250))

    def test_rows_to_groups_makes_relative_and_syncs_label(self):
        model = TableModel()
        rect = [100, 200, 800, 600]
        model.row_events = {
            "r0": [MacroEvent(type=EventType.WINDOW_FOCUS, ts=0.0, window="App", rect=rect)],
            "r1": [MacroEvent(type=EventType.MOUSE_MOVE, ts=0.0, x=150, y=250)],
        }
        groups = model.rows_to_groups(["r0", "r1"], {"r0": None, "r1": "tap"})

        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g.window, "App")
        self.assertEqual((g.recorded_rect.left, g.recorded_rect.top), (100, 200))
        # absolute (150,250) → relative (50,50)
        self.assertEqual((g.events[0].x, g.events[0].y), (50, 50))
        self.assertEqual(g.events[0].label, "tap")

    def test_round_trip_preserves_coordinates(self):
        rect = WindowRect(left=100, top=200, width=800, height=600)
        original = MacroGroup(window="App", recorded_rect=rect, events=[
            MacroEvent(type=EventType.MOUSE_MOVE, ts=0.0, x=50, y=50),
        ])
        flat = TableModel.groups_to_flat_events([original])

        # Rebuild row_events: each flat event as its own row.
        model = TableModel()
        order = []
        for i, ev in enumerate(flat):
            iid = f"r{i}"
            model.row_events[iid] = [ev]
            order.append(iid)
        groups = model.rows_to_groups(order, {iid: None for iid in order})

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].window, "App")
        self.assertEqual((groups[0].events[0].x, groups[0].events[0].y), (50, 50))


if __name__ == "__main__":
    unittest.main()
