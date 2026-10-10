"""The playback engine, driven with fake platform objects and a fake clock.

The action modules' ``play`` functions belong to other work packages, so
these tests swap minimal ones into the catalogue (see :func:`plays`).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
import threading
import time

import pytest

if importlib.util.find_spec("macro_recorder.engine.expressions") is None:
    # engine/expressions.py arrives with the model-core work package; until it
    # is merged the previous evaluator, which has the same three names, stands in.
    import macro_recorder.expressions as _previous_expressions
    sys.modules["macro_recorder.engine.expressions"] = _previous_expressions

from macro_recorder.engine import player as player_mod
from macro_recorder.engine.player import REPEAT_COUNTER, Player
from macro_recorder.model import catalogue
from macro_recorder.model.actions.variables import DYNAMIC_VALUES
from macro_recorder.model.catalogue import Abort, Flow, Jump, make
from macro_recorder.model.document import MacroDocument, make_row
from macro_recorder.model.protocols import Monitor, Rect
from tests.conftest import (
    FakeClock, FakeHost, FakeInput, FakeScreen, FakeStopKey, FakeWindows, window,
)


# ---------------------------------------------------------------------------
# Minimal action plays
# ---------------------------------------------------------------------------

def _wait(ctx, row):
    return Flow.STOP if ctx.sleep(ctx.resolve(row.event["duration_ms"])) else Flow.NEXT


def _click(ctx, row):
    x, y = ctx.coord(row.event["x"], row.event["y"])
    ctx.input.move(x, y)
    ctx.hold_button(row.event["button"])
    ctx.unhold_button(row.event["button"])
    return Flow.NEXT


def _move(ctx, row):
    points = [ctx.coord(e["x"], e["y"]) for e in row.events]
    if row.is_path:
        return Flow.STOP if ctx.move_pointer(points, row.span_s * 1000, [e.dt for e in row.events]) \
            else Flow.NEXT
    ctx.input.move(*points[0])
    return Flow.NEXT


def _var_set(ctx, row):
    ctx.variables[row.event["var_name"]] = ctx.resolve(row.event["expr"])
    return Flow.NEXT


def _goto(ctx, row):
    return Jump(row.event["target"])


def _if(ctx, row):
    if ctx.resolve(row.event["expr"]):
        return Jump(row.event["target"])
    return Jump(row.event["else_target"]) if row.event["else_target"] else Flow.NEXT


def _repeat(ctx, row):
    state = ctx.row_state(row.id)
    if state.get(REPEAT_COUNTER, 0) == 0:
        state["count"] = ctx.resolve_int(row.event["count"])
    state[REPEAT_COUNTER] = state.get(REPEAT_COUNTER, 0) + 1
    if state[REPEAT_COUNTER] < state["count"]:
        return Jump(row.event["target"])
    state[REPEAT_COUNTER] = 0
    return Flow.NEXT


def _key_press(ctx, row):
    ctx.hold_key(row.event["key"])
    return Flow.NEXT


def _key_release(ctx, row):
    ctx.unhold_key(row.event["key"])
    return Flow.NEXT


def _window(ctx, row):
    return ctx.enter_window(row)


def _message(ctx, row):
    answer = ctx.host.ask(row.event["title"], ctx.render(row.event["text"]), row.event["buttons"])
    if answer is None:
        return Flow.STOP
    if row.event["var_name"]:
        ctx.variables[row.event["var_name"]] = answer
    return Flow.NEXT


def _exec(ctx, row):
    code = ctx.run_command(row.event["command"], row.event["wait_exit"], ctx.resolve(row.event["timeout_ms"]))
    if row.event["var_name"]:
        ctx.variables[row.event["var_name"]] = code
    return Flow.NEXT


PLAYS = {
    "wait": _wait, "mouse_click": _click, "mouse_move": _move, "var_set": _var_set,
    "goto": _goto, "if_else": _if, "repeat": _repeat, "key_press": _key_press,
    "key_release": _key_release, "window_focus": _window, "message_box": _message,
    "exec_program": _exec,
}


@pytest.fixture(autouse=True)
def plays(monkeypatch):
    """Swap the minimal plays into the catalogue; returns a function to add more."""
    def install(type_, play):
        spec = catalogue.spec(type_)
        monkeypatch.setitem(catalogue._registry, type_, dataclasses.replace(spec, play=play))
    for type_, play in PLAYS.items():
        install(type_, play)
    return install


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def wait(ms, **fields):
    return make_row(make("wait", duration_ms=ms), **fields)


def click(x, y, button="left", **fields):
    return make_row(make("mouse_click", x=x, y=y, button=button), **fields)


def var(name, expr, **fields):
    return make_row(make("var_set", var_name=name, expr=expr), **fields)


def goto(target, **fields):
    return make_row(make("goto", target=target), **fields)


def if_(expr, target, else_target=None, **fields):
    return make_row(make("if_else", expr=expr, target=target, else_target=else_target), **fields)


def win(title, rect=(100, 80, 900, 600), client=(100, 110, 900, 570), **params):
    fields = {k: params.pop(k) for k in ("label", "enabled", "breakpoint", "delay_ms") if k in params}
    return make_row(make("window_focus", window=title, rect=rect, client_rect=client, **params), **fields)


def path(points, dt):
    return make_row(tuple(make("mouse_move", x=x, y=y, dt=0.0 if i == 0 else dt)
                          for i, (x, y) in enumerate(points)))


class Rig:
    """A player with fakes, ready to play."""

    def __init__(self, rows, settings, *, host=None, windows=None, clock=None, path_mode="recorded",
                 filter=(), stop_key=None, doc_path=None):
        self.document = MacroDocument(rows, filter=filter, path_mode=path_mode, path=doc_path)
        self.host = host or FakeHost()
        self.input = FakeInput()
        self.windows = windows if windows is not None else FakeWindows()
        self.screen = FakeScreen()
        self.clock = clock or FakeClock()
        self.player = Player(self.document, settings, self.host, self.input, self.windows,
                             self.screen, clock=self.clock, stop_key_watcher=stop_key)

    def play(self):
        self.player.play()
        return self

    @property
    def error(self):
        assert len(self.host.finished) == 1
        return self.host.finished[0]

    def clicks(self):
        return [a for a in self.input.actions if a[0] == "move"]


@pytest.fixture
def no_spacing(settings):
    settings.playback.min_spacing_ms = 0
    return settings


# ---------------------------------------------------------------------------
# Linear run and timing
# ---------------------------------------------------------------------------

def test_linear_run_with_delays_and_min_spacing(settings):
    rows = [wait(100), click(10, 20, delay_ms=50), click(30, 40), var("x", "1 + 2")]
    rig = Rig(rows, settings).play()
    assert rig.error is None
    # 10 (spacing) + 100 (wait) + 50 (delay) + 10 + 10 (spacing)
    assert rig.clock.t == pytest.approx(0.180)
    assert rig.host.rows == [r.id for r in rows]
    assert rig.host.logs == ["[1] 100 ms", "[2] left click at (10, 20)",
                             "[3] left click at (30, 40)", "[4] x = 1 + 2"]
    assert rig.input.actions == [
        ("move", 10, 20), ("press_button", "left"), ("release_button", "left"),
        ("move", 30, 40), ("press_button", "left"), ("release_button", "left"),
    ]
    assert rig.player.variables["x"] == 3


def test_schedule_absorbs_small_lateness_but_not_large(settings, plays):
    def slow(ms):
        def play(ctx, row):
            ctx._clock.advance(ms / 1000.0)      # unscheduled work
            return Flow.NEXT
        return play

    clock = FakeClock()
    rows = [var("a", "0"), wait(100)]
    plays("var_set", slow(30))
    Rig(rows, settings, clock=clock).play()
    # 10 spacing, 30 work, then the wait's 10 ms spacing and 100 ms are due
    # from the schedule: 30 ms late is absorbed.
    assert clock.t == pytest.approx(0.010 + 0.030 + 0.110 - 0.030)

    clock = FakeClock()
    plays("var_set", slow(500))
    Rig(rows, settings, clock=clock).play()
    # 500 ms late is more than the slack: the clock resets and the delays run in full.
    assert clock.t == pytest.approx(0.010 + 0.500 + 0.110)


def test_speed_table(settings, plays):
    settings.playback.speed = 2.0
    unscaled = []

    def both(ctx, row):
        start = ctx._clock.now()
        ctx.sleep(1000)
        mid = ctx._clock.now()
        ctx.sleep(1000, scaled=False)
        unscaled.append((mid - start, ctx._clock.now() - mid))
        return Flow.NEXT

    plays("var_set", both)
    rig = Rig([wait(400, delay_ms=100), var("x", "0")], settings).play()
    # Row delay 100 -> 50, wait 400 -> 200, min spacing 10 -> 5; ctx.sleep
    # scaled 1000 -> 500, unscaled stays 1000.
    assert unscaled == [(pytest.approx(0.5), pytest.approx(1.0))]
    assert rig.clock.t == pytest.approx(0.050 + 0.200 + 0.005 + 1.5)


def test_speed_does_not_scale_window_timeout_or_settle(settings):
    settings.playback.speed = 4.0
    windows = FakeWindows([])
    rows = [win("gedit", timeout_ms=1000, on_missing="skip")]
    rig = Rig(rows, settings, windows=windows).play()
    assert rig.error is None
    assert rig.clock.t == pytest.approx(0.010 / 4 + 1.0)


def test_speed_must_be_positive(settings):
    settings.playback.speed = 0
    with pytest.raises(ValueError):
        Rig([], settings)


def test_burst_guard_sleeps_after_ten_thousand_rows(no_spacing):
    rows = [var("c", "0"), var("c", "c + 1", label="top"), if_("c < 6000", "top")]
    rig = Rig(rows, no_spacing).play()
    assert rig.error is None
    assert rig.player.variables["c"] == 6000
    # Rows 10 001 onwards sleep 1 ms each until a second has passed, which
    # starts a new count.
    throttled = [s for s in rig.clock.sleeps if s == pytest.approx(0.001)]
    assert 1000 <= len(throttled) <= 1001


# ---------------------------------------------------------------------------
# Control flow
# ---------------------------------------------------------------------------

def test_jump_to_label_loops(no_spacing):
    rows = [var("n", "0"), var("n", "n + 1", label="loop"), if_("n < 3", "loop"),
            var("done", "1"), goto("End"), var("never", "1")]
    rig = Rig(rows, no_spacing).play()
    assert rig.error is None
    v = rig.player.variables
    assert v["n"] == 3 and v["done"] == 1 and "never" not in v
    assert rig.host.logs.count("[2] n = n + 1") == 3


def test_jump_to_start(no_spacing):
    rows = [if_("started", "body"), var("started", "1"), var("c", "0"),
            var("c", "c + 1", label="body"), if_("c < 3", "Start")]
    rig = Rig(rows, no_spacing)
    rig.player.variables["started"] = 0
    rig.play()
    assert rig.error is None
    assert rig.player.variables["c"] == 3


def test_skipped_rows_and_jump_to_skipped_row(no_spacing):
    rows = [goto("off"), click(1, 1), click(2, 2, label="off", enabled=False),
            make_row(make("mouse_click", x=3, y=3)), wait(5), var("x", "1")]
    rig = Rig(rows, no_spacing, filter=("wait",)).play()
    assert rig.error is None
    played = [rows[0].id, rows[3].id, rows[5].id]
    assert rig.host.rows == played
    assert rig.clicks() == [("move", 3, 3)]


def test_jump_to_skipped_last_row_ends_pass(no_spacing):
    rows = [goto("tail"), click(1, 1), click(2, 2, label="tail", enabled=False)]
    rig = Rig(rows, no_spacing).play()
    assert rig.error is None
    assert rig.clicks() == []


def test_missing_label_aborts(no_spacing):
    rows = [var("x", "1"), goto("nowhere"), var("y", "2")]
    rig = Rig(rows, no_spacing).play()
    assert rig.error == "Row 2: no row is labelled 'nowhere'"
    assert "y" not in rig.player.variables


def test_abort_and_errors_name_the_row(no_spacing, plays):
    rig = Rig([var("x", "undefined_name + 1")], no_spacing).play()
    assert rig.error.startswith("Row 1: ") and "undefined_name" in rig.error

    plays("wait", lambda ctx, row: Abort("pixel did not change"))
    rig = Rig([var("x", "1"), wait(1)], no_spacing).play()
    assert rig.error == "Row 2: pixel did not change"

    plays("wait", lambda ctx, row: Abort("Row 2: already numbered"))
    rig = Rig([var("x", "1"), wait(1)], no_spacing).play()
    assert rig.error == "Row 2: already numbered"

    def broken(ctx, row):
        raise KeyError("oops")
    plays("wait", broken)
    rig = Rig([wait(1)], no_spacing).play()
    assert rig.error == "Row 1: KeyError: 'oops'"


def test_flow_stop_ends_without_error(no_spacing, plays):
    plays("wait", lambda ctx, row: Flow.STOP)
    rows = [wait(1), var("x", "1")]
    rig = Rig(rows, no_spacing)
    rig.player.variables["x"] = 0
    rig.play()
    assert rig.error is None and rig.player.variables["x"] == 0


def test_repeat_passes_and_iteration(no_spacing):
    no_spacing.playback.repeat = 3
    rig = Rig([var("it", "iteration"), var("n", "ex_num")], no_spacing).play()
    v = rig.player.variables
    assert v["it"] == 3 and v["n"] == 6
    assert len(rig.host.rows) == 6


def test_repeat_zero_runs_until_stopped(no_spacing, plays):
    no_spacing.playback.repeat = 0
    passes = []

    def count(ctx, row):
        passes.append(ctx.variables["iteration"])
        if len(passes) == 5:
            rig.player.stop()
        return Flow.NEXT
    plays("wait", count)
    rig = Rig([wait(1)], no_spacing)
    rig.play()
    assert rig.error is None
    assert passes == [1, 2, 3, 4, 5]


def test_repeat_rows_counters_and_loop_value(no_spacing, plays):
    no_spacing.playback.repeat = 2
    loops = []
    rows = [var("total", "0"), var("l", "loop", label="body"),
            make_row(make("repeat", target="body", count=3)), var("after", "loop")]

    def record(ctx, row):
        result = _var_set(ctx, row)
        if row.event["var_name"] == "l":
            loops.append(ctx.variables["l"])
        return result
    plays("var_set", record)
    rig = Rig(rows, no_spacing).play()
    # Counters reset at each pass, so both passes run the body three times.
    assert loops == [1, 2, 3, 1, 2, 3]
    assert rig.player.variables["after"] == 0


# ---------------------------------------------------------------------------
# Window handling
# ---------------------------------------------------------------------------

def moved_xed():
    """xed whose client area is now at (300, 230); it was recorded at (100, 110)."""
    return FakeWindows([window(0x100, "xed", left=300, top=200, width=900, height=600)])


def test_window_delta_applies_to_numbers_and_expression_origin(no_spacing):
    rows = [win("xed", restore_rect="no"), click(150, 160), click("15", "20"),
            var("mx", "mouse_x"), var("wx", "win_x"), var("ww", "win_w"), click(5, 5, label="out_of_group")]
    rows.insert(6, make_row(make("window_focus", window="xed"), enabled=False))
    rig = Rig(rows, no_spacing, windows=moved_xed()).play()
    assert rig.error is None
    assert rig.clicks() == [("move", 350, 280), ("move", 315, 250), ("move", 5, 5)]
    v = rig.player.variables
    assert v["mx"] == 15 and v["wx"] == 300 and v["ww"] == 900
    assert ("activate", 0x100) in rig.windows.calls
    assert not any(c[0] == "move" for c in rig.windows.calls)


def test_restore_moves_then_activates_and_delta_is_zero(no_spacing):
    rows = [win("xed"), click(150, 160)]
    rig = Rig(rows, no_spacing, windows=moved_xed()).play()
    assert rig.error is None
    assert [c[0] for c in rig.windows.calls] == ["move", "activate"]
    assert rig.windows.calls[0][2] == Rect(100, 80, 900, 600)
    assert rig.clicks() == [("move", 150, 160)]


def test_restore_rect_setting_and_off_screen_rect(no_spacing):
    no_spacing.playback.restore_rect = False
    rig = Rig([win("xed")], no_spacing, windows=moved_xed()).play()
    assert [c[0] for c in rig.windows.calls] == ["activate"]

    no_spacing.playback.restore_rect = True
    rig = Rig([win("xed", rect=(3000, 0, 900, 600))], no_spacing, windows=moved_xed()).play()
    assert [c[0] for c in rig.windows.calls] == ["activate"]
    assert any("off-screen" in line for line in rig.host.logs)


def test_maximised_window_is_maximised_not_moved(no_spacing):
    rows = [win("xed", maximised=True)]
    rig = Rig(rows, no_spacing, windows=moved_xed()).play()
    assert [c[0] for c in rig.windows.calls] == ["maximise", "activate"]


def test_size_mismatch_is_logged(no_spacing):
    windows = moved_xed()
    original = windows.move
    windows.move = lambda w, rect, settle_ms=1000: original(w, Rect(rect.left, rect.top, 500, 400), settle_ms)
    rig = Rig([win("xed")], no_spacing, windows=windows).play()
    assert "[1] window 'xed' is 500x400, recorded 900x600" in rig.host.logs


@pytest.mark.parametrize("policy", ["skip", "default"])
def test_missing_window_skip(no_spacing, policy):
    no_spacing.playback.on_missing = "skip"
    rows = [win("gedit", timeout_ms=500, on_missing=policy), click(1, 1), click(2, 2),
            win("xed", restore_rect="no"), click(3, 3)]
    rig = Rig(rows, no_spacing, windows=moved_xed()).play()
    assert rig.error is None
    assert rig.clicks() == [("move", 203, 123)]
    assert rig.clock.t == pytest.approx(0.5)
    assert "[1] window 'gedit' not found, skipping its rows" in rig.host.logs


def test_missing_window_skip_to_end(no_spacing):
    rows = [win("gedit", timeout_ms=0), click(1, 1)]
    rig = Rig(rows, no_spacing, windows=FakeWindows([])).play()
    assert rig.error is None and rig.clicks() == []


def test_missing_window_halt(no_spacing):
    rows = [click(9, 9), win("gedit", timeout_ms=0, on_missing="halt"), click(1, 1)]
    rig = Rig(rows, no_spacing, windows=FakeWindows([])).play()
    assert rig.error == "Row 2: window 'gedit' was not found"
    assert rig.clicks() == [("move", 9, 9)]

    no_spacing.playback.on_missing = "halt"
    rig = Rig([win("gedit", timeout_ms=0)], no_spacing, windows=FakeWindows([])).play()
    assert rig.error == "Row 1: window 'gedit' was not found"


def test_missing_window_goto(no_spacing):
    rows = [win("gedit", timeout_ms=0, on_missing="goto", target="fallback"), click(1, 1),
            click(2, 2, label="inside"), make_row(make("window_focus", window="x"), enabled=False),
            click(3, 3, label="fallback"), var("c", "c + 1"), if_("c < 2", "inside")]
    rig = Rig(rows, no_spacing, windows=FakeWindows([]))
    rig.player.variables["c"] = 0
    rig.play()
    assert rig.error is None
    # The second jump lands in the missing window's group, so it skips to
    # the next window row (disabled) and plays on without a context.
    assert rig.clicks() == [("move", 3, 3), ("move", 3, 3)]


def test_failed_activation_counts_as_missing(no_spacing):
    windows = moved_xed()
    windows.refuse_activate = True
    rig = Rig([win("xed", timeout_ms=0, restore_rect="no"), click(1, 1)], no_spacing, windows=windows).play()
    assert rig.clicks() == []


def test_bad_regex_aborts(no_spacing):
    windows = moved_xed()

    def find(pattern, mode="substring"):
        raise ValueError("unbalanced parenthesis")
    windows.find = find
    rig = Rig([win("(xed", match_mode="regex")], no_spacing, windows=windows).play()
    assert rig.error.startswith("Row 1: bad window pattern '(xed'")


def test_jump_into_group_reenters_its_window(no_spacing):
    windows = FakeWindows([window(0x100, "xed", left=300, top=200), window(0x200, "term", 0, 0)])
    rows = [win("xed", restore_rect="no", client=(100, 110, 800, 570)), click(10, 10, label="inside"),
            win("term", restore_rect="no", client=(0, 30, 800, 570)), var("c", "c + 1"),
            if_("c < 2", "inside")]
    rig = Rig(rows, no_spacing, windows=windows)
    rig.player.variables["c"] = 0
    rig.play()
    assert rig.error is None
    assert rig.clicks() == [("move", 210, 130), ("move", 210, 130)]
    assert [c[1] for c in windows.calls if c[0] == "activate"] == [0x100, 0x200, 0x100, 0x200]


def test_remembered_missing_window_skips_without_waiting(no_spacing):
    finds = []
    windows = FakeWindows([window(0x200, "term")])
    original = windows.find
    windows.find = lambda p, mode="substring": finds.append(p) or original(p, mode)
    rows = [win("gedit", timeout_ms=1000), click(1, 1, label="inside"), win("term", restore_rect="no"),
            var("c", "c + 1"), if_("c < 3", "inside")]
    rig = Rig(rows, no_spacing, windows=windows)
    rig.player.variables["c"] = 0
    rig.play()
    assert rig.error is None
    assert rig.clicks() == []
    assert rig.clock.t == pytest.approx(1.0)
    assert sum(1 for p in finds if p == "gedit") == 5      # t = 0, 0.25, ..., 1.0
    assert rig.host.logs.count("[1] skipped: window 'gedit' was not found earlier") == 2


def test_launch_once_per_pass_then_settle(no_spacing, monkeypatch):
    no_spacing.playback.repeat = 2
    windows = FakeWindows([])
    launches = []

    def fake_run(command, cwd, wait, timeout_ms, stop_event):
        launches.append((command, cwd, wait))
        if len(launches) == 1:
            windows.windows.append(window(0x300, "gedit"))
        return None
    monkeypatch.setattr(player_mod.processes, "run", fake_run)
    rows = [win("gedit", launch="gedit", timeout_ms=1000, restore_rect="no"), click(1, 1)]
    rig = Rig(rows, no_spacing, windows=windows, doc_path="/tmp/macros/m.json").play()
    assert rig.error is None
    assert launches == [("gedit", "/tmp/macros", False)]
    # First lookup fails, launch, found at the 250 ms poll, then 1000 ms settle.
    assert rig.clock.t == pytest.approx(0.25 + 1.0)
    assert "[1] window 'gedit' not found, launching: gedit" in rig.host.logs
    assert len(rig.clicks()) == 2


def test_launch_runs_once_per_pass_while_missing(no_spacing, monkeypatch):
    no_spacing.playback.repeat = 3
    launches = []
    monkeypatch.setattr(player_mod.processes, "run", lambda *a: launches.append(a[0]))
    rows = [win("gedit", launch="gedit", timeout_ms=1000)]
    rig = Rig(rows, no_spacing, windows=FakeWindows([])).play()
    assert rig.error is None
    assert launches == ["gedit"] * 3


def test_launch_failure_aborts(no_spacing, monkeypatch):
    def refuse(*args):
        raise player_mod.processes.ProcessError("Could not run 'gedit': denied")
    monkeypatch.setattr(player_mod.processes, "run", refuse)
    rig = Rig([win("gedit", launch="gedit")], no_spacing, windows=FakeWindows([])).play()
    assert rig.error == "Row 1: Could not run 'gedit': denied"


def test_empty_title_uses_active_window(no_spacing):
    windows = moved_xed()
    rig = Rig([make_row(make("window_focus", window="", client_rect=(100, 110, 900, 570))), click(0, 0)],
              no_spacing, windows=windows).play()
    assert rig.error is None
    assert rig.clicks() == [("move", 200, 120)]


# ---------------------------------------------------------------------------
# Breakpoints, ask, stop
# ---------------------------------------------------------------------------

class PausingHost(FakeHost):
    """Advances the clock while paused, then resumes."""

    def __init__(self, clock, pause_s=5.0):
        super().__init__()
        self.clock = clock
        self.pause_s = pause_s
        self.player = None
        self.inputs_at_pause = []

    def on_paused(self, row_id, variables):
        super().on_paused(row_id, variables)
        self.inputs_at_pause.append(list(self.player.input.actions))
        self.clock.advance(self.pause_s)
        self.player.resume()


def test_breakpoint_pause_releases_and_represses(no_spacing):
    clock = FakeClock()
    host = PausingHost(clock)
    rows = [win("xed", restore_rect="no"), make_row(make("key_press", key="Shift_L")),
            click(1, 1, breakpoint=True), var("t", "rt_ms")]
    rig = Rig(rows, no_spacing, host=host, clock=clock, windows=moved_xed())
    host.player = rig.player
    rig.player.variables["seen"] = 7
    rig.play()
    assert rig.error is None
    assert host.paused[0][0] == rows[2].id
    snapshot = host.paused[0][1]
    assert snapshot["seen"] == 7 and snapshot["op_num"] == 3 and "rt_ms" in snapshot
    assert host.inputs_at_pause[0][-2:] == [("press_key", "Shift_L"), ("release_key", "Shift_L")]
    after = rig.input.actions[len(host.inputs_at_pause[0]):]
    assert after[0] == ("press_key", "Shift_L")
    assert [c[0] for c in rig.windows.calls] == ["activate", "activate"]
    assert rig.player.variables["t"] == pytest.approx(0.0)       # paused time excluded
    assert rig.input.held_keys == set()                          # released on exit


def test_breakpoint_resume_from_another_thread(no_spacing):
    host = FakeHost()
    paused = threading.Event()
    host.on_paused = lambda row_id, variables: paused.set()
    rig = Rig([click(1, 1, breakpoint=True)], no_spacing, host=host)
    thread = threading.Thread(target=rig.play)
    thread.start()
    assert paused.wait(5)
    assert rig.clicks() == []
    rig.player.resume()
    thread.join(5)
    assert not thread.is_alive()
    assert rig.error is None and rig.clicks() == [("move", 1, 1)]


def test_stop_while_paused(no_spacing):
    host = FakeHost()
    rig = Rig([click(1, 1, breakpoint=True)], no_spacing, host=host)
    host.on_paused = lambda row_id, variables: rig.player.stop()
    rig.play()
    assert rig.error is None and rig.clicks() == []


def test_breakpoint_set_while_paused_applies_to_the_current_run(no_spacing):
    host = FakeHost()
    rows = [click(1, 1, breakpoint=True), click(2, 2), click(3, 3, breakpoint=True)]
    rig = Rig(rows, no_spacing, host=host)
    paused_at = []

    def on_paused(row_id, variables):
        paused_at.append(row_id)
        if row_id == rows[0].id:
            rig.player.set_breakpoint(rows[1].id, True)
            rig.player.set_breakpoint(rows[2].id, False)
        rig.player.resume()
    host.on_paused = on_paused
    rig.play()
    assert rig.error is None
    assert paused_at == [rows[0].id, rows[1].id]
    assert len(rig.clicks()) == 3


def test_ask_round_trip(no_spacing):
    host = FakeHost(answers=["no"])
    rows = [win("xed", restore_rect="no"), make_row(make("key_press", key="Control_L")),
            make_row(make("message_box", title="Q", text="n is {n}", buttons="yes_no", var_name="ans"))]
    rig = Rig(rows, no_spacing, host=host, windows=moved_xed())
    rig.player.variables["n"] = 4
    rig.play()
    assert rig.error is None
    assert host.asks == [("Q", "n is 4", "yes_no")]
    assert rig.player.variables["ans"] == "no"
    assert rig.input.actions[:3] == [("press_key", "Control_L"), ("release_key", "Control_L"),
                                     ("press_key", "Control_L")]
    assert [c[0] for c in rig.windows.calls] == ["activate", "activate"]


def test_ask_answered_none_after_stop(no_spacing):
    host = FakeHost()
    rows = [make_row(make("message_box", text="x")), click(1, 1)]
    rig = Rig(rows, no_spacing, host=host)

    def ask(title, text, buttons):
        rig.player.stop()
        return "ok"
    host.ask = ask
    rig.play()
    assert rig.error is None and rig.clicks() == []


class StoppingClock(FakeClock):
    def __init__(self, at):
        super().__init__()
        self.at = at
        self.player = None

    def sleep(self, seconds):
        super().sleep(seconds)
        if self.t >= self.at:
            self.player.stop()


def test_stop_during_sleep_with_fake_clock(no_spacing):
    clock = StoppingClock(1.0)
    rows = [make_row(make("key_press", key="a")), wait(60_000), click(1, 1)]
    rig = Rig(rows, no_spacing, clock=clock)
    clock.player = rig.player
    rig.play()
    assert rig.error is None
    assert clock.t < 1.1
    assert rig.clicks() == []
    assert rig.input.actions[-1] == ("release_key", "a")


def test_stop_from_another_thread_with_real_clock(no_spacing):
    host = FakeHost()
    document = MacroDocument([wait(30_000)])
    player = Player(document, no_spacing, host, FakeInput(), FakeWindows(), FakeScreen())
    threading.Timer(0.1, player.stop).start()
    started = time.monotonic()
    player.play()
    assert time.monotonic() - started < 2
    assert host.finished == [None]


def test_stop_key_watcher(no_spacing, plays):
    watcher = FakeStopKey()
    keys = []

    def press_escape(ctx, row):
        keys.append(watcher.key)
        watcher.press()
        return Flow.NEXT
    plays("wait", press_escape)
    rig = Rig([wait(1), click(1, 1)], no_spacing, stop_key=watcher).play()
    assert keys == ["Escape"]
    assert rig.clicks() == []
    assert watcher.key is None                       # stopped on exit
    assert rig.error is None


def test_finished_is_last_and_input_released(no_spacing):
    order = []
    host = FakeHost()
    host.on_finished = lambda error: order.append(("finished", error))
    rig = Rig([make_row(make("key_press", key="b")), goto("missing")], no_spacing, host=host)
    original = rig.input.release_all
    rig.input.release_all = lambda: (order.append(("release_all",)), original())
    rig.play()
    assert order == [("release_all",), ("finished", "Row 2: no row is labelled 'missing'")]


def test_play_is_single_use(no_spacing):
    rig = Rig([], no_spacing).play()
    with pytest.raises(RuntimeError):
        rig.player.play()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def test_path_recorded_replays_points_with_timing(settings):
    pts = [(0, 0), (10, 0), (20, 0), (30, 0)]
    rig = Rig([path(pts, 0.1)], settings).play()
    assert rig.clicks() == [("move", x, y) for x, y in pts]
    assert rig.clock.t == pytest.approx(0.010 + 0.3)


def test_path_recorded_follows_window_delta(no_spacing):
    rows = [win("xed", restore_rect="no"), path([(100, 110), (110, 120)], 0.05)]
    rig = Rig(rows, no_spacing, windows=moved_xed()).play()
    assert rig.clicks() == [("move", 300, 230), ("move", 310, 240)]


def test_path_skips_points_when_late_but_reaches_the_end(no_spacing):
    rig = Rig([path([(i, 0) for i in range(11)], 0.01)], no_spacing)
    original = rig.input.move

    def slow_move(x, y):
        original(x, y)
        rig.clock.advance(0.03)              # each injection takes 30 ms
    rig.input.move = slow_move
    rig.play()
    moves = rig.clicks()
    assert moves[-1] == ("move", 10, 0)
    assert len(moves) < 11


def test_path_under_linear_mode_draws_from_pointer(no_spacing):
    rig = Rig([path([(0, 0), (50, 80), (100, 0)], 0.1)], no_spacing, path_mode="linear")
    rig.input._pos = (0, 0)
    rig.play()
    moves = rig.clicks()
    assert moves[-1] == ("move", 100, 0)
    assert len(moves) == 21                   # 200 ms at one sample per 10 ms, plus the start
    assert all(m[2] == 0 for m in moves)      # a straight line ignores the recorded bend
    assert rig.clock.t == pytest.approx(0.2)


def test_move_pointer_scaled_by_speed(no_spacing, plays):
    no_spacing.playback.speed = 2.0

    def timed(ctx, row):
        return Flow.STOP if ctx.move_pointer([(100, 100)], 400) else Flow.NEXT
    plays("wait", timed)
    rig = Rig([wait(0)], no_spacing, path_mode="natural").play()
    assert rig.clicks()[-1] == ("move", 100, 100)
    assert rig.clock.t == pytest.approx(0.2)


def test_path_recorded_scaled_by_speed(no_spacing):
    no_spacing.playback.speed = 0.5
    rig = Rig([path([(0, 0), (1, 1)], 0.1)], no_spacing).play()
    assert rig.clock.t == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# PlayContext methods
# ---------------------------------------------------------------------------

def test_resolve_variants(settings):
    rig = Rig([], settings)
    p = rig.player
    p.variables["a"] = 4
    assert p.resolve(None, 7) == 7.0
    assert p.resolve("", 2) == 2.0
    assert p.resolve(3) == 3.0
    assert p.resolve("a * 2") == 8.0
    assert p.resolve_int("a / 3") == 1
    assert p.render("a={a}") == "a=4"


def test_coord_frame_rule_and_to_frame(settings):
    rig = Rig([win("xed", restore_rect="no")], settings, windows=moved_xed())
    p = rig.player
    assert p.coord(5, "5") == (5.0, 5.0)              # no context: screen frame
    p.enter_window(rig.document.rows[0])
    assert p.coord(100, 110) == (300.0, 230.0)
    assert p.coord("0", "0") == (300.0, 230.0)
    assert p.to_frame(310, 240) == (10.0, 10.0)
    with pytest.raises(player_mod.PlaybackError):
        p.coord(None, 1)


def test_region_corners_and_monitor(settings):
    rig = Rig([win("xed", restore_rect="no")], settings, windows=moved_xed())
    p = rig.player
    rig.screen._monitors = [Monitor(1, Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1050)),
                            Monitor(2, Rect(1920, 0, 1280, 1024), Rect(1920, 0, 1280, 1000))]
    assert p.region({"x": 30, "y": 40, "x2": 10, "y2": 20, "monitor": None}) == Rect(10, 20, 20, 20)
    assert p.region({"x": 0, "y": 0, "x2": 0, "y2": 0, "monitor": 2}) == Rect(1920, 0, 1280, 1024)
    p.variables["m"] = 1
    assert p.region({"x": 0, "y": 0, "x2": 0, "y2": 0, "monitor": "m"}) == Rect(0, 0, 1920, 1080)
    with pytest.raises(player_mod.PlaybackError, match="no monitor 3"):
        p.region({"x": 0, "y": 0, "x2": 0, "y2": 0, "monitor": 3})
    p.enter_window(rig.document.rows[0])
    assert p.region({"x": 100, "y": 110, "x2": "10", "y2": "10", "monitor": None}) == Rect(300, 230, 10, 10)


def test_sleep_returns_true_when_stopped(settings):
    rig = Rig([], settings)
    assert rig.player.sleep(100) is False
    rig.player.stop()
    assert rig.player.sleep(100) is True
    assert rig.player.stopped()


def test_dynamic_values_and_read_only(no_spacing):
    rig = Rig([var("x", "1"), var("y", "2")], no_spacing)
    p = rig.player
    for name, _ in DYNAMIC_VALUES:
        assert name in p.variables
        assert isinstance(p.variables[name], (int, float))
    assert p.variables["win_w"] == 1920 and p.variables["win_x"] == 0
    assert 0 <= p.variables["random"] < 1
    assert p.variables["timestamp_ms"] > 1_600_000_000_000
    assert p.variables["loop"] == 0
    assert list(p.variables) == []
    with pytest.raises(player_mod.PlaybackError, match="read-only"):
        p.variables["rt_ms"] = 1
    with pytest.raises(KeyError):
        p.variables["undefined"]
    rig = Rig([var("op_num", "1")], no_spacing).play()
    assert rig.error == "Row 1: op_num is a read-only dynamic value"


def test_ex_num_and_op_num(no_spacing):
    rows = [var("a", "op_num"), goto("third"), var("skipped", "1"),
            var("b", "op_num", label="third"), var("n", "ex_num")]
    rig = Rig(rows, no_spacing).play()
    v = rig.player.variables
    assert v["a"] == 1 and v["b"] == 4 and v["n"] == 4


def test_row_state_cleared_each_pass(no_spacing, plays):
    no_spacing.playback.repeat = 2
    seen = []

    def touch(ctx, row):
        state = ctx.row_state(row.id)
        seen.append(dict(state))
        state["n"] = state.get("n", 0) + 1
        return Flow.NEXT
    plays("wait", touch)
    Rig([wait(1)], no_spacing).play()
    assert seen == [{}, {}]


def test_log_and_run_command(no_spacing, tmp_path, plays):
    def run(ctx, row):
        ctx.log("hello")
        ctx.variables["code"] = ctx.run_command("pwd > out.txt; exit 4", True)
        return Flow.NEXT
    plays("wait", run)
    rig = Rig([wait(1)], no_spacing, doc_path=str(tmp_path / "m.json")).play()
    assert rig.error is None
    assert "[1] hello" in rig.host.logs
    assert rig.player.variables["code"] == 4
    assert (tmp_path / "out.txt").read_text().strip() == str(tmp_path)


def test_exec_timeout_returns_none(no_spacing):
    rows = [make_row(make("exec_program", command="sleep 5", wait_exit=True, timeout_ms=100,
                          var_name="code"))]
    rig = Rig(rows, no_spacing).play()
    assert rig.error is None
    assert rig.player.variables["code"] is None


# ---------------------------------------------------------------------------
# Holds, clock, breakpoints in a new group, errors, repeat setting
# ---------------------------------------------------------------------------

def test_holds_press_release_and_nest(no_spacing, plays):
    def combo(ctx, row):
        ctx.hold_key("Shift_L")
        ctx.hold_key("a")
        ctx.unhold_key("a")
        ctx.unhold_key("Shift_L")
        return Flow.NEXT
    plays("key_combo", combo)
    rows = [make_row(make("key_press", key="Shift_L")), make_row(make("key_combo", keys="shift+a")),
            make_row(make("key_release", key="Shift_L"))]
    rig = Rig(rows, no_spacing).play()
    assert rig.error is None
    # Shift held by key_press stays down through the combo, released only by key_release.
    assert rig.input.actions == [("press_key", "Shift_L"), ("press_key", "a"), ("release_key", "a"),
                                 ("release_key", "Shift_L")]


def test_held_buttons_are_released_on_exit(no_spacing, plays):
    def down(ctx, row):
        ctx.hold_button("left")
        return Flow.NEXT
    plays("mouse_click", down)
    rig = Rig([click(1, 1)], no_spacing).play()
    assert rig.error is None
    assert rig.input.actions[0] == ("press_button", "left")
    assert rig.input.held_buttons == set()


def test_now_follows_the_engine_clock(no_spacing, plays):
    seen = []

    def probe(ctx, row):
        seen.append(ctx.now())
        return Flow.NEXT
    plays("var_set", probe)
    clock = FakeClock(start=12.0)
    Rig([var("a", "1"), wait(250), var("b", "1")], no_spacing, clock=clock).play()
    assert seen == [pytest.approx(12.0), pytest.approx(12.25)]


def test_breakpoint_on_first_row_of_a_new_group_pauses_in_that_window(no_spacing):
    clock = FakeClock()
    host = PausingHost(clock)
    windows = FakeWindows([window(0x100, "xed", left=100, top=80, width=900, height=600),
                           window(0x200, "term", left=400, top=300, width=900, height=600)])
    rows = [win("xed", restore_rect="no"), click(1, 1), goto("t"), win("term", restore_rect="no"),
            click(2, 2, label="t", breakpoint=True)]
    rig = Rig(rows, no_spacing, host=host, clock=clock, windows=windows)
    host.player = rig.player
    rig.play()
    assert rig.error is None
    assert rig.windows.calls == [("activate", 0x100), ("activate", 0x200), ("activate", 0x200)]
    assert host.paused[0][1]["win_x"] == 400


def test_error_while_resuming_names_the_row(no_spacing):
    clock = FakeClock()
    host = PausingHost(clock)
    rows = [win("xed", restore_rect="no"), click(1, 1, breakpoint=True)]
    rig = Rig(rows, no_spacing, host=host, clock=clock, windows=moved_xed())
    host.player = rig.player

    def fail(window, settle_ms=1000):
        raise OSError("connection closed")
    original = host.on_paused

    def paused(row_id, variables):
        rig.windows.activate = fail
        original(row_id, variables)
    host.on_paused = paused
    rig.play()
    assert rig.error == "Row 2: OSError: connection closed"


def test_negative_repeat_is_refused(settings):
    settings.playback.repeat = -1
    with pytest.raises(ValueError, match="Repeat"):
        Rig([click(1, 1)], settings)


def test_unscheduled_row_resets_the_schedule(no_spacing, plays):
    def slow_ocr(ctx, row):
        rig.clock.advance(0.04)
        return Flow.NEXT
    plays("ocr_read", slow_ocr)
    rows = [make_row(make("ocr_read")), wait(100)]
    rig = Rig(rows, no_spacing)
    rig.play()
    assert rig.error is None
    # The 40 ms OCR does not shorten the following wait.
    assert rig.clock.t == pytest.approx(0.140)


def test_unwaited_exec_program_is_killed_when_playback_ends(no_spacing, tmp_path, plays):
    pidfile = tmp_path / "pid"

    def start(ctx, row):
        ctx.run_command("sleep 30 & echo $! > '%s'; wait" % pidfile, False)
        deadline = time.monotonic() + 3
        while not pidfile.exists() or not pidfile.read_text().strip():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        return Flow.NEXT
    plays("exec_program", start)
    rig = Rig([make_row(make("exec_program", command="x"))], no_spacing).play()
    assert rig.error is None
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            with open("/proc/%d/stat" % pid) as f:
                if f.read().split()[2] == "Z":
                    break
        except FileNotFoundError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("the exec_program child is still running")
