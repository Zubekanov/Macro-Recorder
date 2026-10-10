"""Every registered action played by the real Player against the conftest fakes.

The per-action tests in ``test_actions_*.py`` use a fake context and the
player tests swap in minimal plays; these run the real specs on the real
engine, so the PlayContext contract is exercised from both sides.  Bugs the
integration work found have a focused test here.
"""

from __future__ import annotations


import pytest

from macro_recorder.engine import player as player_mod
from macro_recorder.gui.settings import RecordingSettings, Settings
from macro_recorder.model import bundling
from macro_recorder.model.actions import keyboard, misc, wait
from macro_recorder.model.catalogue import make
from macro_recorder.model.document import make_row
from macro_recorder.model.protocols import Rect, WindowInfo
from tests.conftest import FakeClock, FakeHost, FakeScreen, FakeWindows
from tests.test_integration import XED, Rig, r, window_row, xed_at


def frame_settings() -> Settings:
    """Settings that leave the window where it is, so the 40 px delta applies."""
    settings = Settings()
    settings.playback.restore_rect = False
    return settings


# ---------------------------------------------------------------------------
# Mouse
# ---------------------------------------------------------------------------

def test_mouse_move_numbers_get_the_delta_and_expressions_the_client_origin():
    rig = Rig([window_row(), r("mouse_move", x=10, y=20), r("mouse_move", x="5 * 2", y="20")],
              windows=FakeWindows([xed_at(40, 40)]), settings=frame_settings())
    assert rig.play() is None
    assert rig.actions("move") == [("move", 50, 60), ("move", 150, 170)]


def test_mouse_move_timed_starts_at_the_first_point_and_takes_its_duration():
    rig = Rig([r("mouse_move_timed", x=0, y=0, x2=100, y2=0, duration_ms=200)])
    assert rig.play() is None
    moves = rig.actions("move")
    assert moves[0] == ("move", 0, 0) and moves[-1] == ("move", 100, 0)
    assert all(0 <= x <= 100 and y == 0 for _, x, y in moves)
    assert 0.2 <= rig.clock.t <= 0.25


def test_double_click_holds_and_waits_its_interval_scaled_by_speed():
    settings = Settings()
    settings.playback.speed = 2.0
    settings.playback.min_spacing_ms = 0
    rig = Rig([r("mouse_click", x=5, y=5, count=2, hold_ms=100, interval_ms=200)], settings=settings)
    assert rig.play() is None
    assert rig.input.actions == [("move", 5, 5), ("press_button", "left"), ("release_button", "left"),
                                 ("press_button", "left"), ("release_button", "left")]
    assert rig.clock.t == pytest.approx((100 + 200 + 100) / 1000 / 2)


def test_click_does_not_move_when_the_pointer_is_close():
    rig = Rig([r("mouse_move", x=100, y=100), r("mouse_click", x=101, y=101, button="right")])
    assert rig.play() is None
    assert rig.input.actions == [("move", 100, 100), ("press_button", "right"),
                                 ("release_button", "right")]


def test_click_down_left_held_is_released_at_the_end():
    rig = Rig([r("mouse_click", x=1, y=1, action="down"), r("type_text", text="x")])
    assert rig.play() is None
    assert rig.input.actions[-1] == ("release_button", "left")
    assert rig.input.held_buttons == set()


def test_scroll_without_position_and_with_both_axes():
    rig = Rig([r("mouse_scroll", x=None, y=None, dx=-1, dy="1 - 3", notch_ms=0)])
    assert rig.play() is None
    assert rig.input.actions == [("scroll", 0, -1), ("scroll", 0, -1), ("scroll", -1, 0)]


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------

def test_key_combo_presses_in_order_and_releases_in_reverse():
    rig = Rig([r("key_combo", keys="ctrl+shift+s", hold_ms=80)])
    assert rig.play() is None
    assert rig.input.actions == [
        ("press_key", "Control_L"), ("press_key", "Shift_L"), ("press_key", "s"),
        ("release_key", "s"), ("release_key", "Shift_L"), ("release_key", "Control_L"),
    ]


def test_key_combo_inside_a_held_modifier_keeps_it_down():
    rig = Rig([r("key_press", key="Shift_L"), r("key_combo", keys="shift+a"),
               r("type_text", text="b"), r("key_release", key="Shift_L")])
    assert rig.play() is None
    assert rig.input.actions == [
        ("press_key", "Shift_L"), ("press_key", "a"), ("release_key", "a"),
        ("type_char", "b"), ("release_key", "Shift_L"),
    ]


def test_bad_combo_aborts_with_the_row_number():
    rig = Rig([r("type_text", text="a"), r("key_combo", keys="a+b")])
    error = rig.play()
    assert error is not None and error.startswith("Row 2: ")
    assert rig.input.held_keys == set()


def test_type_text_renders_and_spreads_its_duration():
    settings = Settings()
    settings.playback.min_spacing_ms = 0
    rig = Rig([r("var_set", var_name="n", expr="7"),
               r("type_text", text="n={n}", duration_ms=300)], settings=settings)
    assert rig.play() is None
    assert rig.typed() == "n=7"
    assert rig.clock.t == pytest.approx(0.3)


def test_type_text_humanise_uses_the_module_rng(monkeypatch):
    class Fixed:
        def uniform(self, low, high):
            return high
    monkeypatch.setattr(keyboard, "rng", Fixed())
    settings = Settings()
    settings.playback.min_spacing_ms = 0
    rig = Rig([r("type_text", text="ab", char_delay_ms=100, humanise=True)], settings=settings)
    assert rig.play() is None
    assert rig.clock.t == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Wait, variables, OCR
# ---------------------------------------------------------------------------

def test_random_wait_uses_the_module_rng(monkeypatch):
    class Fixed:
        def uniform(self, low, high):
            return (low + high) / 2
    monkeypatch.setattr(wait, "rng", Fixed())
    settings = Settings()
    settings.playback.min_spacing_ms = 0
    rig = Rig([r("wait", duration_ms=1000, random_max_ms=3000)], settings=settings)
    assert rig.play() is None
    assert rig.clock.t == pytest.approx(2.0)


def test_dynamic_values_in_a_window_frame():
    rig = Rig([
        window_row(),
        r("mouse_move", x=150, y=160),
        r("var_set", var_name="mx", expr="mouse_x"),
        r("var_set", var_name="my", expr="mouse_y"),
        r("var_set", var_name="geometry", expr="str(win_x) + ',' + str(win_y) + ',' + str(win_w)"),
        r("var_set", var_name="row", expr="op_num"),
        r("var_set", var_name="outside", expr="loop"),
    ], windows=FakeWindows([xed_at(40, 40)]), settings=frame_settings())
    assert rig.play() is None
    v = rig.player.variables
    assert (v["mx"], v["my"]) == (50, 50)
    assert v["geometry"] == "140,150,900"
    assert v["row"] == 6
    assert v["outside"] == 0


def test_assigning_a_dynamic_value_aborts():
    rig = Rig([r("var_set", var_name="loop", expr="1")])
    error = rig.play()
    assert error is not None and error.startswith("Row 1: ")


def test_ocr_read_reads_the_frame_adjusted_region():
    screen = FakeScreen()
    screen.text = "  total 42 \n"
    rig = Rig([window_row(), r("ocr_read", var_name="text", x=100, y=110, x2=200, y2=160)],
              windows=FakeWindows([xed_at(40, 40)]), screen=screen, settings=frame_settings())
    assert rig.play() is None
    assert rig.player.variables["text"] == "total 42"
    assert screen.captures == [Rect(140, 150, 100, 50)]


def test_ocr_read_monitor_region_is_not_adjusted():
    screen = FakeScreen()
    screen.text = "x"
    rig = Rig([window_row(), r("ocr_read", var_name="t", monitor=1)],
              windows=FakeWindows([xed_at(40, 40)]), screen=screen, settings=frame_settings())
    assert rig.play() is None
    assert screen.captures == [Rect(0, 0, 1920, 1080)]


def test_match_text_captures_the_text_and_times_out():
    screen = FakeScreen()
    screen.text = "Saved  OK"
    rig = Rig([
        r("var_set", var_name="word", expr="'ok'"),
        r("match_text", expr="saved {word}", var_name="hit", capture_var="seen", timeout_ms=1000),
        r("match_text", expr="nothing like it", var_name="miss", timeout_ms=600,
          on_timeout="abort"),
    ], screen=screen)
    error = rig.play()
    assert rig.player.variables["hit"] == 1
    assert rig.player.variables["seen"] == "Saved  OK"
    assert rig.player.variables["miss"] == 0
    assert error is not None and error.startswith("Row 3: ")
    assert 0.6 <= rig.clock.t <= 0.7


# ---------------------------------------------------------------------------
# Window rows
# ---------------------------------------------------------------------------

def test_window_launch_runs_once_and_waits_for_the_window(monkeypatch):
    windows = FakeWindows([])
    clock = FakeClock()
    launches = []

    def fake_run(command, cwd, wait, timeout_ms, stop_event, children=None):
        launches.append(command)
        windows.windows.append(xed_at())
        return None

    monkeypatch.setattr(player_mod.processes, "run", fake_run)
    rig = Rig([window_row(launch="xed"), r("type_text", text="x")], windows=windows, clock=clock)
    assert rig.play() is None
    assert launches == ["xed"]
    assert rig.activations() == [XED]
    assert rig.typed() == "x"
    # One poll, then the settle time before input.
    assert clock.t >= 0.25 + 1.0


def test_window_missing_goto_and_remembered_for_later_jumps():
    windows = FakeWindows([])
    rig = Rig([
        r("var_set", var_name="n", expr="0"),
        window_row(on_missing="goto", target="fallback", timeout_ms=500),
        r("type_text", label="inside", text="in"),
        window_row(enabled=False),
        r("type_text", label="fallback", text="F"),
        r("var_set", var_name="n", expr="n + 1"),
        r("if_else", expr="n < 2", target="inside"),
    ], windows=windows)
    assert rig.play() is None
    # The second jump into the group skips it without polling again.
    assert rig.typed() == "FF"
    assert rig.clock.t < 1.0
    assert any("was not found earlier" in line for line in rig.host.logs)


def test_window_recorded_maximised_is_maximised_not_moved():
    windows = FakeWindows([xed_at(40, 40)])
    rig = Rig([window_row(maximised=True)], windows=windows)
    assert rig.play() is None
    assert ("maximise", XED, True) in windows.calls
    assert not any(c[0] == "move" for c in windows.calls)


def test_window_regex_and_bad_pattern():
    windows = FakeWindows([xed_at()])
    rig = Rig([window_row(title=r"^Untitled\b.*xed$", match_mode="regex")], windows=windows)
    assert rig.play() is None
    assert rig.activations() == [XED]
    bad = Rig([window_row(title="(", match_mode="regex")])
    error = bad.play()
    assert error is not None and error.startswith("Row 1: bad window pattern")


def test_two_groups_switch_windows():
    firefox = WindowInfo(0x200, "Firefox", Rect(0, 0, 1000, 700), Rect(0, 30, 1000, 670))
    windows = FakeWindows([xed_at(), firefox])
    rig = Rig([
        window_row(),
        r("mouse_click", x=200, y=200),
        make_row(make("window_focus", window="Firefox", rect=(0, 0, 1000, 700),
                      client_rect=(10, 40, 1000, 670))),
        r("mouse_click", x=200, y=200),
    ], windows=windows, settings=frame_settings())
    assert rig.play() is None
    assert rig.activations() == [XED, 0x200]
    assert rig.actions("move") == [("move", 200, 200), ("move", 190, 190)]


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def test_exec_program_stores_the_exit_code_in_the_macro_folder(tmp_path):
    rig = Rig([r("exec_program", command="pwd > where.txt; exit 3", wait_exit=True, var_name="code")],
              path=str(tmp_path / "m.json"))
    assert rig.play() is None
    assert rig.player.variables["code"] == 3
    assert (tmp_path / "where.txt").read_text().strip() == str(tmp_path)
    assert any("exited with code 3" in line for line in rig.host.logs)


def test_exec_program_timeout_is_logged_and_playback_continues(tmp_path):
    """Player.run_command returns None on a timeout, as the PlayContext contract says."""
    rig = Rig([r("exec_program", command="sleep 5", wait_exit=True, timeout_ms=200, var_name="code"),
               r("type_text", text="next")], path=str(tmp_path / "m.json"))
    assert rig.play() is None
    assert rig.typed() == "next"
    assert rig.player.variables["code"] == -1
    assert any("did not exit within 200 ms" in line for line in rig.host.logs)


def test_exec_program_that_cannot_start_aborts(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise player_mod.processes.ProcessError("Could not run 'x': [Errno 13] Permission denied")
    monkeypatch.setattr(player_mod.processes, "run", refuse)
    rig = Rig([r("exec_program", command="x")])
    error = rig.play()
    assert error is not None and error.startswith("Row 1: cannot run x: ")


def test_exec_program_quotes_template_values(tmp_path):
    rig = Rig([r("var_set", var_name="name", expr="'a b; touch hacked'"),
               r("exec_program", command="printf %s {name} > out.txt", wait_exit=True)],
              path=str(tmp_path / "m.json"))
    assert rig.play() is None
    assert (tmp_path / "out.txt").read_text() == "a b; touch hacked"
    assert not (tmp_path / "hacked").exists()


def test_notify_failure_is_logged(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("notify-send")
    monkeypatch.setattr(misc.subprocess, "run", missing)
    rig = Rig([r("notify", text="hi"), r("type_text", text="x")])
    assert rig.play() is None
    assert rig.typed() == "x"
    assert any("Notification failed" in line for line in rig.host.logs)


def test_beep_rings_the_window_manager_bell():
    rig = Rig([r("beep"), r("beep")])
    assert rig.play() is None
    assert rig.windows.bells == 2


# ---------------------------------------------------------------------------
# Focused tests for bugs found by the integration work
# ---------------------------------------------------------------------------

def test_split_gap_float_boundary_is_a_delay():
    """bundling.split_gap: 2.7 - 2.6 is 100.00000000000009 ms, which is not over 100."""
    s = RecordingSettings()
    assert bundling.split_gap(2.7 - 2.6, s) == (None, 100)
    assert bundling.split_gap(1.1 - 1.0, s) == (None, 100)
    assert bundling.split_gap(0.1004, s) == (100, 0)


def test_message_box_reactivates_the_window_once():
    rig = Rig([window_row(), r("message_box", text="go")], host=FakeHost(["ok"]))
    assert rig.play() is None
    assert rig.activations() == [XED, XED]


def test_message_box_answered_none_restores_nothing():
    rig = Rig([window_row(), r("key_press", key="a"), r("message_box", text="go")],
              host=FakeHost([None]))
    assert rig.play() is None
    assert rig.activations() == [XED]
    assert rig.input.actions == [("press_key", "a"), ("release_key", "a")]


def test_match_image_path_with_tilde_is_expanded(tmp_path, monkeypatch):
    """validate expands ~ in a FILE field, so playback must too."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "shot.png").write_bytes(b"png")
    screen = FakeScreen()
    screen.images[str(tmp_path / "shot.png")] = (150, 150, 1.0)
    rig = Rig([r("match_image", image_path="~/shot.png", x=0, y=0, x2=300, y2=300)], screen=screen)
    assert rig.play() is None
    assert rig.player.variables["found"] == 1


@pytest.mark.parametrize("text, names", [
    ("+", ("plus",)),
    ("ctrl++", ("Control_L", "plus")),
    ("ctrl+shift+s", ("Control_L", "Shift_L", "s")),
])
def test_parse_combo_plus_key(text, names):
    from macro_recorder.model.keys import parse_combo
    assert parse_combo(text) == names


def test_parse_combo_rejects_a_missing_key_instead_of_dropping_modifiers():
    """keys.parse_combo turned "ctrl+" into the bare plus key, losing Ctrl."""
    from macro_recorder.model.keys import parse_combo
    with pytest.raises(ValueError, match="No key after"):
        parse_combo("ctrl+")
    rig = Rig([r("key_combo", keys="ctrl+")])
    error = rig.play()
    assert error is not None and error.startswith("Row 1: No key after")
    assert rig.input.actions == []
