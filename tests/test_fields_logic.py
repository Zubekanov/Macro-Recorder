"""Tk-free parsing and validation behind the Details panel's field widgets."""

from __future__ import annotations

import os

import pytest

from macro_recorder.gui import details
from macro_recorder.gui import fields as f
from macro_recorder.gui.fields import FieldError
from macro_recorder.gui.targeting import Capture
from macro_recorder.model.catalogue import FieldKind, FieldSpec, make, spec
from macro_recorder.model.document import make_row


def field(type_: str, key: str) -> FieldSpec:
    return spec(type_).field_for(key)


# ------------------------------------------------------------------ numbers

@pytest.mark.parametrize("value, text", [
    (None, ""), (0, "0"), (500, "500"), (12.5, "12.5"), (0.1234, "0.123"), (2.0, "2"),
    (1.10, "1.1"), (-0.0001, "0"), ("x + 1", "x + 1"), (True, "1"),
])
def test_format_number(value, text):
    assert f.format_number(value) == text


def test_number_parses_ints_floats_and_expressions():
    duration = field("wait", "duration_ms")
    assert f.parse_number_field("250", duration).values == {"duration_ms": 250}
    assert isinstance(f.parse_number_field(" 250 ", duration).values["duration_ms"], int)
    assert f.parse_number_field("12.5", duration).values == {"duration_ms": 12.5}
    parsed = f.parse_number_field("delay * 2", duration)
    assert parsed.values == {"duration_ms": "delay * 2"}
    assert parsed.names == frozenset({"delay"})


def test_number_range_and_integer_rules():
    count = field("mouse_click", "count")           # 1..3, integer, numbers only
    assert f.parse_number_field("3", count).values == {"count": 3}
    assert f.parse_number_field("2.0", count).values == {"count": 2}
    with pytest.raises(FieldError, match="at most 3"):
        f.parse_number_field("4", count)
    with pytest.raises(FieldError, match="at least 1"):
        f.parse_number_field("0", count)
    with pytest.raises(FieldError, match="whole number"):
        f.parse_number_field("1.5", count)
    with pytest.raises(FieldError, match="must be a number"):
        f.parse_number_field("n", count)


def test_number_blank_needs_a_none_default():
    with pytest.raises(FieldError, match="needs a value"):
        f.parse_number_field("", field("wait", "duration_ms"))
    timeout = field("window_focus", "timeout_ms")   # default None
    assert f.parse_number_field("  ", timeout).values == {"timeout_ms": None}


def test_number_rejects_bad_expressions_and_huge_numbers():
    duration = field("wait", "duration_ms")
    with pytest.raises(FieldError):
        f.parse_number_field("1 +", duration)
    with pytest.raises(FieldError, match="out of range"):
        f.parse_number_field("9" * 30, duration)


def test_negative_numbers_respect_minimum():
    with pytest.raises(FieldError, match="at least 0"):
        f.parse_number_field("-5", field("wait", "duration_ms"))
    assert f.parse_number_field("-5", field("mouse_scroll", "dy")).values == {"dy": -5}


# ------------------------------------------------------------------ pairs

def test_pair_numbers_and_expressions():
    pos = field("mouse_click", "x")
    assert f.parse_pair("10", "20", pos).values == {"x": 10, "y": 20}
    parsed = f.parse_pair("left + 5", "top", pos)
    assert parsed.values == {"x": "left + 5", "y": "top"}
    assert parsed.names == frozenset({"left", "top"})


def test_pair_rejects_a_mix():
    with pytest.raises(FieldError, match="both values must be numbers or both expressions"):
        f.parse_pair("10", "top", field("mouse_click", "x"))


def test_pair_blank_only_where_the_default_is_none():
    with pytest.raises(FieldError, match="needs a value"):
        f.parse_pair("", "", field("mouse_click", "x"))
    scroll = field("mouse_scroll", "x")
    assert f.parse_pair("", "", scroll).values == {"x": None, "y": None}
    with pytest.raises(FieldError, match="both values or neither"):
        f.parse_pair("5", "", scroll)


def test_path_start_takes_numbers_only():
    row = make_row([make("mouse_move", x=1, y=2), make("mouse_move", x=5, y=6, dt=0.1)])
    start = spec("mouse_move").layout_for(row)[0]
    assert f.parse_pair("3", "4", start).values == {"x": 3, "y": 4}
    with pytest.raises(FieldError, match="must be a number"):
        f.parse_pair("a", "b", start)


# ------------------------------------------------------------------ regions

def test_region_mode_detection():
    region = field("ocr_read", "x")
    assert f.REGION_MODES == (f.REGION_RECTANGLE, f.REGION_MONITOR)
    assert f.region_mode({"x": 1, "y": 2, "x2": 3, "y2": 4, "monitor": None}, region) == f.REGION_RECTANGLE
    assert f.region_mode({"x": 1, "y": 2, "x2": 3, "y2": 4, "monitor": 2}, region) == f.REGION_MONITOR
    assert f.region_mode({"x": 0, "y": 0, "x2": 1920, "y2": 1080, "monitor": None}, region) == \
        f.REGION_RECTANGLE


def test_region_rectangle():
    region = field("ocr_read", "x")
    current = {"x": 1, "y": 2, "x2": 3, "y2": 4, "monitor": 1}
    parsed = f.parse_region(f.REGION_RECTANGLE, ("10", "20", "w", "40"), "1", region, current)
    assert parsed.values == {"x": 10, "y": 20, "x2": "w", "y2": 40, "monitor": None}
    assert parsed.names == frozenset({"w"})
    with pytest.raises(FieldError, match="needs a value"):
        f.parse_region(f.REGION_RECTANGLE, ("10", "", "30", "40"), "1", region, current)


def test_region_monitor_keeps_corners():
    region = field("ocr_read", "x")
    current = {"x": 1, "y": 2, "x2": 3, "y2": 4, "monitor": None}
    parsed = f.parse_region(f.REGION_MONITOR, ("", "", "", ""), "2", region, current)
    assert parsed.values == {"x": 1, "y": 2, "x2": 3, "y2": 4, "monitor": 2}
    for bad in ("0", "", "x", "-1"):
        with pytest.raises(FieldError, match="start at 1"):
            f.parse_region(f.REGION_MONITOR, ("", "", "", ""), bad, region, current)


def test_region_rejects_an_unknown_mode():
    with pytest.raises(FieldError, match="Unknown region mode"):
        f.parse_region("Ellipse", ("", "", "", ""), "", field("ocr_read", "x"), {})


# ------------------------------------------------------------------ text kinds

def test_colour():
    assert f.parse_colour("#FF8000") == "#ff8000"
    assert f.parse_colour(" #00aa11 ") == "#00aa11"
    for bad in ("ff8000", "#ff80", "#gg0000", "", "red"):
        with pytest.raises(FieldError, match="#rrggbb"):
            f.parse_colour(bad)
    assert f.parse_colour_field("#ABCDEF", field("wait_pixel", "colour")).values == {"colour": "#abcdef"}


def test_template_braces():
    text = field("type_text", "text")
    parsed = f.parse_template_field("Hello {name}, {{literal}}", text)
    assert parsed.values == {"text": "Hello {name}, {{literal}}"}
    assert parsed.names == frozenset({"name"})
    for bad in ("{unclosed", "stray }", "{}", "{1 +}"):
        with pytest.raises(FieldError):
            f.parse_template_field(bad, text)


def test_text_holds_an_expression_only_where_the_action_evaluates_one():
    expr = field("var_set", "expr")
    parsed = f.parse_text_field("count + 1", expr, "var_set")
    assert parsed.names == frozenset({"count"})
    with pytest.raises(FieldError):
        f.parse_text_field("count +", expr, "var_set")
    with pytest.raises(FieldError, match="needs an expression"):
        f.parse_text_field(" ", expr, "var_set")
    title = field("window_focus", "window")
    assert f.parse_text_field("{not checked", title, "window_focus").values == {"window": "{not checked"}


def test_command_is_a_template_for_exec_program_only():
    command = field("exec_program", "command")
    assert f.parse_command_field("echo {n}", command, "exec_program").names == frozenset({"n"})
    with pytest.raises(FieldError):
        f.parse_command_field("echo {n", command, "exec_program")
    launch = field("window_focus", "launch")
    assert f.parse_command_field("xed {x", launch, "window_focus").values == {"launch": "xed {x"}


def test_choice():
    buttons = field("message_box", "buttons")
    assert f.choice_text("ok_cancel") == "ok cancel"
    assert f.parse_choice("ok cancel", buttons).values == {"buttons": "ok_cancel"}
    assert f.parse_choice("yes_no", buttons).values == {"buttons": "yes_no"}
    with pytest.raises(FieldError):
        f.parse_choice("maybe", buttons)


def test_bool():
    humanise = field("type_text", "humanise")
    assert f.parse_bool("1", humanise).values == {"humanise": True}
    assert f.parse_bool("0", humanise).values == {"humanise": False}


def test_var_name():
    found = field("match_image", "var_name")        # default "found": required
    capture = field("match_image", "capture_var")   # default "": optional
    assert f.parse_var_name(" total ", found).values == {"var_name": "total"}
    assert f.parse_var_name("", capture).values == {"capture_var": ""}
    with pytest.raises(FieldError, match="required"):
        f.parse_var_name("", found)
    for bad in ("1x", "a-b", "for", "x y"):
        with pytest.raises(FieldError, match="letters, digits"):
            f.parse_var_name(bad, found)
    with pytest.raises(FieldError, match="dynamic"):
        f.parse_var_name("mouse_x", found)


def test_keys():
    combo = field("key_combo", "keys")
    key = field("key_press", "key")
    assert f.parse_key_field("Ctrl+Shift+s", combo).values == {"keys": "ctrl+shift+s"}
    assert f.parse_key_field("Control_R+F5", combo).values == {"keys": "ctrl+F5"}
    assert f.parse_key_field("Return", combo).values == {"keys": "Return"}
    assert f.parse_key_field("shift", key).values == {"key": "Shift_L"}
    assert f.parse_key_field("kp_1", key).values == {"key": "KP_1"}
    with pytest.raises(FieldError, match="one key"):
        f.parse_key_field("ctrl+a", key)
    with pytest.raises(FieldError):
        f.parse_key_field("", combo)
    with pytest.raises(FieldError, match="Only modifiers"):
        f.parse_key_field("a+b", combo)


def test_keys_must_be_known_keysyms():
    combo = field("key_combo", "keys")
    key = field("key_press", "key")
    assert f.parse_key_field("XF86AudioMute", key).values == {"key": "XF86AudioMute"}
    assert f.parse_key_field("super+Left", combo).values == {"keys": "super+Left"}
    with pytest.raises(FieldError, match="unknown key 'Retrun'"):
        f.parse_key_field("Retrun", key)
    with pytest.raises(FieldError, match="unknown key 'Fx'"):
        f.parse_key_field("ctrl+Fx", combo)


def test_focus_paths():
    assert f.path_inside(".panel.f1.e0", ".panel.f1")
    assert f.path_inside(".panel.f1", ".panel.f1")
    assert not f.path_inside(".panel.f10", ".panel.f1")
    assert not f.path_inside(".table", ".panel.f1")
    assert f.path_inside(".anything", ".")
    assert not f.widget_inside(None, ".panel")
    assert f.widget_inside(".panel.x", ".panel")


# ------------------------------------------------------------------ label targets

def test_label_target_choices_in_row_order():
    assert f.label_target_choices(["top", "loop"], None, "top") == ["Start", "top", "loop", "End"]
    assert f.label_target_choices(["top"], "(next row)", None) == ["(next row)", "Start", "top", "End"]
    assert f.label_target_choices(["top"], None, "gone") == ["Start", "top", "End", "gone"]
    assert f.label_target_choices(["Top"], None, "top") == ["Start", "Top", "End"]


def test_label_target_parse():
    else_target = field("if_else", "else_target")       # none_label "(next row)"
    target = field("goto", "target")
    assert f.label_target_text(None, "(next row)") == "(next row)"
    assert f.label_target_text("top", "(next row)") == "top"
    assert f.parse_label_target("(next row)", else_target, ["top"]).values == {"else_target": None}
    assert f.parse_label_target("top", target, ["top"]).values == {"target": "top"}
    assert f.parse_label_target("End", target, []).values == {"target": "End"}
    with pytest.raises(FieldError, match="does not exist"):
        f.parse_label_target("gone", target, ["top"])
    with pytest.raises(FieldError, match="choose a label"):
        f.parse_label_target("", field("repeat", "target"), [])


# ------------------------------------------------------------------ files and commands

def test_file_field_warns_when_missing(tmp_path):
    image = field("match_image", "image_path")
    (tmp_path / "button.png").write_bytes(b"")
    assert f.parse_file_field("button.png", image, str(tmp_path)).warning == ""
    assert "does not exist" in f.parse_file_field("other.png", image, str(tmp_path)).warning
    assert "no file" in f.parse_file_field("", image, str(tmp_path)).warning
    assert f.parse_file_field("other.png", image, None).values == {"image_path": "other.png"}


def test_relative_to(tmp_path):
    doc_dir = str(tmp_path)
    inside = os.path.join(doc_dir, "img", "a.png")
    assert f.relative_to(inside, doc_dir) == os.path.join("img", "a.png")
    assert f.relative_to("/usr/share/a.png", doc_dir) == "/usr/share/a.png"
    assert f.relative_to(inside, None) == inside


def test_insert_quoted():
    assert f.insert_quoted("run ", 4, "/tmp/my file.sh") == ("run '/tmp/my file.sh'", 21)
    assert f.insert_quoted("ab", 1, "/x") == ("a/xb", 3)
    assert f.insert_quoted("ab", 99, "/x") == ("ab/x", 4)


def test_format_info_and_labels():
    assert f.format_info(None) == "none"
    assert f.format_info((100, 80, 900, 600)) == "100, 80, 900 x 600"
    assert f.format_info("(1, 2)") == "(1, 2)"
    assert f.field_label(field("wait", "duration_ms")) == "Duration (ms)"
    assert f.field_label(field("mouse_click", "button")) == "Button"


def test_unknown_names_excludes_assigned_and_dynamic():
    names = frozenset({"a", "b", "mouse_x", "loop"})
    assert f.unknown_names(names, {"a"}) == ["b"]
    assert f.unknown_names(frozenset(), set()) == []


def test_every_field_kind_has_a_widget_class():
    assert set(f.FIELD_CLASSES) == set(FieldKind)


# ------------------------------------------------------------------ details helpers

def test_row_values():
    row = make_row(make("wait"), label="a", comment="c", enabled=False, delay_ms=40)
    assert details.row_values(row) == {"enabled": False, "delay_ms": 40}
    assert [s.keys for s in details.ROW_SPECS] == [("enabled",), ("delay_ms",)]


def test_targeting_coord_pair_with_colour():
    pixel = field("wait_pixel", "x")
    assert details.targeting_steps(pixel) == 1
    assert details.targeting_keys(pixel, 0) == ("x", "y", "colour")
    assert details.targeting_values(pixel, Capture(5, 6, "#ABCDEF"), 0) == \
        {"x": 5, "y": 6, "colour": "#abcdef"}
    to = field("mouse_move_timed", "x2")
    assert details.targeting_values(to, Capture(5, 6, "#000000"), 0) == {"x2": 5, "y2": 6}


def test_targeting_region_takes_two_captures():
    region = field("ocr_read", "x")
    assert details.targeting_steps(region) == 2
    assert details.targeting_keys(region, 0) == ("x", "y")
    assert details.targeting_keys(region, 1) == ("x2", "y2")
    assert details.targeting_values(region, Capture(9, 8, "#000000"), 1) == {"x2": 9, "y2": 8}


def test_visibility_and_enablement():
    interval = field("mouse_click", "interval_ms")
    assert not details.field_visible(interval, {"action": "click", "count": 1})
    assert details.field_visible(interval, {"action": "click", "count": 2})
    humanise = field("type_text", "humanise")
    assert not details.field_enabled(humanise, {"char_delay_ms": 0, "duration_ms": 0})
    assert details.field_enabled(humanise, {"char_delay_ms": 10, "duration_ms": 0})
    assert details.field_visible(field("wait", "duration_ms"), {})


def test_panel_title():
    path = make_row([make("mouse_move", x=1, y=2), make("mouse_move", x=3, y=4, dt=0.1)])
    assert details.panel_title(path, 3) == "Row 3 - Mouse Path"
    assert details.panel_title(make_row(make("mouse_click")), 1) == "Row 1 - Mouse Click"
