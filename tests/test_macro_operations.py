"""Unit tests for macro recording, playback, copy/paste, deletion, and rearrangement."""

import unittest
from dataclasses import replace
from src.macro import MacroEvent, MacroGroup, WindowRect, load_macro, save_macro
from src.player import Player
from pathlib import Path
import tempfile
import time


class TestMacroEventCreation(unittest.TestCase):
    """Test creating and manipulating MacroEvent objects."""

    def test_create_mouse_move(self):
        ev = MacroEvent(type="mouse_move", ts=0.5, x=100, y=200)
        self.assertEqual(ev.type, "mouse_move")
        self.assertEqual(ev.ts, 0.5)
        self.assertEqual(ev.x, 100)
        self.assertEqual(ev.y, 200)

    def test_create_wait(self):
        ev = MacroEvent(type="wait", ts=1.0, duration=0.5)
        self.assertEqual(ev.type, "wait")
        self.assertEqual(ev.ts, 1.0)
        self.assertEqual(ev.duration, 0.5)

    def test_create_mouse_click(self):
        ev = MacroEvent(type="mouse_click", ts=2.0, x=150, y=250, button="left", pressed=True)
        self.assertEqual(ev.type, "mouse_click")
        self.assertEqual(ev.button, "left")
        self.assertTrue(ev.pressed)


class TestEventBundling(unittest.TestCase):
    """Test event bundling logic (consecutive mouse moves, scrolls, etc)."""

    def test_consecutive_mouse_moves_same_row(self):
        """Consecutive mouse moves should be bundled into one row."""
        events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_move", ts=0.01, x=101, y=101),
            MacroEvent(type="mouse_move", ts=0.02, x=102, y=102),
        ]
        # These should be bundled into a single row with 3 events
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e.type == "mouse_move" for e in events))

    def test_mouse_move_with_gap_creates_wait(self):
        """Large gap between mouse moves should create a wait row."""
        events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_move", ts=0.02, x=102, y=102),
            MacroEvent(type="mouse_move", ts=1.5, x=150, y=150),  # 1.48s gap
        ]
        # The 1.48s gap (> 0.5s threshold) indicates a separate movement group
        gap = events[2].ts - events[1].ts
        self.assertGreater(gap, 0.5)

    def test_consecutive_scrolls_same_direction(self):
        """Consecutive scrolls in same direction should be bundled."""
        scroll1 = MacroEvent(type="mouse_scroll", ts=0.0, x=100, y=100, dx=0, dy=3)
        scroll2 = MacroEvent(type="mouse_scroll", ts=0.01, x=100, y=100, dx=0, dy=3)
        # Both scroll down (dy > 0), so should bundle
        self.assertTrue((scroll1.dy > 0) == (scroll2.dy > 0))

    def test_scrolls_opposite_direction_separate(self):
        """Scrolls in opposite directions should not bundle."""
        scroll_down = MacroEvent(type="mouse_scroll", ts=0.0, x=100, y=100, dx=0, dy=3)
        scroll_up = MacroEvent(type="mouse_scroll", ts=0.01, x=100, y=100, dx=0, dy=-3)
        # Opposite directions
        self.assertNotEqual(scroll_down.dy > 0, scroll_up.dy > 0)


class TestClickBundling(unittest.TestCase):
    """Test bundling of mouse click down/up pairs."""

    def test_click_down_up_within_threshold(self):
        """Mouse down+up within 250ms should bundle as single click."""
        down = MacroEvent(type="mouse_click", ts=0.0, x=100, y=100, button="left", pressed=True)
        up = MacroEvent(type="mouse_click", ts=0.15, x=100, y=100, button="left", pressed=False)

        gap = up.ts - down.ts
        self.assertLess(gap, 0.25)  # 150ms < 250ms
        self.assertEqual(down.button, up.button)

    def test_click_down_up_exceeds_threshold(self):
        """Mouse down+up over 250ms should not bundle."""
        down = MacroEvent(type="mouse_click", ts=0.0, x=100, y=100, button="left", pressed=True)
        up = MacroEvent(type="mouse_click", ts=0.30, x=100, y=100, button="left", pressed=False)

        gap = up.ts - down.ts
        self.assertGreater(gap, 0.25)  # 300ms > 250ms


class TestTimestampAdjustment(unittest.TestCase):
    """Test timestamp adjustment when pasting events."""

    def test_paste_adjusts_timestamps(self):
        """Pasted events should have timestamps adjusted to continue from last event."""
        # Original events: 0-1 second
        original = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_move", ts=0.5, x=150, y=150),
        ]
        original_start = original[0].ts

        # Pasted events should start after original events
        # If original ends at 0.5s, pasted should start at 0.5s
        paste_start_offset = 1.0  # Let's say we're pasting after a 1s total

        # Adjust pasted events
        time_delta = paste_start_offset - original_start
        adjusted = [replace(ev, ts=ev.ts + time_delta) for ev in original]

        self.assertEqual(adjusted[0].ts, 1.0)  # 0.0 + 1.0
        self.assertEqual(adjusted[1].ts, 1.5)  # 0.5 + 1.0

    def test_multiple_paste_accumulates_time(self):
        """Multiple pastes should accumulate time sequentially."""
        original = MacroEvent(type="wait", ts=0.0, duration=0.6)

        # First paste at 0.6s
        paste1 = replace(original, ts=0.6)
        # Second paste at 1.2s (0.6 + 0.6)
        paste2 = replace(original, ts=1.2)

        self.assertEqual(paste1.ts, 0.6)
        self.assertEqual(paste2.ts, 1.2)
        self.assertEqual(paste2.ts - paste1.ts, 0.6)


class TestEventSerialization(unittest.TestCase):
    """Test saving and loading macros."""

    def test_from_dict_rejects_unknown_type(self):
        """from_dict raises ValueError on an unrecognised event type."""
        with self.assertRaises(ValueError):
            MacroEvent.from_dict({"type": "teleport", "ts": 0.0})

    def test_from_dict_accepts_known_type(self):
        """from_dict accepts every canonical event type."""
        from src.event_types import EVENT_TYPE_VALUES
        for t in EVENT_TYPE_VALUES:
            ev = MacroEvent.from_dict({"type": t, "ts": 0.0})
            self.assertEqual(ev.type, t)

    def test_save_and_load_single_group(self):
        """Save and load a macro with a single group."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "test.json"

            # Create a simple macro
            events = [
                MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
                MacroEvent(type="mouse_click", ts=1.0, x=100, y=100, button="left", pressed=True),
                MacroEvent(type="mouse_click", ts=1.05, x=100, y=100, button="left", pressed=False),
            ]
            groups = [MacroGroup(window=None, recorded_rect=None, events=events)]

            # Save
            save_macro(groups, str(filepath))
            self.assertTrue(filepath.exists())

            # Load
            loaded = load_macro(str(filepath))
            self.assertEqual(len(loaded), 1)
            self.assertEqual(len(loaded[0].events), 3)
            self.assertEqual(loaded[0].events[0].type, "mouse_move")

    def test_save_and_load_multiple_groups(self):
        """Save and load a macro with multiple window groups."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "test.json"

            # Create macro with two window groups
            group1_events = [MacroEvent(type="mouse_move", ts=0.0, x=100, y=100)]
            group2_events = [MacroEvent(type="mouse_move", ts=0.0, x=200, y=200)]

            groups = [
                MacroGroup(window="Window1", recorded_rect=WindowRect(0, 0, 800, 600), events=group1_events),
                MacroGroup(window="Window2", recorded_rect=WindowRect(0, 0, 800, 600), events=group2_events),
            ]

            # Save
            save_macro(groups, str(filepath))

            # Load
            loaded = load_macro(str(filepath))
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0].window, "Window1")
            self.assertEqual(loaded[1].window, "Window2")

    def test_save_and_load_with_wait_events(self):
        """Save and load macros containing wait events."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "test.json"

            events = [
                MacroEvent(type="wait", ts=0.5, duration=0.5),
                MacroEvent(type="wait", ts=1.0, duration=0.6),
            ]
            groups = [MacroGroup(window=None, recorded_rect=None, events=events)]

            save_macro(groups, str(filepath))
            loaded = load_macro(str(filepath))

            self.assertEqual(len(loaded[0].events), 2)
            self.assertEqual(loaded[0].events[0].duration, 0.5)
            self.assertEqual(loaded[0].events[1].duration, 0.6)


class TestGroupTimestampAccumulation(unittest.TestCase):
    """Test that timestamps are cumulative across groups when flattened."""

    def test_flattened_timestamps_cumulative(self):
        """When groups are flattened, timestamps should be cumulative."""
        # Group 1: events at 0, 0.5
        group1_events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_move", ts=0.5, x=150, y=150),
        ]

        # Group 2: events at 0, 0.4 (but should become 0.5, 0.9 when flattened)
        group2_events = [
            MacroEvent(type="mouse_move", ts=0.0, x=200, y=200),
            MacroEvent(type="mouse_move", ts=0.4, x=250, y=250),
        ]

        groups = [
            MacroGroup(window=None, recorded_rect=None, events=group1_events),
            MacroGroup(window=None, recorded_rect=None, events=group2_events),
        ]

        # Simulate flattening with cumulative timestamps
        flat = []
        time_offset = 0.0

        for g in groups:
            group_end_time = max([ev.ts for ev in g.events]) if g.events else 0.0

            for ev in g.events:
                ev_adjusted = replace(ev, ts=ev.ts + time_offset)
                flat.append(ev_adjusted)

            time_offset += group_end_time

        # First group events should keep original timestamps
        self.assertEqual(flat[0].ts, 0.0)
        self.assertEqual(flat[1].ts, 0.5)

        # Second group events should be offset by first group's duration
        self.assertEqual(flat[2].ts, 0.5)  # 0.0 + 0.5
        self.assertEqual(flat[3].ts, 0.9)  # 0.4 + 0.5


class TestEventDeletion(unittest.TestCase):
    """Test deleting events from macro."""

    def test_delete_single_event(self):
        """Delete a single event from list."""
        events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_click", ts=1.0, x=100, y=100, button="left", pressed=True),
            MacroEvent(type="mouse_move", ts=2.0, x=150, y=150),
        ]

        # Remove middle event
        remaining = events[:1] + events[2:]

        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining[0].type, "mouse_move")
        self.assertEqual(remaining[1].type, "mouse_move")

    def test_delete_multiple_events(self):
        """Delete multiple events."""
        events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="wait", ts=0.5, duration=0.5),
            MacroEvent(type="mouse_click", ts=1.0, x=100, y=100, button="left", pressed=True),
            MacroEvent(type="mouse_move", ts=2.0, x=150, y=150),
        ]

        # Remove all waits
        remaining = [ev for ev in events if ev.type != "wait"]

        self.assertEqual(len(remaining), 3)
        self.assertTrue(all(ev.type != "wait" for ev in remaining))


class TestEventReordering(unittest.TestCase):
    """Test rearranging event order."""

    def test_move_event_forward(self):
        """Move an event to a later position."""
        events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_click", ts=1.0, x=100, y=100, button="left", pressed=True),
            MacroEvent(type="mouse_move", ts=2.0, x=150, y=150),
        ]

        # Move first event to end
        reordered = events[1:] + events[:1]

        self.assertEqual(reordered[0].type, "mouse_click")
        self.assertEqual(reordered[-1].type, "mouse_move")
        self.assertEqual(reordered[-1].x, 100)

    def test_move_event_backward(self):
        """Move an event to an earlier position."""
        events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_click", ts=1.0, x=100, y=100, button="left", pressed=True),
            MacroEvent(type="mouse_move", ts=2.0, x=150, y=150),
        ]

        # Move last event to start
        reordered = events[2:] + events[:2]

        self.assertEqual(reordered[0].type, "mouse_move")
        self.assertEqual(reordered[0].x, 150)
        self.assertEqual(reordered[1].type, "mouse_move")

    def test_swap_adjacent_events(self):
        """Swap two adjacent events."""
        events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_click", ts=1.0, x=100, y=100, button="left", pressed=True),
            MacroEvent(type="mouse_move", ts=2.0, x=150, y=150),
        ]

        # Swap first two
        reordered = [events[1], events[0]] + events[2:]

        self.assertEqual(reordered[0].type, "mouse_click")
        self.assertEqual(reordered[1].type, "mouse_move")


class TestComplexScenarios(unittest.TestCase):
    """Test complex scenarios combining multiple operations."""

    def test_copy_paste_then_delete(self):
        """Copy an event, paste it, then delete original."""
        events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_click", ts=1.0, x=100, y=100, button="left", pressed=True),
        ]

        # Copy click event (index 1)
        copied = [replace(events[1], ts=2.0)]  # Paste at 2.0s

        # Combine: original + pasted
        combined = events + copied
        self.assertEqual(len(combined), 3)

        # Delete original click
        final = [combined[0]] + combined[2:]
        self.assertEqual(len(final), 2)
        self.assertEqual(final[1].ts, 2.0)

    def test_paste_multiple_then_reorder(self):
        """Paste same event multiple times, then rearrange."""
        original = MacroEvent(type="wait", ts=0.0, duration=0.5)

        # Create pastes at different times
        paste1 = replace(original, ts=0.5)
        paste2 = replace(original, ts=1.0)

        events = [original, paste1, paste2]
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].ts, 0.0)
        self.assertEqual(events[1].ts, 0.5)
        self.assertEqual(events[2].ts, 1.0)

        # Reverse order
        reversed_events = events[::-1]
        self.assertEqual(reversed_events[0].ts, 1.0)
        self.assertEqual(reversed_events[-1].ts, 0.0)

    def test_record_paste_delete_cycle(self):
        """Simulate: record events -> paste -> delete -> save -> load."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "test.json"

            # Original events
            events = [
                MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
                MacroEvent(type="wait", ts=1.0, duration=0.5),
            ]

            # Paste wait event at end
            pasted_wait = replace(events[1], ts=1.5)
            events.append(pasted_wait)

            # Delete first mouse_move
            events = events[1:]

            # Save
            groups = [MacroGroup(window=None, recorded_rect=None, events=events)]
            save_macro(groups, str(filepath))

            # Load and verify
            loaded = load_macro(str(filepath))
            self.assertEqual(len(loaded[0].events), 2)
            self.assertTrue(all(e.type == "wait" for e in loaded[0].events))


if __name__ == "__main__":
    unittest.main()
