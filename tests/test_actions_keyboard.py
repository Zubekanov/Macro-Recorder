"""Playback of the keyboard actions against a fake play context."""

import random

import pytest

from macro_recorder.model import catalogue
from macro_recorder.model.actions import keyboard
from macro_recorder.model.catalogue import Abort, Flow, make
from macro_recorder.model.document import make_row
from tests.fake_play_context import FakePlayContext


def play(ctx, row):
    return catalogue.spec_for(row).play(ctx, row)


def row(type_, **params):
    return make_row(make(type_, **params))


# ------------------------------------------------------------------- key_combo

def test_combo_presses_modifiers_then_key_and_releases_in_reverse():
    ctx = FakePlayContext()
    assert play(ctx, row("key_combo", keys="ctrl+shift+s", hold_ms=60)) is Flow.NEXT
    assert ctx.input.actions == [
        ("press_key", "Control_L"), ("press_key", "Shift_L"), ("press_key", "s"),
        ("release_key", "s"), ("release_key", "Shift_L"), ("release_key", "Control_L"),
    ]
    assert ctx.sleeps == [(60, True)]
    assert ctx.held_keys == []


def test_single_key_combo_and_plus_key():
    ctx = FakePlayContext()
    play(ctx, row("key_combo", keys="Return", hold_ms=0))
    assert ctx.input.actions == [("press_key", "Return"), ("release_key", "Return")]
    assert ctx.sleeps == []
    ctx = FakePlayContext()
    play(ctx, row("key_combo", keys="ctrl++"))
    assert ctx.input.actions[:2] == [("press_key", "Control_L"), ("press_key", "plus")]


def test_stop_during_combo_hold_releases_everything():
    ctx = FakePlayContext(stop_on_sleep=1)
    assert play(ctx, row("key_combo", keys="alt+F4")) is Flow.STOP
    assert ctx.input.actions[-2:] == [("release_key", "F4"), ("release_key", "Alt_L")]
    assert ctx.input.held_keys == set()


def test_bad_combo_aborts_without_input():
    ctx = FakePlayContext()
    result = play(ctx, row("key_combo", keys="a+b"))
    assert isinstance(result, Abort)
    assert "modifiers" in result.message
    assert ctx.input.actions == []


# ------------------------------------------------------- key_press, key_release

def test_press_and_release_use_hold_bookkeeping():
    ctx = FakePlayContext()
    assert play(ctx, row("key_press", key="Shift_L")) is Flow.NEXT
    assert ctx.held_keys == ["Shift_L"]
    assert play(ctx, row("key_release", key="Shift_L")) is Flow.NEXT
    assert ctx.held_keys == []
    assert ctx.input.actions == [("press_key", "Shift_L"), ("release_key", "Shift_L")]


def test_press_normalises_aliases():
    ctx = FakePlayContext()
    play(ctx, row("key_press", key="ctrl"))
    assert ctx.held_keys == ["Control_L"]


def test_empty_key_aborts():
    ctx = FakePlayContext()
    assert isinstance(play(ctx, row("key_press", key="")), Abort)
    assert isinstance(play(ctx, row("key_release", key=" ")), Abort)
    assert ctx.input.actions == []


def test_combo_over_a_key_held_by_key_press_keeps_it_held():
    ctx = FakePlayContext()
    play(ctx, row("key_press", key="Shift_L"))
    play(ctx, row("key_combo", keys="shift+a", hold_ms=0))
    assert ctx.held_keys == ["Shift_L"]
    assert ctx.input.actions == [("press_key", "Shift_L"), ("press_key", "a"), ("release_key", "a")]
    play(ctx, row("key_release", key="Shift_L"))
    assert ctx.held_keys == []
    assert ctx.input.actions[-1] == ("release_key", "Shift_L")


# ------------------------------------------------------------------- type_text

def typed(ctx):
    return "".join(a[1] for a in ctx.input.actions if a[0] == "type_char")


def test_type_text_renders_template_and_types_each_char():
    ctx = FakePlayContext()
    ctx.variables["who"] = "Sam"
    assert play(ctx, row("type_text", text="Hi {who} {{x}}")) is Flow.NEXT
    assert typed(ctx) == "Hi Sam {x}"
    assert ctx.sleeps == []


def test_char_delay_after_each_character_scaled():
    ctx = FakePlayContext(speed=2.0)
    play(ctx, row("type_text", text="abc", char_delay_ms=40))
    assert ctx.sleeps == [(40, True)] * 3
    assert ctx.clock.sleeps == [pytest.approx(0.02)] * 3


def test_duration_spreads_over_text():
    ctx = FakePlayContext()
    play(ctx, row("type_text", text="abcd", duration_ms=200))
    assert ctx.sleeps == [(50, True)] * 4


def test_char_delay_wins_over_duration():
    assert keyboard.char_delays(2, 10, 1000, False, random.Random(0)) == [10, 10]


def test_humanise_stays_within_fifty_percent(monkeypatch):
    monkeypatch.setattr(keyboard, "rng", random.Random(1234))
    ctx = FakePlayContext()
    play(ctx, row("type_text", text="x" * 200, char_delay_ms=100, humanise=True))
    delays = [ms for ms, _ in ctx.sleeps]
    assert len(delays) == 200
    assert all(50 <= d <= 150 for d in delays)
    assert min(delays) < 60 and max(delays) > 140
    assert len(set(delays)) > 100


def test_humanise_bounds_are_exact():
    class Edge:
        def __init__(self, value):
            self.value = value

        def uniform(self, a, b):
            return a if self.value == "low" else b

    assert keyboard.char_delays(1, 100, 0, True, Edge("low")) == [50]
    assert keyboard.char_delays(1, 100, 0, True, Edge("high")) == [150]


def test_humanise_without_delay_types_at_once():
    assert keyboard.char_delays(3, 0, 0, True, random.Random(0)) == [0.0, 0.0, 0.0]


def test_humanise_is_enabled_only_with_a_delay():
    field = next(f for f in keyboard.TYPE_TEXT.fields if f.keys == ("humanise",))
    assert not field.enabled_when({"char_delay_ms": 0, "duration_ms": 0, "humanise": True})
    assert field.enabled_when({"char_delay_ms": 20, "duration_ms": 0})
    assert field.enabled_when({"char_delay_ms": 0, "duration_ms": "t * 2"})


def test_stop_during_typing():
    ctx = FakePlayContext(stop_on_sleep=2)
    assert play(ctx, row("type_text", text="hello", char_delay_ms=10)) is Flow.STOP
    assert typed(ctx) == "he"


def test_stop_flag_checked_between_undelayed_characters():
    ctx = FakePlayContext()
    ctx.stop()
    assert play(ctx, row("type_text", text="hello")) is Flow.STOP
    assert typed(ctx) == ""
