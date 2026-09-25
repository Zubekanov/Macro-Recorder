"""X11 window management: pure geometry helpers, backend selection, and a live
round trip against a Tk window.  The live tests skip without an X display."""

import os
import threading
import tkinter as tk
import unittest
from unittest import mock

import pytest

from macro_recorder import window_manager as wm_mod
from macro_recorder.window_manager import (
    NullWindowManager, WindowInfo, X11WindowManager, get_window_manager,
)


class TestGeometryHelpers(unittest.TestCase):
    def test_outer_rect_grows_by_frame_extents(self):
        from macro_recorder.x11 import outer_rect
        # client at (100, 80) 400x300, frame: left 2, right 2, top 30, bottom 2
        self.assertEqual(outer_rect(100, 80, 400, 300, [2, 2, 30, 2]), (98, 50, 404, 332))

    def test_outer_rect_without_extents_is_identity(self):
        from macro_recorder.x11 import outer_rect
        self.assertEqual(outer_rect(5, 6, 7, 8, None), (5, 6, 7, 8))
        self.assertEqual(outer_rect(5, 6, 7, 8, [1, 2]), (5, 6, 7, 8))

    def test_client_size_inverts_outer_rect(self):
        from macro_recorder.x11 import client_size, outer_rect
        ext = [3, 3, 25, 3]
        _, _, ow, oh = outer_rect(0, 0, 640, 480, ext)
        self.assertEqual(client_size(ow, oh, ext), (640, 480))
        self.assertEqual(client_size(2, 2, ext), (1, 1))   # never below 1x1


class TestBackendSelection(unittest.TestCase):
    def test_no_display_on_linux_gives_null(self):
        with mock.patch.object(wm_mod.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"DISPLAY": ""}):
            self.assertIsInstance(get_window_manager(), NullWindowManager)

    def test_unreachable_display_gives_null(self):
        with mock.patch.object(wm_mod.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"DISPLAY": ":250"}):
            self.assertIsInstance(get_window_manager(), NullWindowManager)

    def test_windows_without_pywin32_gives_null(self):
        with mock.patch.object(wm_mod.sys, "platform", "win32"), \
                mock.patch.dict("sys.modules", {"win32gui": None}):
            self.assertIsInstance(get_window_manager(), NullWindowManager)

    def test_default_watch_polls_get_foreground(self):
        class Flipping(NullWindowManager):
            def __init__(self):
                self.calls = 0

            def get_foreground(self):
                self.calls += 1
                return WindowInfo("A" if self.calls < 3 else "B", 0, 0, 1, 1, hwnd=self.calls // 3)

        seen = []
        stop = threading.Event()
        wm = Flipping()

        def cb(info):
            seen.append(info.title)
            if len(seen) == 2:
                stop.set()

        wm.watch_foreground(cb, stop, poll_interval=0.001)
        self.assertEqual(seen, ["A", "B"])


# ---------------------------------------------------------------------------
# Live tests against Xvfb / a real X session
# ---------------------------------------------------------------------------

@pytest.fixture
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip("no display: %s" % e)
    root.title("Macro Recorder Probe Alpha")
    root.geometry("300x200+120+80")
    root.update()
    yield root
    root.destroy()


def _x11_wm():
    wm = get_window_manager()
    if not isinstance(wm, X11WindowManager):
        pytest.skip("not running under X11")
    return wm


def test_find_window_reports_tk_geometry(tk_root):
    wm = _x11_wm()
    info = wm.find_window("probe alpha")             # case-insensitive substring
    assert info is not None
    assert info.title == "Macro Recorder Probe Alpha"
    assert (info.left, info.top, info.width, info.height) == (120, 80, 300, 200)
    assert wm.find_window(r"Probe\s+Alpha$", "regex").hwnd == info.hwnd
    assert wm.find_window("no such window") is None


def test_move_resize_and_activate(tk_root):
    wm = _x11_wm()
    info = wm.find_window("Macro Recorder Probe Alpha")
    assert wm.move_resize(info, 40, 30, 250, 150)
    tk_root.update()
    assert (tk_root.winfo_rootx(), tk_root.winfo_rooty()) == (40, 30)
    assert (tk_root.winfo_width(), tk_root.winfo_height()) == (250, 150)

    assert wm.set_foreground(info)
    fg = wm.get_foreground()
    assert fg is not None and fg.title == "Macro Recorder Probe Alpha"


def test_watch_foreground_reports_focus_changes(tk_root):
    wm = _x11_wm()
    seen = []
    stop = threading.Event()
    got_beta = threading.Event()

    def cb(info):
        seen.append(info.title)
        if info.title == "Macro Recorder Probe Beta":
            got_beta.set()

    alpha = wm.find_window("Macro Recorder Probe Alpha")
    wm.set_foreground(alpha)
    thread = threading.Thread(target=wm.watch_foreground, args=(cb, stop, 0.05), daemon=True)
    thread.start()

    beta = tk.Toplevel(tk_root)
    beta.title("Macro Recorder Probe Beta")
    beta.geometry("100x100+500+300")
    beta.update()
    wm.set_foreground(wm.find_window("Macro Recorder Probe Beta"))
    assert got_beta.wait(timeout=3.0), seen
    stop.set()
    thread.join(timeout=3.0)
    assert not thread.is_alive()
    assert seen[-1] == "Macro Recorder Probe Beta"
