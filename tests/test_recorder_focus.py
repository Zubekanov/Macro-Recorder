"""The recorder turns window-manager focus reports into window_focus markers.

Input listeners are mocked, so nothing touches the real keyboard or mouse; the
window manager is a fake whose foreground window the test flips."""

import threading
import unittest
from unittest import mock

from macro_recorder.event_types import EventType
from macro_recorder.recorder import Recorder
from macro_recorder.window_manager import NullWindowManager, WindowInfo


class _FakeWindowManager(NullWindowManager):
    def __init__(self):
        self.foreground = WindowInfo("Editor", 10, 20, 300, 200, hwnd=1)

    def get_foreground(self):
        return self.foreground


class TestRecorderFocusMarkers(unittest.TestCase):
    def test_focus_change_becomes_window_focus_group(self):
        wm = _FakeWindowManager()
        with mock.patch("macro_recorder.recorder.get_window_manager", return_value=wm), \
                mock.patch("macro_recorder.recorder.mouse.Listener"), \
                mock.patch("macro_recorder.recorder.keyboard.Listener"):
            recorder = Recorder()
            result = {}
            thread = threading.Thread(target=lambda: result.setdefault("groups", recorder.start()))
            thread.start()
            # Let the watcher observe the initial window, then switch.
            deadline = threading.Event()
            deadline.wait(0.15)
            wm.foreground = WindowInfo("Browser", 0, 0, 800, 600, hwnd=2)
            deadline.wait(0.25)
            recorder.stop()
            thread.join(timeout=3.0)
            self.assertFalse(thread.is_alive())

        groups = result["groups"]
        # The initial foreground is context, not a change: no group for "Editor".
        self.assertEqual([g.window for g in groups], ["Browser"])
        rect = groups[0].recorded_rect
        self.assertEqual((rect.left, rect.top, rect.width, rect.height), (0, 0, 800, 600))

    def test_no_window_manager_records_flat(self):
        with mock.patch("macro_recorder.recorder.get_window_manager", return_value=NullWindowManager()), \
                mock.patch("macro_recorder.recorder.mouse.Listener"), \
                mock.patch("macro_recorder.recorder.keyboard.Listener"):
            recorder = Recorder()
            thread = threading.Thread(target=recorder.start)
            thread.start()
            threading.Event().wait(0.1)
            recorder.stop()
            thread.join(timeout=3.0)
            self.assertFalse(thread.is_alive())
        self.assertEqual([e.type for e in recorder.get_events() if e.type == EventType.WINDOW_FOCUS], [])
