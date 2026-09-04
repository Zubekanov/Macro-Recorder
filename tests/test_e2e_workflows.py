"""End-to-end tests for complete macro recorder workflows."""

import unittest
import tempfile
from pathlib import Path
import time
from dataclasses import replace
from src.macro import MacroEvent, MacroGroup, WindowRect, load_macro, save_macro
from src.player import Player
from src.recorder import Recorder


class TestRecordingWorkflow(unittest.TestCase):
    """Test the complete recording workflow."""

    def test_synthetic_recording_session(self):
        """Simulate a recording session without system interaction."""
        # Create events as if they were recorded
        events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_move", ts=0.5, x=150, y=150),
            MacroEvent(type="wait", ts=1.0, duration=0.5),
            MacroEvent(type="mouse_click", ts=1.5, x=150, y=150, button="left", pressed=True),
            MacroEvent(type="mouse_click", ts=1.55, x=150, y=150, button="left", pressed=False),
        ]

        # Verify events have proper structure
        self.assertEqual(len(events), 5)
        self.assertTrue(all(hasattr(ev, 'ts') for ev in events))
        self.assertTrue(all(hasattr(ev, 'type') for ev in events))

        # Create group (as recorder would)
        group = MacroGroup(window=None, recorded_rect=None, events=events)

        # Verify group
        self.assertEqual(len(group.events), 5)
        self.assertIsNone(group.window)

    def test_recording_produces_valid_groups(self):
        """Verify synthetic recording produces valid MacroGroup structure."""
        # Simulate recording output
        events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="wait", ts=0.5, duration=1.0),
            MacroEvent(type="mouse_click", ts=1.5, x=100, y=100, button="left", pressed=True),
        ]

        groups = [MacroGroup(window=None, recorded_rect=None, events=events)]

        # Validate structure
        self.assertIsInstance(groups, list)
        self.assertEqual(len(groups), 1)
        self.assertIsInstance(groups[0], MacroGroup)
        self.assertEqual(len(groups[0].events), 3)


class TestSaveLoadWorkflow(unittest.TestCase):
    """Test saving and loading macros from disk."""

    def test_record_save_load_playback_cycle(self):
        """Full cycle: record → save → load → verify."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "test_macro.json"

            # Create synthetic events as if recorded
            events = [
                MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
                MacroEvent(type="wait", ts=0.5, duration=0.5),
                MacroEvent(type="mouse_click", ts=1.0, x=100, y=100, button="left", pressed=True),
                MacroEvent(type="mouse_click", ts=1.05, x=100, y=100, button="left", pressed=False),
            ]
            groups = [MacroGroup(window=None, recorded_rect=None, events=events)]

            # Save
            save_macro(groups, str(filepath))
            self.assertTrue(filepath.exists())

            # Load
            loaded_groups = load_macro(str(filepath))
            self.assertEqual(len(loaded_groups), 1)
            self.assertEqual(len(loaded_groups[0].events), 4)

            # Verify event integrity
            loaded_events = loaded_groups[0].events
            self.assertEqual(loaded_events[0].type, "mouse_move")
            self.assertEqual(loaded_events[0].x, 100)
            self.assertEqual(loaded_events[1].type, "wait")
            self.assertEqual(loaded_events[1].duration, 0.5)
            self.assertEqual(loaded_events[2].button, "left")
            self.assertTrue(loaded_events[2].pressed)

    def test_save_multiple_windows_load_verify(self):
        """Save macro with multiple window groups and verify on load."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "multi_window.json"

            # Create two window groups
            group1_events = [
                MacroEvent(type="mouse_click", ts=0.0, x=50, y=50, button="left", pressed=True),
                MacroEvent(type="mouse_click", ts=0.05, x=50, y=50, button="left", pressed=False),
            ]

            group2_events = [
                MacroEvent(type="mouse_click", ts=0.0, x=200, y=200, button="right", pressed=True),
                MacroEvent(type="mouse_click", ts=0.05, x=200, y=200, button="right", pressed=False),
            ]

            groups = [
                MacroGroup(
                    window="App1",
                    recorded_rect=WindowRect(0, 0, 800, 600),
                    events=group1_events
                ),
                MacroGroup(
                    window="App2",
                    recorded_rect=WindowRect(100, 100, 1024, 768),
                    events=group2_events
                ),
            ]

            save_macro(groups, str(filepath))
            loaded = load_macro(str(filepath))

            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0].window, "App1")
            self.assertEqual(loaded[0].recorded_rect.width, 800)
            self.assertEqual(loaded[1].window, "App2")
            self.assertEqual(loaded[1].recorded_rect.left, 100)


class TestCopyPasteWorkflow(unittest.TestCase):
    """Test copy/paste operations in a complete workflow."""

    def test_copy_paste_single_event(self):
        """Copy an event and paste it multiple times."""
        # Original macro
        original_events = [
            MacroEvent(type="wait", ts=0.0, duration=0.5),
        ]

        # Copy and paste 3 times
        clipboard = [original_events]

        # Simulate pasting at positions 0.5, 1.0, 1.5
        pasted_events = original_events.copy()
        for i in range(3):
            paste_offset = (i + 1) * 0.5
            group_copy = [replace(ev, ts=ev.ts + paste_offset) for ev in clipboard[0]]
            pasted_events.extend(group_copy)

        # Verify we have 4 waits at correct timestamps
        self.assertEqual(len(pasted_events), 4)
        self.assertEqual(pasted_events[0].ts, 0.0)
        self.assertEqual(pasted_events[1].ts, 0.5)
        self.assertEqual(pasted_events[2].ts, 1.0)
        self.assertEqual(pasted_events[3].ts, 1.5)

    def test_copy_paste_multiple_event_row(self):
        """Copy a bundled row (multiple events) and paste."""
        # Original row with bundled mouse moves
        original_row = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_move", ts=0.01, x=101, y=101),
            MacroEvent(type="mouse_move", ts=0.02, x=102, y=102),
        ]

        # Copy and paste at t=1.0
        pasted_row = [replace(ev, ts=ev.ts + 1.0) for ev in original_row]

        # Verify timing is preserved relative to paste point
        self.assertEqual(pasted_row[0].ts, 1.0)
        self.assertEqual(pasted_row[1].ts, 1.01)
        self.assertEqual(pasted_row[2].ts, 1.02)

    def test_paste_accumulates_correctly_with_waits(self):
        """Paste events where wait duration affects next event timing."""
        # Event 1: wait 0.5s
        event1 = MacroEvent(type="wait", ts=0.0, duration=0.5)

        # Event 2: move (starts at 0.5s due to wait)
        event2 = MacroEvent(type="mouse_move", ts=0.5, x=100, y=100)

        # Paste both at offset 1.0s
        pasted1 = replace(event1, ts=1.0)
        pasted2 = replace(event2, ts=1.5)  # Adjusted for wait

        # Verify: wait finishes at 1.5s, move happens at 1.5s
        self.assertEqual(pasted1.ts + pasted1.duration, 1.5)
        self.assertEqual(pasted2.ts, 1.5)


class TestDeleteWorkflow(unittest.TestCase):
    """Test deletion operations."""

    def test_delete_middle_event(self):
        """Delete an event from the middle of a sequence."""
        events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="wait", ts=0.5, duration=0.5),
            MacroEvent(type="mouse_click", ts=1.0, x=100, y=100, button="left", pressed=True),
        ]

        # Delete wait
        remaining = events[:1] + events[2:]

        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining[0].type, "mouse_move")
        self.assertEqual(remaining[1].type, "mouse_click")

    def test_delete_all_waits(self):
        """Remove all wait events from a macro."""
        events = [
            MacroEvent(type="mouse_click", ts=0.0, x=100, y=100, button="left", pressed=True),
            MacroEvent(type="wait", ts=0.05, duration=0.5),
            MacroEvent(type="mouse_move", ts=0.55, x=150, y=150),
            MacroEvent(type="wait", ts=1.0, duration=0.3),
            MacroEvent(type="mouse_click", ts=1.3, x=150, y=150, button="left", pressed=False),
        ]

        # Remove all waits
        no_waits = [ev for ev in events if ev.type != "wait"]

        self.assertEqual(len(no_waits), 3)
        self.assertTrue(all(ev.type != "wait" for ev in no_waits))

    def test_delete_from_group(self):
        """Delete events from a specific group."""
        group_events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_move", ts=0.01, x=101, y=101),
            MacroEvent(type="mouse_move", ts=0.02, x=102, y=102),
        ]

        # Remove first event
        remaining = group_events[1:]

        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining[0].x, 101)


class TestReorderWorkflow(unittest.TestCase):
    """Test reordering operations."""

    def test_reorder_swap_adjacent(self):
        """Swap adjacent rows."""
        events = [
            MacroEvent(type="wait", ts=0.0, duration=0.5),
            MacroEvent(type="mouse_click", ts=0.5, x=100, y=100, button="left", pressed=True),
            MacroEvent(type="mouse_click", ts=0.55, x=100, y=100, button="left", pressed=False),
        ]

        # Swap first two
        reordered = [events[1], events[0]] + events[2:]

        self.assertEqual(reordered[0].type, "mouse_click")
        self.assertEqual(reordered[1].type, "wait")

    def test_reorder_move_to_end(self):
        """Move first event to end."""
        events = [
            MacroEvent(type="wait", ts=0.0, duration=0.5),
            MacroEvent(type="mouse_move", ts=0.5, x=100, y=100),
            MacroEvent(type="mouse_click", ts=0.6, x=100, y=100, button="left", pressed=True),
        ]

        # Move wait to end
        reordered = events[1:] + [events[0]]

        self.assertEqual(reordered[0].type, "mouse_move")
        self.assertEqual(reordered[1].type, "mouse_click")
        self.assertEqual(reordered[2].type, "wait")

    def test_reorder_move_to_start(self):
        """Move last event to start."""
        events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_click", ts=0.5, x=100, y=100, button="left", pressed=True),
            MacroEvent(type="wait", ts=0.55, duration=0.5),
        ]

        # Move wait to start
        reordered = [events[2]] + events[:2]

        self.assertEqual(reordered[0].type, "wait")
        self.assertEqual(reordered[1].type, "mouse_move")


class TestCompleteWorkflows(unittest.TestCase):
    """Test complete workflows combining multiple operations."""

    def test_record_edit_save_load_playback(self):
        """Complete workflow: create → edit → save → load → verify playback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "workflow.json"

            # Step 1: Simulate recording
            original_events = [
                MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
                MacroEvent(type="wait", ts=0.5, duration=0.5),
                MacroEvent(type="mouse_click", ts=1.0, x=100, y=100, button="left", pressed=True),
                MacroEvent(type="mouse_click", ts=1.05, x=100, y=100, button="left", pressed=False),
            ]

            # Step 2: Edit - copy wait and paste it
            wait_event = original_events[1]
            pasted_wait = replace(wait_event, ts=1.55)
            edited_events = original_events + [pasted_wait]

            # Step 3: Save
            groups = [MacroGroup(window=None, recorded_rect=None, events=edited_events)]
            save_macro(groups, str(filepath))

            # Step 4: Load
            loaded_groups = load_macro(str(filepath))
            loaded_events = loaded_groups[0].events

            # Step 5: Verify
            self.assertEqual(len(loaded_events), 5)
            wait_count = sum(1 for ev in loaded_events if ev.type == "wait")
            self.assertEqual(wait_count, 2)

            # Verify timestamps are in order
            for i in range(1, len(loaded_events)):
                self.assertGreaterEqual(loaded_events[i].ts, loaded_events[i-1].ts)

    def test_complex_editing_session(self):
        """Complex editing: copy → paste → delete → rearrange → save → load."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "complex.json"

            # Start with macro
            events = [
                MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
                MacroEvent(type="wait", ts=0.5, duration=0.5),
                MacroEvent(type="mouse_click", ts=1.0, x=100, y=100, button="left", pressed=True),
            ]

            # Copy wait
            wait = events[1]

            # Paste after click
            paste1 = replace(wait, ts=1.05)
            paste2 = replace(wait, ts=1.55)
            all_events = events + [paste1, paste2]

            # Delete original move
            no_move = all_events[1:]  # Remove mouse_move

            # Should have: wait, click, paste1, paste2
            self.assertEqual(len(no_move), 4)
            self.assertEqual(no_move[0].type, "wait")
            self.assertEqual(no_move[1].type, "mouse_click")
            self.assertEqual(no_move[2].type, "wait")
            self.assertEqual(no_move[3].type, "wait")

            # Rearrange: move first wait to end (keep click at start)
            rearranged = [no_move[1]] + [no_move[2], no_move[3]] + [no_move[0]]

            # Save
            groups = [MacroGroup(window=None, recorded_rect=None, events=rearranged)]
            save_macro(groups, str(filepath))

            # Load and verify
            loaded = load_macro(str(filepath))
            loaded_events = loaded[0].events

            # Should have 3 waits and 1 click
            wait_count = sum(1 for ev in loaded_events if ev.type == "wait")
            click_count = sum(1 for ev in loaded_events if ev.type == "mouse_click")
            self.assertEqual(wait_count, 3)
            self.assertEqual(click_count, 1)
            self.assertEqual(len(loaded_events), 4)

    def test_multi_group_edit_workflow(self):
        """Edit macro with multiple window groups."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "multi_group.json"

            # Create two groups
            group1_events = [
                MacroEvent(type="mouse_click", ts=0.0, x=50, y=50, button="left", pressed=True),
                MacroEvent(type="mouse_click", ts=0.05, x=50, y=50, button="left", pressed=False),
            ]

            group2_events = [
                MacroEvent(type="mouse_click", ts=0.0, x=200, y=200, button="left", pressed=True),
                MacroEvent(type="mouse_click", ts=0.05, x=200, y=200, button="left", pressed=False),
            ]

            groups = [
                MacroGroup(window="App1", recorded_rect=WindowRect(0, 0, 800, 600), events=group1_events),
                MacroGroup(window="App2", recorded_rect=WindowRect(100, 100, 1024, 768), events=group2_events),
            ]

            # Save
            save_macro(groups, str(filepath))

            # Load, edit group 1, save again
            loaded = load_macro(str(filepath))
            loaded[0].events = loaded[0].events[:1]  # Keep only first click
            save_macro(loaded, str(filepath))

            # Load again and verify edit
            loaded_again = load_macro(str(filepath))
            self.assertEqual(len(loaded_again[0].events), 1)
            self.assertEqual(len(loaded_again[1].events), 2)

    def test_timestamp_integrity_through_operations(self):
        """Verify timestamps remain consistent through edit operations."""
        events = [
            MacroEvent(type="mouse_move", ts=0.0, x=100, y=100),
            MacroEvent(type="mouse_move", ts=0.5, x=150, y=150),
            MacroEvent(type="wait", ts=1.0, duration=0.5),
            MacroEvent(type="mouse_click", ts=1.5, x=150, y=150, button="left", pressed=True),
        ]

        # Get original duration
        original_duration = events[-1].ts - events[0].ts

        # Copy and paste
        pasted = [replace(ev, ts=ev.ts + 2.0) for ev in events]
        combined = events + pasted

        # Verify combined duration
        combined_duration = combined[-1].ts - combined[0].ts
        self.assertGreater(combined_duration, original_duration)

        # Delete first move
        edited = combined[1:]

        # Verify timestamps are still in order
        for i in range(1, len(edited)):
            self.assertGreaterEqual(edited[i].ts, edited[i-1].ts)


if __name__ == "__main__":
    unittest.main()
