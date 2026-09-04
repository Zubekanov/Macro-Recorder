"""Tests for timestamp normalization after copy/paste/delete/reorder operations.

These exercise the real TableModel.normalize_timestamps (no duplicated copy).
"""

import unittest
from macro_recorder.macro import MacroEvent
from macro_recorder.table_model import TableModel


class TestTimestampNormalization(unittest.TestCase):
    """Test TableModel.normalize_timestamps."""

    def test_normalize_simple_scramble(self):
        """Normalize timestamps when rows are visually out of order."""
        row_events = {}

        # Row order in table: [row1, row2, row3]
        # But row2 has ts=4.5 and row3 has ts=2.5 (scrambled)
        row_events['row1'] = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_move", ts=0.2, x=102, y=102),
        ]
        row_events['row2'] = [
            MacroEvent(type="wait", ts=4.5, duration=1.0),
        ]
        row_events['row3'] = [
            MacroEvent(type="mouse_move", ts=2.5, x=200, y=200),
            MacroEvent(type="mouse_move", ts=2.6, x=201, y=201),
        ]

        # Apply normalization
        table_order = ['row1', 'row2', 'row3']
        normalized = self._normalize(row_events, table_order)

        # Check all events are in order
        all_events = [e for events in normalized.values() for e in events]
        for i in range(1, len(all_events)):
            self.assertGreaterEqual(all_events[i].ts, all_events[i-1].ts,
                f"Event {i} has ts {all_events[i].ts} < {all_events[i-1].ts}")

    def test_normalize_paste_in_middle(self):
        """When pasting a bundle in the middle, normalization re-sequences all rows."""
        row_events = {}

        # Original: [row1 (ts: 0-1), row2 (ts: 2-3)]
        # Paste clipboard (ts: 0-0.5) at position 1: [row1, pasted, row2]
        # The pasted bundle preserves its internal timing (0 to 0.5)
        # After normalization, it should start when row1 ends

        row_events['row1'] = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_move", ts=0.5, x=101, y=101),
        ]
        row_events['pasted'] = [
            MacroEvent(type="mouse_move", ts=0.0, x=200, y=200),
            MacroEvent(type="mouse_move", ts=0.3, x=201, y=201),
        ]
        row_events['row2'] = [
            MacroEvent(type="mouse_move", ts=2.0, x=300, y=300),
        ]

        table_order = ['row1', 'pasted', 'row2']
        normalized = self._normalize(row_events, table_order)

        # After normalization:
        # row1: 0.0-0.5
        # pasted: 0.5-0.8 (preserves internal 0.3s gap)
        # row2: 0.8+
        row1_start = normalized['row1'][0].ts
        pasted_start = normalized['pasted'][0].ts
        row2_start = normalized['row2'][0].ts

        self.assertEqual(row1_start, 0.0)
        # Pasted starts after row1 ends (0.5) plus the instant-action min spacing.
        self.assertAlmostEqual(pasted_start, 0.55, places=4)
        # Row2 should start after pasted
        self.assertGreater(row2_start, pasted_start)

    def test_normalize_drag_reorder(self):
        """After dragging a row to a new position, timestamps adjust correctly."""
        row_events = {}

        # Original order: [move_right, move_left, move_right]
        # User drags move_left to position 0: [move_left, move_right, move_right]
        # The old ts values were: 0-1s, 1-2s, 2-3s
        # After reorder and normalize: should be 0-1s, 1-2s, 2-3s in new visual order

        row_events['row1'] = [MacroEvent(type="mouse_move", ts=1.0, x=100, y=100),
                              MacroEvent(type="mouse_move", ts=1.5, x=101, y=101)]
        row_events['row2'] = [MacroEvent(type="mouse_move", ts=0.0, x=200, y=200),
                              MacroEvent(type="mouse_move", ts=0.5, x=201, y=201)]
        row_events['row3'] = [MacroEvent(type="mouse_move", ts=2.0, x=300, y=300),
                              MacroEvent(type="mouse_move", ts=2.5, x=301, y=301)]

        # After drag, visual order is [row2, row1, row3]
        table_order = ['row2', 'row1', 'row3']
        normalized = self._normalize(row_events, table_order)

        # Verify ordering
        all_events = [e for iid in table_order for e in normalized[iid]]
        for i in range(1, len(all_events)):
            self.assertGreaterEqual(all_events[i].ts, all_events[i-1].ts)

        # Verify first event of each row is in order
        first_of_row2 = normalized['row2'][0].ts
        first_of_row1 = normalized['row1'][0].ts
        first_of_row3 = normalized['row3'][0].ts
        self.assertLess(first_of_row2, first_of_row1)
        self.assertLess(first_of_row1, first_of_row3)

    def test_normalize_delete_and_playback(self):
        """After deleting a row, remaining rows have correct relative timing."""
        row_events = {}

        # row1: 0-1s (move), row2: 1-1.5s (wait), row3: 1.5-2s (move)
        row_events['row1'] = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_move", ts=1.0, x=101, y=101),
        ]
        row_events['row2'] = [MacroEvent(type="wait", ts=1.0, duration=0.5)]
        row_events['row3'] = [MacroEvent(type="mouse_move", ts=1.5, x=200, y=200)]

        # Delete row2 (the wait)
        del row_events['row2']
        table_order = ['row1', 'row3']
        normalized = self._normalize(row_events, table_order)

        # row1: 0-1, row3 starts at 1.0 plus the instant-action min spacing.
        self.assertEqual(normalized['row1'][0].ts, 0.0)
        self.assertEqual(normalized['row1'][-1].ts, 1.0)
        self.assertAlmostEqual(normalized['row3'][0].ts, 1.05, places=4)

    def test_normalize_with_window_focus(self):
        """Normalization resets clock at window_focus rows."""
        row_events = {}

        # Group 1: regular moves
        row_events['row1'] = [MacroEvent(type="mouse_move", ts=0.0, x=100, y=100)]

        # Group boundary
        row_events['row2'] = [MacroEvent(type="window_focus", ts=1.0, window="NotePad")]

        # Group 2: regular moves (should reset to ts~0)
        row_events['row3'] = [MacroEvent(type="mouse_move", ts=0.0, x=200, y=200)]

        table_order = ['row1', 'row2', 'row3']
        normalized = self._normalize(row_events, table_order)

        # row1 at 0.0
        self.assertEqual(normalized['row1'][0].ts, 0.0)
        # row2 (window_focus) stays as-is
        self.assertEqual(normalized['row2'][0].ts, 1.0)
        # row3 should reset clock to 0.0 (new group)
        self.assertEqual(normalized['row3'][0].ts, 0.0)

    def _normalize(self, row_events, table_order):
        """Run the production normalization via TableModel."""
        model = TableModel()
        model.row_events = dict(row_events)
        model.normalize_timestamps(table_order)
        return model.row_events


if __name__ == "__main__":
    unittest.main()
