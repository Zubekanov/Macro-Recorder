"""Semantic checks: labels, targets, expressions, names, files, ranges, keys."""

from __future__ import annotations

import pytest

from macro_recorder.model.catalogue import make
from macro_recorder.model.document import MacroDocument, Row, make_row
from macro_recorder.model.validate import Problem, check, names_assigned


def doc(*rows: Row, path: str | None = None) -> MacroDocument:
    return MacroDocument(rows, path=path)


def row(type_: str, **params) -> Row:
    return make_row(make(type_, **params))


def found(problems: list[Problem], index: int, key: str | None, fragment: str,
          severity: str = "error") -> bool:
    return any(p.row_index == index and p.key == key and fragment in p.message and p.severity == severity
               for p in problems)


def errors(problems: list[Problem]) -> list[Problem]:
    return [p for p in problems if p.severity == "error"]


def test_clean_document_has_no_problems(tmp_path):
    (tmp_path / "button.png").write_bytes(b"png")
    d = doc(
        make_row(make("window_focus", window="xed"), label="top"),
        row("var_set", var_name="x", expr="x + 1"),
        row("mouse_click", x="win_w / 2", y="win_h - 10"),
        row("mouse_scroll"),
        row("key_combo", keys="ctrl+shift+s"),
        row("key_press", key="Shift_L"),
        row("type_text", text="x is {x}, pointer {mouse_x}", char_delay_ms=20),
        row("wait", duration_ms="x * 100"),
        row("match_image", image_path="button.png"),
        row("if_else", expr="found and x < 10", target="top"),
        row("repeat", target="top", count=3),
        row("message_box", buttons="yes_no", target_yes="top"),
        row("exec_program", command="echo {x}"),
        row("goto", target="end"),
        path=str(tmp_path / "m.json"),
    )
    assert check(d) == []


def test_problem_text_and_identity():
    d = doc(row("goto", target="nowhere"))
    (p,) = check(d)
    assert p.row_index == 0 and p.row_id == d.rows[0].id and p.key == "target"
    assert str(p) == "Row 1: Go to: label 'nowhere' does not exist"


# ------------------------------------------------------------------ labels

def test_label_rules():
    d = doc(make_row(make("beep"), label="loop"),
            make_row(make("beep"), label="LOOP"),
            make_row(make("beep"), label="Start"),
            make_row(make("beep"), label="1abc"),
            make_row(make("beep"), label="off", enabled=False))
    p = check(d)
    assert not any(x.row_index == 0 for x in p)
    assert found(p, 1, "label", "already used by row 1")
    assert found(p, 2, "label", "reserved")
    assert found(p, 3, "label", "Labels start with a letter")
    assert found(p, 4, "label", "disabled row", "warning")
    assert not errors([x for x in p if x.row_index == 4])


# ----------------------------------------------------------------- targets

def test_label_targets():
    d = doc(make_row(make("beep"), label="Top"),
            row("goto", target="top"),                       # case-insensitive
            row("goto", target="START"),
            row("goto", target="end"),
            row("goto", target="missing"),
            row("goto", target=None),
            row("if_else", expr="1", target="top", else_target=None),
            row("message_box", buttons="ok_cancel", target_ok=None, target_cancel=None),
            row("wait_pixel", on_timeout="abort", target="ghost"),   # hidden: not checked
            row("wait_pixel", on_timeout="goto", target="ghost"))
    p = check(d)
    assert [x.row_index for x in p] == [4, 5, 9]
    assert found(p, 4, "target", "label 'missing' does not exist")
    assert found(p, 5, "target", "choose a label")
    assert found(p, 9, "target", "label 'ghost' does not exist")


def test_repeat_target_must_be_at_or_above():
    d = doc(make_row(make("beep"), label="above"),
            make_row(make("repeat", target="above")),
            make_row(make("repeat", target="self"), label="self"),
            make_row(make("repeat", target="Start")),
            make_row(make("repeat", target="below")),
            make_row(make("repeat", target="End")),
            make_row(make("repeat", target=None)),
            make_row(make("beep"), label="below"))
    p = check(d)
    assert [x.row_index for x in p] == [4, 5, 6]
    assert found(p, 4, "target", "label 'below' is below this row")
    assert found(p, 5, "target", "label 'End' is below this row")
    assert found(p, 6, "target", "choose a label")


def test_huge_integers_are_reported_not_raised():
    d = doc(row("wait", duration_ms=10 ** 400),
            row("mouse_click", x=10 ** 400, y=1),
            make_row(make("beep"), delay_ms=10 ** 400))
    p = check(d)
    assert found(p, 0, "duration_ms", "Duration_ms is out of range")
    assert found(p, 1, "x", "X is out of range")
    assert found(p, 2, "delay_ms", "whole number")


# ------------------------------------------------------------- expressions

@pytest.mark.parametrize("type_, params, key", [
    ("mouse_scroll", {"dy": "1 +"}, "dy"),
    ("wait", {"duration_ms": "("}, "duration_ms"),
    ("mouse_click", {"x": "a b", "y": "2"}, "x"),
    ("ocr_read", {"x2": "open('f')"}, "x2"),
    ("var_set", {"expr": "x ="}, "expr"),
    ("if_else", {"expr": "x >"}, "expr"),
    ("type_text", {"text": "value {x"}, "text"),
    ("notify", {"text": "{}"}, "text"),
    ("match_text", {"expr": "a } b"}, "expr"),
    ("exec_program", {"command": "echo {1 +}"}, "command"),
    ("message_box", {"text": "{import os}"}, "text"),
    ("wait", {"duration_ms": "2 ** foo(1)"}, "duration_ms"),
])
def test_expression_syntax_errors(type_, params, key):
    p = check(doc(row("var_set", var_name="x", expr="1"), row(type_, **params)))
    assert found(p, 1, key, ""), p


def test_window_launch_is_not_a_template():
    assert check(doc(row("window_focus", window="w", launch="find . -exec ls {} \\;"))) == []


def test_coord_pair_mixing():
    d = doc(row("var_set", var_name="x", expr="1"),
            row("mouse_click", x=10, y="x + 1"),
            row("mouse_scroll", x=10, y=None),
            row("mouse_scroll", x="x", y="x"),
            row("mouse_scroll", x=None, y=None))
    p = check(d)
    assert found(p, 1, "x", "both values must be numbers or both expressions")
    assert found(p, 2, "x", "set both values or neither")
    assert [x.row_index for x in p] == [1, 2]


def test_unassigned_variable_warnings():
    d = doc(row("if_else", expr="count > 0 and mouse_x > win_x", target="End"),
            row("type_text", text="{ghost} at {rt_ms}"),
            row("wait", duration_ms="random * 1000"),
            row("ocr_read", var_name="text"),
            row("notify", text="{text}"),
            row("match_image", image_path="/", capture_var="cx", capture_var_y="cy"),
            row("mouse_click", x="cx", y="cy"),
            row("exec_program", command="true", wait_exit=True, var_name="code"),
            row("message_box", var_name="answer"),
            row("var_set", var_name="total", expr="code + found + len(answer)"))
    p = check(d)
    warnings = [(x.row_index, x.key, x.message) for x in p if x.severity == "warning"]
    assert warnings == [
        (0, "expr", "Variable 'count' is never assigned in this macro"),
        (1, "text", "Variable 'ghost' is never assigned in this macro"),
    ]
    assert names_assigned(d) == {"text", "found", "cx", "cy", "code", "answer", "total"}


def test_names_assigned_counts_every_var_name_field():
    d = doc(row("var_set", var_name="a", expr="1"),
            row("match_text", var_name="hit", capture_var="read"),
            row("exec_program", var_name=""))
    assert names_assigned(d) == {"a", "hit", "read"}


# --------------------------------------------------------- variable names

def test_variable_names():
    d = doc(row("var_set", var_name="2fast", expr="1"),
            row("var_set", var_name="", expr="1"),
            row("var_set", var_name="mouse_x", expr="1"),
            row("var_set", var_name="for", expr="1"),
            row("match_image", image_path="/", capture_var=""),
            row("match_image", image_path="/", var_name=""))
    p = check(d)
    assert found(p, 0, "var_name", "'2fast' is not a valid name")
    assert found(p, 1, "var_name", "a variable name is required")
    assert found(p, 2, "var_name", "read-only dynamic value")
    assert found(p, 3, "var_name", "'for' is not a valid name")
    assert not any(x.row_index == 4 and x.key.startswith("capture") for x in p)
    assert found(p, 5, "var_name", "a variable name is required")


# ------------------------------------------------------------------ files

def test_files_resolve_against_the_document_folder(tmp_path, monkeypatch):
    folder = tmp_path / "macros"
    folder.mkdir()
    (folder / "img.png").write_bytes(b"x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    rows = (row("match_image", image_path="img.png"),
            row("match_image", image_path=str(folder / "img.png")),
            row("match_image", image_path="nope.png"),
            row("match_image", image_path=""),
            row("match_image", image_path=str(folder)))          # a directory is not a file
    p = check(doc(*rows, path=str(folder / "m.json")))
    assert [x.row_index for x in p] == [2, 3, 4]
    assert found(p, 2, "image_path", "nope.png does not exist")
    assert found(p, 3, "image_path", "no file chosen")
    # Unsaved: relative paths resolve against the working directory.
    p = check(doc(*rows))
    assert found(p, 0, "image_path", "img.png does not exist")


# ----------------------------------------------------------------- ranges

@pytest.mark.parametrize("type_, params, key, fragment", [
    ("mouse_click", {"count": 4}, "count", "at most 3"),
    ("mouse_click", {"count": 0}, "count", "at least 1"),
    ("mouse_click", {"count": 1.5}, "count", "whole number"),
    ("match_image", {"image_path": "/", "confidence": 1.5}, "confidence", "at most 1"),
    ("match_text", {"similarity": -0.1}, "similarity", "at least 0"),
    ("wait_pixel", {"colour_tolerance": 300}, "colour_tolerance", "at most 255"),
    ("wait", {"duration_ms": -5}, "duration_ms", "at least 0"),
    ("repeat", {"target": "Start", "count": -1}, "count", "at least 0"),
    ("mouse_scroll", {"dy": 2.5}, "dy", "whole number"),
    ("ocr_read", {"monitor": 0}, "monitor", "Monitor numbers start at 1"),
])
def test_out_of_range_numbers(type_, params, key, fragment):
    p = check(doc(row(type_, **params)))
    assert found(p, 0, key, fragment), p


def test_row_delay_must_be_a_non_negative_int():
    p = check(doc(make_row(make("beep"), delay_ms=-1), make_row(make("beep"), delay_ms=1.5)))
    assert found(p, 0, "delay_ms", "whole number") and found(p, 1, "delay_ms", "whole number")


# ------------------------------------------------------ kinds built in memory

def test_bad_choice_colour_and_kinds():
    d = doc(make_row(make("mouse_click").with_params(button="thumb")),
            make_row(make("wait_pixel").with_params(colour="#ffff")),
            make_row(make("wait_pixel").with_params(colour="#GGGGGG")),
            make_row(make("type_text").with_params(humanise=1)),
            make_row(make("mouse_click").with_params(hold_ms="1 + 1")),
            make_row(make("wait").with_params(bogus=1)))
    p = check(d)
    assert found(p, 0, "button", "must be one of left, right, middle")
    assert found(p, 1, "colour", "must be a colour like #ff8800")
    assert found(p, 2, "colour", "must be a colour like #ff8800")
    assert found(p, 3, "humanise", "must be true or false")
    assert found(p, 4, "hold_ms", "must be a number")
    assert found(p, 5, None, "unknown bogus")


def test_path_points_must_be_numbers():
    path = make_row((make("mouse_move", x=0, y=0), make("mouse_move", x="a", y=1, dt=0.1),
                     make("mouse_move", x=2, y=2, dt=-0.1)))
    p = check(doc(path))
    assert found(p, 0, "x", "Path point 2: x must be a number")
    assert found(p, 0, None, "Path point 3 has a bad time offset")


# ------------------------------------------------------------------- keys

@pytest.mark.parametrize("type_, params, key, fragment", [
    ("key_combo", {"keys": ""}, "keys", "Empty key combination"),
    ("key_combo", {"keys": "a+ctrl"}, "keys", "Only modifiers may precede the key"),
    ("key_combo", {"keys": "   "}, "keys", "Empty key combination"),
    ("key_press", {"key": ""}, "key", "Empty key combination"),
    ("key_release", {"key": "ctrl+a"}, "key", "this field takes one key"),
])
def test_bad_keys(type_, params, key, fragment):
    p = check(doc(row(type_, **params)))
    assert found(p, 0, key, fragment), p


def test_good_keys():
    assert check(doc(row("key_combo", keys="+"), row("key_combo", keys="ctrl++"),
                     row("key_press", key="Control_L"), row("key_release", key="a"))) == []


# --------------------------------------------------------------- type_text

def test_type_text_delay_and_duration_are_exclusive():
    p = check(doc(row("type_text", char_delay_ms=10, duration_ms=500),
                  row("type_text", char_delay_ms="2 * 5", duration_ms=500),
                  row("type_text", char_delay_ms=10, duration_ms=0),
                  row("type_text", char_delay_ms=0, duration_ms=500)))
    assert [x.row_index for x in p] == [0, 1]
    assert found(p, 0, "duration_ms", "not both")
