"""Playback of the mouse actions against a fake play context."""

import pytest

from macro_recorder.model import catalogue
from macro_recorder.model.catalogue import Flow, make
from macro_recorder.model.document import make_row
from tests.fake_play_context import FakePlayContext


def play(ctx, row):
    return catalogue.spec_for(row).play(ctx, row)


def row(type_, **params):
    return make_row(make(type_, **params))


def path_row(points, dt=0.02):
    return make_row(tuple(make("mouse_move", x=x, y=y, dt=0.0 if i == 0 else dt)
                          for i, (x, y) in enumerate(points)))


# ------------------------------------------------------------------ mouse_move

def test_single_move_sets_pointer_with_frame():
    ctx = FakePlayContext(delta=(10, -5))
    assert play(ctx, row("mouse_move", x=100, y=200)) is Flow.NEXT
    assert ctx.input.actions == [("move", 110, 195)]
    assert ctx.pointer_moves == []


def test_move_expression_resolves():
    ctx = FakePlayContext()
    ctx.variables.update(px=40, py=50)
    play(ctx, row("mouse_move", x="px", y="py"))
    assert ctx.input.actions == [("move", 40, 50)]


def test_path_goes_through_move_pointer_with_frame_and_dts():
    ctx = FakePlayContext(delta=(5, 5))
    r = path_row([(0, 0), (10, 0), (20, 10)], dt=0.05)
    assert play(ctx, r) is Flow.NEXT
    points, duration_ms, dts = ctx.pointer_moves[0]
    assert points == [(5, 5), (15, 5), (25, 15)]
    assert duration_ms == pytest.approx(100.0)
    assert dts == [0.0, 0.05, 0.05]


def test_path_stopped_returns_stop():
    ctx = FakePlayContext()
    ctx.pointer_stop = True
    assert play(ctx, path_row([(0, 0), (1, 1)])) is Flow.STOP


# ------------------------------------------------------------ mouse_move_timed

def test_timed_move_starts_at_first_point_then_moves_over_duration():
    ctx = FakePlayContext(delta=(1, 2))
    r = row("mouse_move_timed", x=10, y=20, x2=110, y2=220, duration_ms=750)
    assert play(ctx, r) is Flow.NEXT
    assert ctx.input.actions[0] == ("move", 11, 22)
    assert ctx.pointer_moves == [([(11, 22), (111, 222)], 750.0, None)]


def test_timed_move_stop():
    ctx = FakePlayContext()
    ctx.pointer_stop = True
    assert play(ctx, row("mouse_move_timed")) is Flow.STOP


# ----------------------------------------------------------------- mouse_click

def test_click_moves_then_presses_and_releases_with_hold():
    ctx = FakePlayContext(delta=(100, 0))
    r = row("mouse_click", x=10, y=20, hold_ms=40)
    assert play(ctx, r) is Flow.NEXT
    assert ctx.input.actions == [("move", 110, 20), ("press_button", "left"),
                                 ("release_button", "left")]
    assert ctx.sleeps == [(40, True)]
    assert ctx.held_buttons == []


def test_click_skips_move_within_two_pixels():
    ctx = FakePlayContext()
    ctx.input.move(11, 21)
    ctx.input.actions.clear()
    play(ctx, row("mouse_click", x=10, y=20))
    assert ctx.input.actions[0] == ("press_button", "left")


def test_click_moves_when_just_over_two_pixels():
    ctx = FakePlayContext()
    ctx.input.move(13, 20)
    ctx.input.actions.clear()
    play(ctx, row("mouse_click", x=10, y=20))
    assert ctx.input.actions[0] == ("move", 10, 20)


def test_triple_click_waits_interval_between_clicks_only():
    ctx = FakePlayContext()
    ctx.input.move(5, 5)
    play(ctx, row("mouse_click", x=5, y=5, button="right", count=3, hold_ms=30, interval_ms=70))
    presses = [a for a in ctx.input.actions if a[0] != "move"]
    assert presses == [("press_button", "right"), ("release_button", "right")] * 3
    assert ctx.sleeps == [(30, True), (70, True), (30, True), (70, True), (30, True)]


def test_click_timing_scales_with_speed():
    ctx = FakePlayContext(speed=2.0)
    play(ctx, row("mouse_click", x=0, y=0, hold_ms=100))
    assert ctx.clock.sleeps == [pytest.approx(0.05)]


def test_click_with_zero_hold_does_not_sleep():
    ctx = FakePlayContext()
    play(ctx, row("mouse_click", x=0, y=0, hold_ms=0))
    assert ctx.sleeps == []


def test_stop_mid_click_releases_the_button():
    ctx = FakePlayContext(stop_on_sleep=1)
    assert play(ctx, row("mouse_click", x=0, y=0, count=2)) is Flow.STOP
    assert ctx.input.actions[-2:] == [("press_button", "left"), ("release_button", "left")]
    assert ctx.input.held_buttons == set()
    assert ctx.held_buttons == []


def test_stop_in_interval_ends_after_first_click():
    ctx = FakePlayContext(stop_on_sleep=2)
    assert play(ctx, row("mouse_click", x=0, y=0, count=3)) is Flow.STOP
    assert [a for a in ctx.input.actions if a[0] == "press_button"] == [("press_button", "left")]
    assert ctx.input.held_buttons == set()


def test_click_down_and_up_halves_use_hold_bookkeeping():
    ctx = FakePlayContext()
    play(ctx, row("mouse_click", x=0, y=0, action="down", button="middle"))
    assert ctx.held_buttons == ["middle"]
    assert ctx.input.actions[-1] == ("press_button", "middle")
    play(ctx, row("mouse_click", x=50, y=0, action="up", button="middle"))
    assert ctx.held_buttons == []
    assert ctx.input.actions[-2:] == [("move", 50, 0), ("release_button", "middle")]
    assert ctx.sleeps == []


# ---------------------------------------------------------------- mouse_scroll

def test_scroll_without_position_does_not_move():
    ctx = FakePlayContext()
    assert play(ctx, row("mouse_scroll", dy=3, notch_ms=25)) is Flow.NEXT
    assert ctx.input.actions == [("scroll", 0, 1)] * 3
    assert ctx.sleeps == [(25, True), (25, True)]


def test_scroll_with_position_moves_first_with_frame():
    ctx = FakePlayContext(delta=(-10, 10))
    play(ctx, row("mouse_scroll", x=100, y=100, dx=-2, dy=-1))
    assert ctx.input.actions == [("move", 90, 110), ("scroll", 0, -1), ("scroll", -1, 0),
                                 ("scroll", -1, 0)]


def test_scroll_expression_and_zero():
    ctx = FakePlayContext()
    ctx.variables["n"] = 2
    play(ctx, row("mouse_scroll", dx=0, dy="n"))
    assert ctx.input.actions == [("scroll", 0, 1)] * 2
    ctx = FakePlayContext()
    play(ctx, row("mouse_scroll", dx=0, dy=0))
    assert ctx.input.actions == []


def test_scroll_stop_between_notches():
    ctx = FakePlayContext(stop_on_sleep=1)
    assert play(ctx, row("mouse_scroll", dy=5)) is Flow.STOP
    assert ctx.input.actions == [("scroll", 0, 1)]


# ------------------------------------------------------------------- describe

def test_specs_keep_their_overlays_and_describe():
    r = row("mouse_click", x=1, y=2, count=2)
    spec = catalogue.spec_for(r)
    assert spec.describe(r) == "left double-click at (1, 2)"
    assert spec.overlay(r).kind == "click"
    assert catalogue.spec("mouse_scroll").describe(row("mouse_scroll", dy=-2)) == "scroll up 2"
