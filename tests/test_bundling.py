"""Bundling a raw recorded stream into rows: rules 1-8 of DESIGN.md."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from macro_recorder.gui.settings import RecordingSettings
from macro_recorder.model import bundling
from macro_recorder.model.bundling import StreamBundler, rows_from_stream
from macro_recorder.model.keys import parse_combo
from macro_recorder.model.protocols import RawEvent, Rect, WindowInfo

XED = WindowInfo(id=0x100, title="xed", rect=Rect(100, 80, 900, 600),
                 client_rect=Rect(100, 110, 900, 570))
FIREFOX = WindowInfo(id=0x200, title="Firefox", rect=Rect(0, 0, 1920, 1080),
                     client_rect=Rect(0, 30, 1920, 1050), maximised=True)


# ---------------------------------------------------------------- builders

def press(t, x=10, y=10, button="left"):
    return RawEvent("button_press", t, x=x, y=y, button=button)


def release(t, x=10, y=10, button="left"):
    return RawEvent("button_release", t, x=x, y=y, button=button)


def move(t, x, y):
    return RawEvent("motion", t, x=x, y=y)


def scroll(t, dy=1, dx=0, x=50, y=50):
    return RawEvent("scroll", t, x=x, y=y, dx=dx, dy=dy)


def kdown(t, key):
    return RawEvent("key_press", t, key=key)


def kup(t, key):
    return RawEvent("key_release", t, key=key)


def focus(t, window=XED):
    return RawEvent("window_focus", t, window=window)


def click(t, x=10, y=10, button="left", hold=0.05):
    return [press(t, x, y, button), release(t + hold, x, y, button)]


def tap(t, key, hold=0.05):
    return [kdown(t, key), kup(t + hold, key)]


def bundle(*parts, **settings):
    stream = []
    for p in parts:
        stream.extend(p if isinstance(p, list) else [p])
    return rows_from_stream(stream, RecordingSettings(**settings))


def summary(rows):
    """Compact, readable form of rows for assertions."""
    out = []
    for r in rows:
        e = r.event
        if r.is_path:
            out.append(("path", tuple((ev["x"], ev["y"]) for ev in r.events)))
        elif e.type == "mouse_move":
            out.append(("path", ((e["x"], e["y"]),)))
        elif e.type == "mouse_click":
            if e["action"] == "click":
                out.append(("click", e["button"], e["x"], e["y"], e["count"]))
            else:
                out.append((e["action"], e["button"], e["x"], e["y"]))
        elif e.type == "mouse_scroll":
            out.append(("scroll", e["x"], e["y"], e["dx"], e["dy"]))
        elif e.type == "mouse_move_timed":
            out.append(("timed", e["x"], e["y"], e["x2"], e["y2"], e["duration_ms"]))
        elif e.type == "key_combo":
            out.append(("key", e["keys"]))
        elif e.type in ("key_press", "key_release"):
            out.append((e.type, e["key"]))
        elif e.type == "wait":
            out.append(("wait", e["duration_ms"]))
        elif e.type == "window_focus":
            out.append(("window", e["window"]))
        else:
            out.append((e.type,))
    return out


def no_waits(rows):
    return [s for s in summary(rows) if s[0] != "wait"]


# ------------------------------------------------- rule 2: clicks and counts

def test_click_has_hold_from_recording():
    rows = bundle(click(0.0, 5, 6, hold=0.07))
    assert summary(rows) == [("click", "left", 5, 6, 1)]
    assert rows[0].event["hold_ms"] == 70


def test_press_release_past_click_gap_in_place_is_down_up():
    rows = bundle(press(0.0, 5, 5), release(0.4, 5, 5))
    assert summary(rows) == [("down", "left", 5, 5), ("wait", 400), ("up", "left", 5, 5)]


def test_click_with_jitter_inside_min_move_is_a_click():
    rows = bundle(press(0.0, 5, 5), move(0.02, 6, 5), release(0.05, 5, 5))
    assert summary(rows) == [("click", "left", 5, 5, 1)]


def test_double_and_triple_click():
    rows = bundle(click(0.0), click(0.2), click(0.4))
    assert summary(rows) == [("click", "left", 10, 10, 3)]
    assert rows[0].event["interval_ms"] == 150
    assert rows[0].event["hold_ms"] == 50


def test_fourth_click_starts_a_new_row():
    rows = bundle(click(0.0), click(0.2), click(0.4), click(0.6))
    assert no_waits(rows) == [("click", "left", 10, 10, 3), ("click", "left", 10, 10, 1)]


def test_double_click_needs_same_point_button_and_time():
    assert no_waits(bundle(click(0.0), click(0.2, x=30))) == [
        ("click", "left", 10, 10, 1), ("click", "left", 30, 10, 1)]
    assert no_waits(bundle(click(0.0), click(0.2, button="right"))) == [
        ("click", "left", 10, 10, 1), ("click", "right", 10, 10, 1)]
    assert no_waits(bundle(click(0.0), click(0.5))) == [
        ("click", "left", 10, 10, 1), ("click", "left", 10, 10, 1)]


def test_row_between_clicks_prevents_double_click():
    rows = bundle(click(0.0), tap(0.1, "a"), click(0.2))
    assert no_waits(rows) == [("click", "left", 10, 10, 1), ("key", "a"), ("click", "left", 10, 10, 1)]


def test_continues_click_helper():
    s = RecordingSettings()
    first = bundling._Draft("mouse_click", 0.0, 0.05, {"x": 10, "y": 10, "button": "left",
                                                      "action": "click", "count": 1},
                            meta={"last_press": 0.0})
    assert bundling.continues_click(first, press(0.4), s)
    assert not bundling.continues_click(first, press(0.41), s)
    assert not bundling.continues_click(first, press(0.1, x=13), s)


# ------------------------------------------------------------ rule 3: drags

def test_drag_becomes_down_path_up():
    rows = bundle(press(1.0, 10, 10), move(1.1, 20, 15), move(1.2, 40, 25), move(1.5, 60, 30),
                  release(1.7, 60, 30))
    assert summary(rows) == [
        ("down", "left", 10, 10),
        ("path", ((20, 15), (40, 25), (60, 30))),
        ("wait", 200),
        ("up", "left", 60, 30),
    ]
    path = rows[1]
    assert path.delay_ms == 100         # exactly min_wait_ms is a delay, not a wait
    assert [round(e.dt, 3) for e in path.events] == [0.0, 0.1, 0.3]


def test_drag_without_record_paths_is_timed_move():
    rows = bundle(press(1.0, 10, 10), move(1.1, 20, 15), move(1.2, 40, 25), move(1.5, 60, 30),
                  release(1.55, 60, 30), record_paths=False)
    assert no_waits(rows) == [("down", "left", 10, 10), ("timed", 10, 10, 60, 30, 450),
                              ("up", "left", 60, 30)]


def test_timed_move_ends_at_the_release_point():
    rows = bundle(press(0.0, 0, 0), move(0.1, 20, 0), release(0.2, 40, 0), record_paths=False)
    assert no_waits(rows) == [("down", "left", 0, 0), ("timed", 0, 0, 40, 0, 100), ("up", "left", 40, 0)]


def test_timed_move_continues_after_an_interruption():
    rows = bundle(press(0.0, 0, 0), move(0.05, 10, 0), tap(0.1, "a"), move(0.15, 30, 0),
                  release(0.2, 40, 0), record_paths=False)
    assert no_waits(rows) == [("down", "left", 0, 0), ("timed", 0, 0, 10, 0, 0), ("key", "a"),
                              ("timed", 10, 0, 40, 0, 50), ("up", "left", 40, 0)]


def test_fast_flick_far_away_is_a_drag_not_a_click():
    rows = bundle(press(0.0, 0, 0), move(0.05, 50, 50), release(0.1, 50, 50))
    assert no_waits(rows) == [("down", "left", 0, 0), ("path", ((50, 50),)), ("up", "left", 50, 50)]


def test_two_consecutive_drags():
    rows = bundle(press(0.0, 0, 0), move(0.05, 5, 5), release(0.1, 5, 5),
                  press(1.0, 5, 5), move(1.05, 9, 9), release(1.1, 9, 9))
    assert [s[0] for s in no_waits(rows)] == ["down", "path", "up"] * 2


def test_key_during_held_button_splits_into_down_and_up():
    rows = bundle(press(0.0, 0, 0), move(0.02, 5, 5), tap(0.05, "Shift_L", 0.01), move(0.08, 10, 10),
                  release(0.1, 10, 10))
    assert no_waits(rows) == [("down", "left", 0, 0), ("path", ((5, 5),)), ("key", "Shift_L"),
                              ("path", ((10, 10),)), ("up", "left", 10, 10)]


def test_other_button_inside_a_hold():
    rows = bundle(press(0.0, 0, 0, "left"), click(0.1, 0, 0, "right"), release(0.3, 0, 0, "left"))
    assert no_waits(rows) == [("down", "left", 0, 0), ("click", "right", 0, 0, 1), ("up", "left", 0, 0)]


def test_drag_ignores_path_gap_rule():
    rows = bundle(press(0.0, 0, 0), move(0.05, 10, 0), move(0.6, 20, 0), release(0.65, 20, 0))
    assert no_waits(rows) == [("down", "left", 0, 0), ("path", ((10, 0), (20, 0))), ("up", "left", 20, 0)]


# ------------------------------------------------------------ rule 4: paths

def test_path_drops_samples_closer_than_min_move():
    rows = bundle(move(0.0, 0, 0), move(0.01, 1, 0), move(0.02, 3, 0), move(0.03, 4, 0), move(0.04, 7, 0))
    assert summary(rows) == [("path", ((0, 0), (3, 0), (7, 0)))]
    assert [round(e.dt, 3) for e in rows[0].events] == [0.0, 0.02, 0.02]


def test_path_gap_over_min_wait_closes_it():
    rows = bundle(move(0.0, 0, 0), move(0.05, 10, 0), move(0.3, 20, 0), move(0.35, 30, 0))
    assert summary(rows) == [("path", ((0, 0), (10, 0))), ("wait", 250), ("path", ((20, 0), (30, 0)))]


def test_jitter_after_click_makes_no_path():
    rows = bundle(click(0.0, 10, 10), move(0.1, 11, 11), move(0.2, 12, 10))
    assert summary(rows) == [("click", "left", 10, 10, 1)]


def test_free_motion_is_not_recorded_without_record_paths():
    rows = bundle(move(0.0, 0, 0), move(0.05, 50, 0), click(0.1, 50, 0), record_paths=False)
    assert summary(rows) == [("click", "left", 50, 0, 1)]


def test_path_closes_at_a_click():
    rows = bundle(move(0.0, 0, 0), move(0.02, 10, 0), click(0.05, 10, 0))
    assert summary(rows) == [("path", ((0, 0), (10, 0))), ("click", "left", 10, 0, 1)]
    assert rows[1].delay_ms == 30


# ----------------------------------------------------------- rule 5: scroll

def test_scroll_notches_sum():
    rows = bundle(scroll(0.0), scroll(0.03), scroll(0.06))
    assert summary(rows) == [("scroll", 50, 50, 0, 3)]
    assert rows[0].event["notch_ms"] == 30


def test_scroll_direction_change_starts_new_row():
    rows = bundle(scroll(0.0), scroll(0.03), scroll(0.06, dy=-1))
    assert summary(rows) == [("scroll", 50, 50, 0, 2), ("scroll", 50, 50, 0, -1)]


def test_scroll_at_another_point_starts_new_row():
    rows = bundle(scroll(0.0), scroll(0.03, x=80))
    assert summary(rows) == [("scroll", 50, 50, 0, 1), ("scroll", 80, 50, 0, 1)]


def test_scroll_gap_over_min_wait_starts_new_row():
    rows = bundle(scroll(0.0), scroll(0.2))
    assert summary(rows) == [("scroll", 50, 50, 0, 1), ("wait", 200), ("scroll", 50, 50, 0, 1)]


def test_horizontal_scroll():
    rows = bundle(scroll(0.0, dy=0, dx=-1), scroll(0.02, dy=0, dx=-1))
    assert summary(rows) == [("scroll", 50, 50, -2, 0)]


# -------------------------------------------------------------- rule 6: keys

def test_key_tap_is_one_combo_with_hold():
    rows = bundle(tap(0.0, "a", 0.08))
    assert summary(rows) == [("key", "a")]
    assert rows[0].event["hold_ms"] == 80


def test_modifier_combo():
    rows = bundle(kdown(0.0, "Control_L"), kdown(0.05, "Shift_R"), tap(0.1, "s"),
                  kup(0.2, "Shift_R"), kup(0.25, "Control_L"))
    assert summary(rows) == [("key", "ctrl+shift+s")]
    assert parse_combo(rows[0].event["keys"]) == ("Control_L", "Shift_L", "s")


def test_modifier_held_over_several_keys():
    rows = bundle(kdown(0.0, "Control_L"), tap(0.05, "c"), tap(0.15, "v"), kup(0.25, "Control_L"))
    assert no_waits(rows) == [("key", "ctrl+c"), ("key", "ctrl+v")]


def test_lone_modifier_tap():
    assert summary(bundle(tap(0.0, "Super_L"))) == [("key", "Super_L")]


def test_autorepeat_collapses_into_one_hold():
    rows = bundle(kdown(0.0, "a"), kdown(0.5, "a"), kdown(0.53, "a"), kdown(0.56, "a"), kup(0.6, "a"))
    assert summary(rows) == [("key", "a")]
    assert rows[0].event["hold_ms"] == 600


def test_key_held_across_a_click():
    rows = bundle(kdown(0.0, "Shift_L"), click(0.1), kup(0.3, "Shift_L"))
    assert no_waits(rows) == [("key_press", "Shift_L"), ("click", "left", 10, 10, 1),
                              ("key_release", "Shift_L")]


def test_combo_modifier_still_held_at_click_is_pressed_again():
    rows = bundle(kdown(0.0, "Control_L"), tap(0.05, "s"), click(0.2), tap(0.3, "v"),
                  kup(0.4, "Control_L"))
    assert no_waits(rows) == [("key", "ctrl+s"), ("key_press", "Control_L"),
                              ("click", "left", 10, 10, 1), ("key", "v"),
                              ("key_release", "Control_L")]


def test_ctrl_held_over_a_rollover_keeps_the_modifier():
    rows = bundle(kdown(0.0, "Control_L"), kdown(0.1, "c"), kdown(0.15, "v"), kup(0.2, "c"),
                  kup(0.25, "v"), kup(0.3, "Control_L"))
    assert summary(rows) == [("key_press", "Control_L"), ("key_press", "c"), ("key_press", "v"),
                             ("key_release", "c"), ("key_release", "v"), ("key_release", "Control_L")]
    assert [r.delay_ms for r in rows] == [0, 100, 50, 50, 50, 50]


def test_shift_held_over_a_rollover_of_capitals():
    rows = bundle(kdown(0.0, "Shift_L"), kdown(0.1, "H"), kdown(0.15, "I"), kup(0.2, "H"),
                  kup(0.25, "I"), kup(0.3, "Shift_L"))
    assert summary(rows) == [("key_press", "Shift_L"), ("key_press", "H"), ("key_press", "I"),
                             ("key_release", "H"), ("key_release", "I"), ("key_release", "Shift_L")]


def test_key_held_across_a_click_with_ctrl_held():
    rows = bundle(kdown(0.0, "Control_L"), kdown(0.1, "a"), click(0.2), kup(0.4, "a"),
                  kup(0.5, "Control_L"))
    assert no_waits(rows) == [("key_press", "Control_L"), ("key_press", "a"),
                              ("click", "left", 10, 10, 1), ("key_release", "a"),
                              ("key_release", "Control_L")]


def test_rollover_tap_does_not_repeat_a_held_modifier():
    rows = bundle(kdown(0.0, "Control_L"), kdown(0.1, "c"), tap(0.15, "v", 0.02), kup(0.2, "c"),
                  kup(0.3, "Control_L"))
    assert summary(rows) == [("key_press", "Control_L"), ("key_press", "c"), ("key", "v"),
                             ("key_release", "c"), ("key_release", "Control_L")]


def test_modifier_released_before_its_key_turns_held():
    rows = bundle(kdown(0.0, "Control_L"), kdown(0.1, "c"), kup(0.15, "Control_L"), kdown(0.2, "v"),
                  kup(0.25, "c"), kup(0.3, "v"))
    assert summary(rows) == [("key_press", "Control_L"), ("key_press", "c"), ("key_release", "Control_L"),
                             ("key_press", "v"), ("key_release", "c"), ("key_release", "v")]


def test_overlapping_keys_become_halves():
    rows = bundle(kdown(0.0, "a"), kdown(0.02, "b"), kup(0.04, "a"), kup(0.06, "b"))
    assert summary(rows) == [("key_press", "a"), ("key_press", "b"), ("key_release", "a"),
                             ("key_release", "b")]


def test_orphan_key_press_and_release_are_dropped_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="macro_recorder.model.bundling"):
        rows = bundle(kup(0.0, "x"), tap(0.1, "a"), kdown(0.3, "b"))
    assert no_waits(rows) == [("key", "a")]
    assert "x key release with no press" in caplog.text
    assert "b key press with no release" in caplog.text


def test_held_key_without_release_loses_its_press_row():
    rows = bundle(kdown(0.0, "Shift_L"), click(0.1))
    assert summary(rows) == [("click", "left", 10, 10, 1)]


def test_orphan_button_press_and_release_are_dropped(caplog):
    with caplog.at_level(logging.WARNING, logger="macro_recorder.model.bundling"):
        rows = bundle(release(0.0, button="right"), click(0.1), press(0.3, 1, 1), move(0.35, 50, 50))
    assert no_waits(rows) == [("click", "left", 10, 10, 1)]
    assert "right button release with no press" in caplog.text
    assert "left button press with no release" in caplog.text


def test_typing_while_moving_closes_the_path():
    rows = bundle(move(0.0, 0, 0), move(0.02, 10, 0), tap(0.03, "a"), move(0.1, 20, 0))
    assert no_waits(rows) == [("path", ((0, 0), (10, 0))), ("key", "a"), ("path", ((20, 0),))]


# ------------------------------------------------------------- rule 7: gaps

def test_gap_up_to_min_wait_is_delay_and_over_is_wait():
    rows = bundle(tap(0.0, "a"), tap(0.15, "b"), tap(0.40, "c"))
    assert summary(rows) == [("key", "a"), ("key", "b"), ("wait", 200), ("key", "c")]
    assert [r.delay_ms for r in rows] == [0, 100, 0, 0]


def test_split_gap_boundary():
    s = RecordingSettings()
    assert bundling.split_gap(0.1, s) == (None, 100)
    assert bundling.split_gap(0.1006, s) == (101, 0)
    assert bundling.split_gap(-0.01, s) == (None, 0)


# ------------------------------------------------- rules 1 and 8: windows

def test_initial_window_row():
    rows = bundle(focus(0.0, XED), click(0.05))
    assert summary(rows) == [("window", "xed"), ("click", "left", 10, 10, 1)]
    e = rows[0].event
    assert e["match_mode"] == "substring"
    assert e["rect"] == (100, 80, 900, 600)
    assert e["client_rect"] == (100, 110, 900, 570)
    assert e["maximised"] is False
    assert rows[1].delay_ms == 50


def test_window_row_records_maximised():
    rows = bundle(focus(0.0, FIREFOX))
    assert rows[0].event["maximised"] is True


def test_marker_after_activating_click_moves_before_it():
    rows = bundle(focus(0.0, XED), tap(0.5, "x"), click(1.0, 500, 500), focus(1.2, FIREFOX),
                  tap(2.0, "a"))
    assert no_waits(rows) == [("window", "xed"), ("key", "x"), ("window", "Firefox"),
                              ("click", "left", 500, 500, 1), ("key", "a")]
    i = [s[0] for s in summary(rows)].index("click")
    assert rows[i].delay_ms == 0


def test_marker_between_press_and_release_moves_before_the_press():
    rows = bundle(press(1.0, 5, 5), focus(1.02, FIREFOX), move(1.05, 6, 6), release(1.1, 5, 5))
    assert summary(rows) == [("window", "Firefox"), ("click", "left", 5, 5, 1)]


def test_marker_too_late_after_click_stays():
    rows = bundle(click(1.0), focus(1.6, FIREFOX))
    assert no_waits(rows) == [("click", "left", 10, 10, 1), ("window", "Firefox")]


def test_marker_after_other_input_stays():
    rows = bundle(click(1.0), tap(1.1, "a"), focus(1.2, FIREFOX))
    assert no_waits(rows) == [("click", "left", 10, 10, 1), ("key", "a"), ("window", "Firefox")]


def test_marker_never_splits_a_drag():
    rows = bundle(press(0.0, 0, 0), move(0.1, 20, 0), move(0.7, 40, 0), focus(0.8, FIREFOX),
                  move(0.9, 60, 0), release(1.0, 60, 0))
    assert no_waits(rows) == [("down", "left", 0, 0), ("path", ((20, 0), (40, 0), (60, 0))),
                              ("up", "left", 60, 0), ("window", "Firefox")]


def test_marker_never_splits_a_path():
    rows = bundle(move(0.0, 0, 0), move(0.05, 10, 0), focus(0.06, FIREFOX), move(0.1, 20, 0))
    assert summary(rows) == [("path", ((0, 0), (10, 0), (20, 0))), ("window", "Firefox")]


def test_marker_before_activating_click_ends_preceding_path_first():
    rows = bundle(move(0.0, 0, 0), move(0.05, 10, 0), click(0.1, 10, 0), focus(0.2, FIREFOX))
    assert no_waits(rows) == [("path", ((0, 0), (10, 0))), ("window", "Firefox"),
                              ("click", "left", 10, 0, 1)]


def test_consecutive_window_rows_keep_the_last():
    rows = bundle(focus(0.0, XED), focus(0.5, FIREFOX), tap(0.6, "a"))
    assert no_waits(rows) == [("window", "Firefox"), ("key", "a")]


def test_repeated_marker_for_same_window_is_ignored():
    rows = bundle(focus(0.0, XED), tap(0.1, "a"), focus(0.2, XED), tap(0.3, "b"))
    assert no_waits(rows) == [("window", "xed"), ("key", "a"), ("key", "b")]


def test_marker_for_moved_window_is_kept():
    moved = replace(XED, rect=Rect(200, 80, 900, 600), client_rect=Rect(200, 110, 900, 570))
    rows = bundle(focus(0.0, XED), tap(0.1, "a"), focus(0.2, moved))
    assert no_waits(rows) == [("window", "xed"), ("key", "a"), ("window", "xed")]


def test_activating_marker_index_helper():
    buf = [press(0.0), move(0.1, 50, 50), release(0.2), move(0.3, 60, 60), focus(0.4)]
    assert bundling.activating_marker_index(buf, 0) == 4
    assert bundling.activating_marker_index([press(0.0), focus(0.51)], 0) is None
    assert bundling.activating_marker_index([press(0.0), scroll(0.1), focus(0.2)], 0) is None


# ------------------------------------------------------------ general

def test_events_out_of_time_order_are_sorted():
    rows = bundle(tap(0.2, "b"), tap(0.0, "a"))
    assert no_waits(rows) == [("key", "a"), ("key", "b")]


def test_empty_stream():
    assert bundle() == []


def test_rows_have_fresh_ids_and_valid_events():
    from macro_recorder.model.catalogue import spec
    rows = bundle(focus(0.0), move(0.1, 0, 0), move(0.12, 30, 0), click(0.2), scroll(0.5),
                  tap(0.6, "a"), press(1.0, 0, 0), move(1.1, 40, 40), release(1.2, 40, 40))
    assert len({r.id for r in rows}) == len(rows)
    for r in rows:
        for e in r.events:
            assert set(e.params) == set(spec(e.type).keys)


# ----------------------------------------------------- incremental bundling

def _stream():
    return [focus(0.0), move(0.1, 0, 0), move(0.12, 30, 0), *click(0.2, 30, 0), focus(0.3, FIREFOX),
            scroll(0.5), scroll(0.52), *tap(0.6, "a"), press(1.0, 0, 0), move(1.1, 40, 40),
            release(1.2, 40, 40), *tap(2.0, "b"), *click(2.5), *click(2.7)]


def test_incremental_feed_matches_one_shot():
    settings = RecordingSettings()
    stream = _stream()
    b = StreamBundler(settings)
    for i, e in enumerate(stream):
        b.feed([e])
        b.advance(e.time)
        b.provisional()
    b.finish()
    assert summary(b.rows()) == summary(rows_from_stream(stream, settings))


def test_provisional_rows_converge_and_keep_stable_ids():
    settings = RecordingSettings()
    stream = _stream()
    b = StreamBundler(settings)
    stable_ids: list[int] = []
    for e in stream:
        b.feed([e])
        b.advance(e.time)
        rows = b.provisional()
        cached = [r.id for r in rows[:len(b._cache_rows)]]
        assert cached[:len(stable_ids)] == stable_ids     # a stable row never changes
        stable_ids = cached
    b.advance(10.0)
    final = b.provisional()
    assert summary(final) == summary(rows_from_stream(stream, settings))
    assert [r.id for r in final[:len(stable_ids)]] == stable_ids
    assert len(stable_ids) >= len(final) // 2


def test_press_is_held_back_until_marker_window_passes():
    b = StreamBundler(RecordingSettings())
    b.feed(click(1.0))
    b.advance(1.1)
    assert b.provisional() == []
    b.feed([focus(1.2, FIREFOX)])
    b.advance(1.6)
    assert summary(b.provisional()) == [("window", "Firefox"), ("click", "left", 10, 10, 1)]


@pytest.mark.parametrize("n", [2000])
def test_provisional_work_per_poll_is_bounded(n):
    """Each poll builds only the tail; the cached prefix grows monotonically."""
    b = StreamBundler(RecordingSettings())
    t = 0.0
    for i in range(n):
        t += 0.3
        b.feed(tap(t, "a"))
        b.advance(t + 0.1)
        b.provisional()
        assert b._cache_upto >= len(b._drafts) - 2
