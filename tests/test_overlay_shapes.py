"""Tk-free overlay geometry: the items each shape draws and their SHAPE cover."""

from __future__ import annotations

from macro_recorder.gui.overlay import (
    CLICK_RADIUS, DOT_RADIUS, MARK_COLOUR, START_COLOUR, clip_rectangles, overlay_items,
    shape_rectangles_for_items,
)
from macro_recorder.model.protocols import OverlayShape


def covers(rects, x, y):
    return any(rx <= x < rx + rw and ry <= y < ry + rh for rx, ry, rw, rh in rects)


def cover(shape):
    return shape_rectangles_for_items((i.kind, i.coords, i.width) for i in overlay_items(shape))


# ------------------------------------------------- ported from test_overlay_shape.py

def test_oval_is_one_padded_box():
    rects = shape_rectangles_for_items([("oval", [10, 10, 30, 30], 2)], pad=3)
    assert rects == [(6, 6, 29, 29)]


def test_rectangle_outline_is_four_strips_and_hollow():
    rects = shape_rectangles_for_items([("rectangle", [0, 0, 100, 60], 2)], pad=1)
    assert len(rects) == 4
    assert covers(rects, 50, 0)         # top edge
    assert covers(rects, 100, 30)       # right edge
    assert not covers(rects, 50, 30)    # the interior stays transparent


def test_line_is_sampled_along_its_length():
    rects = shape_rectangles_for_items([("line", [0, 0, 100, 0, 100, 50], 2)], step=4, pad=2)
    for x, y in ((0, 0), (37, 0), (100, 0), (100, 25), (100, 50)):
        assert covers(rects, x, y), (x, y)
    assert not covers(rects, 50, 25)


def test_empty_and_degenerate_input():
    assert shape_rectangles_for_items([]) == []
    assert len(shape_rectangles_for_items([("line", [5, 5], 1)])) == 1


# ------------------------------------------------- items per shape kind

def test_nothing_for_none_or_no_points():
    assert overlay_items(None) == []
    assert overlay_items(OverlayShape("point", ())) == []
    assert overlay_items(OverlayShape("unknown", ((1, 2),))) == []


def test_point_is_one_dot():
    (item,) = overlay_items(OverlayShape("point", ((50, 60),)))
    assert item.kind == "oval" and item.fill == MARK_COLOUR
    assert item.coords == (50 - DOT_RADIUS, 60 - DOT_RADIUS, 50 + DOT_RADIUS, 60 + DOT_RADIUS)


def test_click_is_a_circle_and_a_cross():
    items = overlay_items(OverlayShape("click", ((200, 150),)))
    assert [i.kind for i in items] == ["oval", "line", "line"]
    rects = cover(OverlayShape("click", ((200, 150),)))
    assert covers(rects, 200, 150)
    assert covers(rects, 200 + CLICK_RADIUS, 150)
    assert not covers(rects, 600, 600)


def test_line_has_start_and_end_dots():
    items = overlay_items(OverlayShape("line", ((0, 0), (100, 0))))
    assert [i.kind for i in items] == ["line", "oval", "oval"]
    assert items[1].fill == START_COLOUR and items[2].fill == MARK_COLOUR
    assert items[0].coords == (0, 0, 100, 0)


def test_path_runs_through_every_point():
    shape = OverlayShape("path", ((10, 10), (50, 10), (50, 80)))
    items = overlay_items(shape)
    assert items[0].coords == (10, 10, 50, 10, 50, 80)
    rects = cover(shape)
    for x, y in ((10, 10), (30, 10), (50, 45), (50, 80)):
        assert covers(rects, x, y)
    assert not covers(rects, 20, 60)


def test_single_point_path_is_a_start_dot():
    items = overlay_items(OverlayShape("path", ((10, 10),)))
    assert [(i.kind, i.fill) for i in items] == [("oval", START_COLOUR)]


def test_rect_is_a_hollow_outline():
    rects = cover(OverlayShape("rect", ((100, 100), (400, 300))))
    assert covers(rects, 250, 100) and covers(rects, 400, 200)
    assert not covers(rects, 250, 200)


def test_clip_rectangles():
    rects = [(-10, -10, 20, 20), (100, 100, 10, 10), (100, 100, 10, 10), (5000, 5, 10, 10),
             (1910, 1070, 20, 20)]
    assert clip_rectangles(rects, 1920, 1080) == [(0, 0, 10, 10), (100, 100, 10, 10),
                                                  (1910, 1070, 10, 10)]
