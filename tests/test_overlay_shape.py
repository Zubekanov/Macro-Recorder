"""Overlay shaping: the pure rectangle cover and, under X11, the real window shape."""

import sys
import tkinter as tk
import unittest

import pytest

from macro_recorder.event_types import EventType
from macro_recorder.macro import MacroEvent
from macro_recorder.overlay_renderer import OverlayRenderer, shape_rectangles_for_items


def _covers(rects, x, y):
    return any(rx <= x < rx + rw and ry <= y < ry + rh for rx, ry, rw, rh in rects)


class TestShapeRectangles(unittest.TestCase):
    def test_oval_is_one_padded_box(self):
        rects = shape_rectangles_for_items([("oval", [10, 10, 30, 30], 2)], pad=3)
        self.assertEqual(len(rects), 1)
        self.assertEqual(rects[0], (6, 6, 29, 29))

    def test_rectangle_outline_is_four_strips_and_hollow(self):
        rects = shape_rectangles_for_items([("rectangle", [0, 0, 100, 60], 2)], pad=1)
        self.assertEqual(len(rects), 4)
        self.assertTrue(_covers(rects, 50, 0))     # top edge
        self.assertTrue(_covers(rects, 100, 30))   # right edge
        self.assertFalse(_covers(rects, 50, 30))   # interior stays transparent

    def test_line_is_sampled_along_its_length(self):
        rects = shape_rectangles_for_items([("line", [0, 0, 100, 0, 100, 50], 2)], step=4, pad=2)
        for x, y in ((0, 0), (37, 0), (100, 0), (100, 25), (100, 50)):
            self.assertTrue(_covers(rects, x, y), (x, y))
        self.assertFalse(_covers(rects, 50, 25))

    def test_empty_and_degenerate_input(self):
        self.assertEqual(shape_rectangles_for_items([]), [])
        self.assertEqual(len(shape_rectangles_for_items([("line", [5, 5], 1)])), 1)


@pytest.mark.skipif(sys.platform == "win32", reason="X11 shaping only")
def test_overlay_window_is_shaped_to_its_marks():
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip("no display: %s" % e)
    try:
        root.withdraw()
        overlay = OverlayRenderer(root)
        overlay.draw_click(MacroEvent(type=EventType.MOUSE_CLICK, ts=0.0, x=200, y=150,
                                      button="left", pressed=True))
        root.update()
        assert overlay._shaper is not None, "SHAPE extension expected on X11"
        from macro_recorder import x11
        d = overlay._shaper._display
        xid = overlay._shaper._xid
        bounding = x11.get_window_shape(d, xid, x11.SHAPE_BOUNDING)
        assert bounding and _covers(bounding, 200, 150)
        assert not _covers(bounding, 600, 600)
        assert x11.get_window_shape(d, xid, x11.SHAPE_INPUT) == []   # click-through
        overlay.hide()
        assert overlay._shaper is None and overlay._overlay is None
    finally:
        root.destroy()
