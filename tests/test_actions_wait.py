"""Playback of the wait actions against a fake play context."""

import random

import pytest

from macro_recorder.model import catalogue
from macro_recorder.model.actions import wait
from macro_recorder.model.catalogue import Abort, Flow, Jump, make
from macro_recorder.model.document import make_row
from tests.fake_play_context import FakePlayContext


def play(ctx, row):
    return catalogue.spec_for(row).play(ctx, row)


def row(type_, **params):
    return make_row(make(type_, **params))


@pytest.fixture
def ctx(monkeypatch):
    c = FakePlayContext()
    monkeypatch.setattr(wait, "clock", c.clock.now)
    return c


# ------------------------------------------------------------------------ wait

def test_fixed_wait_sleeps_scaled(ctx):
    assert play(ctx, row("wait", duration_ms=500)) is Flow.NEXT
    assert ctx.sleeps == [(500, True)]


def test_wait_expression(ctx):
    ctx.variables["t"] = 250
    play(ctx, row("wait", duration_ms="t"))
    assert ctx.sleeps == [(250, True)]


def test_random_wait_is_uniform_between_bounds(ctx, monkeypatch):
    monkeypatch.setattr(wait, "rng", random.Random(7))
    for _ in range(100):
        play(ctx, row("wait", duration_ms=100, random_max_ms=300))
    values = [ms for ms, _ in ctx.sleeps]
    assert all(100 <= v <= 300 for v in values)
    assert min(values) < 130 and max(values) > 270


def test_random_max_below_duration_is_fixed(ctx):
    play(ctx, row("wait", duration_ms=400, random_max_ms=200))
    assert ctx.sleeps == [(400, True)]


def test_zero_wait_and_stop(ctx):
    assert play(ctx, row("wait", duration_ms=0)) is Flow.NEXT
    assert ctx.sleeps == []
    ctx.stop_on_sleep = 1
    assert play(ctx, row("wait", duration_ms=10)) is Flow.STOP


# ------------------------------------------------------------------ wait_pixel

def test_pixel_already_matching_returns_without_sleeping(ctx):
    ctx.screen.pixels[(10, 20)] = (255, 255, 255)
    assert play(ctx, row("wait_pixel", x=10, y=20, colour="#ffffff")) is Flow.NEXT
    assert ctx.sleeps == []


def test_pixel_reads_framed_point(ctx):
    ctx.delta = (100, 50)
    ctx.screen.pixels[(110, 70)] = (1, 2, 3)
    assert play(ctx, row("wait_pixel", x=10, y=20, colour="#010203")) is Flow.NEXT


def test_pixel_polls_every_50_ms_unscaled_until_match(ctx):
    ctx.speed = 4.0
    reads = []

    def pixel(x, y):
        reads.append((x, y))
        return (255, 0, 0) if len(reads) >= 4 else (0, 0, 0)

    ctx.screen.pixel = pixel
    assert play(ctx, row("wait_pixel", x=1, y=1, colour="#ff0000")) is Flow.NEXT
    assert ctx.sleeps == [(50, False)] * 3
    assert len(reads) == 4


def test_pixel_tolerance_per_channel(ctx):
    ctx.screen.pixels[(0, 0)] = (110, 90, 100)
    assert play(ctx, row("wait_pixel", x=0, y=0, colour="#646464", colour_tolerance=10,
                         timeout_ms=0)) is Flow.NEXT
    result = play(ctx, row("wait_pixel", x=0, y=0, colour="#646464", colour_tolerance=9,
                           timeout_ms=0))
    assert isinstance(result, Abort)


def test_pixel_differs_mode(ctx):
    ctx.screen.pixels[(0, 0)] = (0, 0, 0)
    assert play(ctx, row("wait_pixel", x=0, y=0, colour="#ffffff", mode="differs")) is Flow.NEXT
    result = play(ctx, row("wait_pixel", x=0, y=0, colour="#000000", mode="differs", timeout_ms=100))
    assert isinstance(result, Abort)
    assert result.message == "pixel (0, 0) did not change from #000000 within 100 ms"


def test_pixel_timeout_abort_message_and_timing(ctx):
    result = play(ctx, row("wait_pixel", x=5, y=6, colour="#FF0000", timeout_ms=120))
    assert result == Abort("pixel (5, 6) did not become #ff0000 within 120 ms")
    assert ctx.sleeps == [(50, False), (50, False), (pytest.approx(20), False)]
    assert ctx.clock.now() == pytest.approx(0.12)


def test_pixel_timeout_continue_logs(ctx):
    result = play(ctx, row("wait_pixel", x=0, y=0, colour="#ffffff", timeout_ms=60,
                           on_timeout="continue"))
    assert result is Flow.NEXT
    assert "did not become #ffffff" in ctx.logs[0]


def test_pixel_timeout_goto_jumps(ctx):
    result = play(ctx, row("wait_pixel", x=0, y=0, colour="#ffffff", timeout_ms=60,
                           on_timeout="goto", target="retry"))
    assert result == Jump("retry")


def test_pixel_stop_while_polling(ctx):
    ctx.stop_on_sleep = 2
    assert play(ctx, row("wait_pixel", x=0, y=0, colour="#ffffff")) is Flow.STOP
    assert len(ctx.sleeps) == 2


def test_bad_colour_aborts(ctx):
    result = play(ctx, row("wait_pixel", x=0, y=0, colour="red"))
    assert isinstance(result, Abort) and "#rrggbb" in result.message


def test_colour_helpers():
    assert wait.parse_colour(" #FF8000 ") == (255, 128, 0)
    assert wait.format_colour((255, 128, 0)) == "#ff8000"
    with pytest.raises(ValueError):
        wait.parse_colour("#fff")
    assert wait.colour_matches((10, 10, 10), (12, 8, 10), 2)
    assert not wait.colour_matches((10, 10, 10), (13, 10, 10), 2)
