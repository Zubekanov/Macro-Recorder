"""Version 3 file format: round trip, structural errors, atomic save, clipboard."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from typing import Any, Iterator

import pytest

from macro_recorder.model import catalogue, fileformat, validate
from macro_recorder.model.catalogue import FieldKind, FieldSpec, make
from macro_recorder.model.document import MacroDocument, Row, make_row
from macro_recorder.model.fileformat import FormatError, load, save


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bump(default: Any) -> Any:
    """A number different from ``default``."""
    return 7 if default is None else default + 7


def variants(field: FieldSpec) -> Iterator[dict[str, Any]]:
    """Non-default values for every key of a field, covering each value form."""
    defaults = field.defaults()
    kind = field.kind
    if kind in (FieldKind.NUMBER, FieldKind.DURATION):
        key, default = field.key, field.default
        yield {key: _bump(default)}
        yield {key: 0.125 if default != 0.125 else 0.25}
        if field.expr_allowed:
            yield {key: "speed * 2 + 1"}
        if default is None:
            yield {key: None}
        else:
            yield {key: 0 if default != 0 else 1}
    elif kind == FieldKind.COORD_PAIR:
        a, b = field.keys
        yield {a: 321, b: 654}
        yield {a: 1.5, b: -2.25}
        if field.expr_allowed:
            yield {a: "win_w - 10", b: "win_h / 2"}
        if defaults[a] is None:
            yield {a: None, b: None}
    elif kind == FieldKind.REGION:
        x, y, x2, y2, monitor = field.keys
        yield {x: 11, y: 12.5, x2: 913, y2: 614}
        if field.expr_allowed:
            yield {x: "mouse_x - 5", y: "mouse_y - 5", x2: "mouse_x + 5", y2: "mouse_y + 5"}
        yield {monitor: 2}
    elif kind in (FieldKind.TEXT, FieldKind.TEMPLATE, FieldKind.FILE, FieldKind.COMMAND):
        yield {field.key: "changed {x} caf\u00e9 \"quoted\""}
        yield {field.key: ""} if field.default != "" else {field.key: "z"}
    elif kind == FieldKind.VAR_NAME:
        yield {field.key: "counter_1"}
        if field.default:
            yield {field.key: ""}
    elif kind == FieldKind.CHOICE:
        for choice in field.choices:
            yield {field.key: choice}
    elif kind == FieldKind.BOOL:
        yield {field.key: not field.default}
    elif kind == FieldKind.COLOUR:
        yield {field.key: "#12abEF"}
    elif kind == FieldKind.LABEL_TARGET:
        yield {field.key: "somewhere"}
        yield {field.key: "Start"}
        yield {field.key: None}
        yield {field.key: "End"}
    elif kind == FieldKind.KEY:
        yield {field.key: "Return"}
        yield {field.key: "KP_1"}
    elif kind == FieldKind.KEY_COMBO:
        yield {field.key: "ctrl+shift+s"}
        yield {field.key: "super+F6"}
    elif kind == FieldKind.INFO:
        yield {field.key: (10, 20, 300, 400)}
        yield {field.key: None}
    else:
        raise AssertionError("No variants for %s" % kind)


def all_rows() -> list[Row]:
    """One row per preset and one per field variant of every registered type."""
    rows: list[Row] = []
    for spec in catalogue.all_specs():
        assert spec.presets, spec.type
        for preset in spec.presets:
            rows.append(make_row(preset.make()))
        base = spec.presets[0].make()
        for field in spec.fields:
            for values in variants(field):
                rows.append(make_row(base.with_params(**values)))
    return rows


def strict(value: Any) -> Any:
    """A comparable form that keeps int, float, bool and None apart."""
    if isinstance(value, tuple):
        return ("tuple", tuple(strict(v) for v in value))
    return (type(value).__name__, value)


def row_key(row: Row) -> tuple:
    events = tuple((e.type, strict(e.dt), tuple(sorted((k, strict(v)) for k, v in e.params.items())))
                   for e in row.events)
    return (events, row.label, row.comment, row.enabled, row.breakpoint, strict(row.delay_ms))


def write_json(tmp_path, data: Any, name: str = "m.json") -> str:
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return str(path)


def doc_with(rows: list[dict]) -> dict:
    return {"version": 3, "rows": rows}


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_every_field_of_every_type_round_trips(tmp_path):
    rows = all_rows()
    # Row fields with non-default values on some rows.
    rows[0] = replace(rows[0], label="first", comment="note", enabled=False, breakpoint=True, delay_ms=60)
    rows[1] = replace(rows[1], label="Second label-2", delay_ms=1)
    path_row = make_row(tuple(make("mouse_move", x=i, y=2 * i, dt=0.0 if i == 0 else 0.012 * i)
                              for i in range(5)), comment="path")
    rows.append(path_row)
    doc = MacroDocument(rows, filter=("mouse_move", "wait"), path_mode="spline")
    target = tmp_path / "all.json"
    save(doc, target)
    loaded = load(target)

    assert [row_key(r) for r in loaded.rows] == [row_key(r) for r in rows]
    assert loaded.filter == ("mouse_move", "wait")
    assert loaded.path_mode == "spline"
    assert loaded.path == str(target)
    assert not loaded.dirty and not loaded.can_undo
    assert {r.id for r in loaded.rows}.isdisjoint({r.id for r in rows})     # fresh ids
    # Saving the loaded document gives the same bytes.
    again = tmp_path / "again.json"
    save(loaded, again)
    assert again.read_text(encoding="utf-8") == target.read_text(encoding="utf-8")


def test_every_type_and_kind_is_covered():
    rows = all_rows()
    assert {r.type for r in rows} == {s.type for s in catalogue.all_specs()}
    kinds = {f.kind for s in catalogue.all_specs() for f in s.fields}
    assert kinds >= {FieldKind.NUMBER, FieldKind.COORD_PAIR, FieldKind.REGION, FieldKind.DURATION,
                     FieldKind.TEXT, FieldKind.TEMPLATE, FieldKind.CHOICE, FieldKind.FILE,
                     FieldKind.COMMAND, FieldKind.BOOL, FieldKind.COLOUR, FieldKind.LABEL_TARGET,
                     FieldKind.VAR_NAME, FieldKind.KEY, FieldKind.KEY_COMBO, FieldKind.INFO}


def test_written_dicts_omit_defaults():
    rows = [make_row(make("wait")),
            make_row(make("mouse_click", x=240, y=300), delay_ms=60),
            make_row(make("wait_pixel", x=10, y=10, colour="#ff0000", timeout_ms=5000,
                          on_timeout="goto", target="top"),
                     enabled=False, breakpoint=True, comment="skip for now"),
            make_row((make("mouse_move", x=1, y=2), make("mouse_move", x=5, y=9, dt=0.012)))]
    assert fileformat.rows_to_dicts(rows) == [
        {"events": [{"type": "wait"}]},
        {"delay_ms": 60, "events": [{"type": "mouse_click", "x": 240, "y": 300}]},
        {"enabled": False, "breakpoint": True, "comment": "skip for now",
         "events": [{"type": "wait_pixel", "x": 10, "y": 10, "colour": "#ff0000",
                     "timeout_ms": 5000, "on_timeout": "goto", "target": "top"}]},
        {"events": [{"type": "mouse_move", "x": 1, "y": 2},
                    {"type": "mouse_move", "x": 5, "y": 9, "dt": 0.012}]},
    ]
    # A float equal to an int default is still written, so its type survives.
    assert fileformat.event_to_dict(make("wait", duration_ms=500.0)) == {"type": "wait", "duration_ms": 500.0}


def test_document_dict_shape():
    doc = MacroDocument([make_row(make("beep"), label="top")], filter=("wait",), path_mode="natural")
    assert fileformat.document_to_dict(doc) == {
        "version": 3, "filter": ["wait"], "path_mode": "natural",
        "rows": [{"label": "top", "events": [{"type": "beep"}]}],
    }


def test_design_example_loads(tmp_path):
    text = """{
      "version": 3,
      "filter": ["mouse_move"],
      "path_mode": "recorded",
      "rows": [
        {"label": "top",
         "events": [{"type": "window_focus", "window": "xed", "match_mode": "substring",
                     "rect": [100, 80, 900, 600], "client_rect": [100, 110, 900, 570]}]},
        {"delay_ms": 60,
         "events": [{"type": "mouse_click", "x": 240, "y": 300, "button": "left"}]},
        {"enabled": false, "breakpoint": true, "comment": "skip for now",
         "events": [{"type": "wait_pixel", "x": 10, "y": 10, "colour": "#ff0000",
                     "timeout_ms": 5000, "on_timeout": "goto", "target": "top"}]},
        {"events": [{"type": "mouse_move", "x": 1, "y": 2},
                    {"type": "mouse_move", "x": 5, "y": 9, "dt": 0.012}]}
      ]
    }"""
    doc = load(write_json(tmp_path, text))
    assert len(doc.rows) == 4
    assert doc.rows[0].event["rect"] == (100, 80, 900, 600)
    assert doc.rows[0].label == "top"
    assert doc.rows[2].event["target"] == "top" and not doc.rows[2].enabled
    assert doc.rows[3].is_path and doc.rows[3].events[1].dt == 0.012
    assert doc.rows[1].event["hold_ms"] == 50       # defaults filled in
    assert [str(p) for p in validate.check(doc)] == []


@pytest.mark.parametrize("event, row_extra, fragment", [
    ({"type": "wait", "duration_ms": 10 ** 400}, {}, "duration_ms is out of range"),
    ({"type": "wait", "duration_ms": -(2 ** 60)}, {}, "duration_ms is out of range"),
    ({"type": "mouse_move", "x": 1, "y": 2}, {"delay_ms": 10 ** 400}, "delay_ms must be"),
])
def test_huge_integers_are_reported(tmp_path, event, row_extra, fragment):
    text = '{"version": 3, "rows": [%s]}' % json.dumps({**row_extra, "events": [event]})
    with pytest.raises(FormatError) as info:
        load(write_json(tmp_path, text))
    assert any(p.startswith("Row 1: ") and fragment in p for p in info.value.problems)


@pytest.mark.parametrize("key, value", [
    ("client_rect", [1, 2, 3]),
    ("rect", "abcd"),
    ("rect", [1, 2, 3, "x"]),
    ("client_rect", [1, 2, 3, 10 ** 400]),
    ("rect", [1, 2, 3, True]),
])
def test_window_rects_must_be_four_numbers(tmp_path, key, value):
    event = {"type": "window_focus", "window": "xed", key: value}
    with pytest.raises(FormatError) as info:
        load(write_json(tmp_path, json.dumps(doc_with([{"events": [event]}]))))
    assert any(p.startswith("Row 1: ") and "%s must be null or a list of 4 numbers" % key in p
               for p in info.value.problems)


def test_window_rects_load_as_null_or_four_numbers(tmp_path):
    event = {"type": "window_focus", "window": "xed", "rect": [0, 0, 10.5, 20], "client_rect": None}
    doc = load(write_json(tmp_path, json.dumps(doc_with([{"events": [event]}]))))
    assert doc.rows[0].event["rect"] == (0, 0, 10.5, 20)


def test_save_refuses_a_bad_window_rect(tmp_path):
    bad =MacroDocument([make_row(make("window_focus", window="xed", rect=(1, 2, 3)))])
    with pytest.raises(FormatError):
        save(bad, tmp_path / "bad.json")
    assert not (tmp_path / "bad.json").exists()


def test_huge_dt_is_reported(tmp_path):
    events = [{"type": "mouse_move", "x": 1, "y": 2},
              {"type": "mouse_move", "x": 3, "y": 4, "dt": 10 ** 400}]
    with pytest.raises(FormatError) as info:
        load(write_json(tmp_path, json.dumps(doc_with([{"events": events}]))))
    assert any("dt must be a non-negative number" in p for p in info.value.problems)


def test_empty_document_round_trips(tmp_path):
    save(MacroDocument(), tmp_path / "empty.json")
    doc = load(tmp_path / "empty.json")
    assert doc.rows == () and doc.filter == () and doc.path_mode == "recorded"


def test_filter_is_normalised_and_optional_keys_default(tmp_path):
    doc = load(write_json(tmp_path, {"version": 3, "filter": ["wait", "mouse_move", "wait"], "rows": []}))
    assert doc.filter == ("mouse_move", "wait")
    assert doc.path_mode == "recorded"


# ---------------------------------------------------------------------------
# Structural errors
# ---------------------------------------------------------------------------

def test_every_structural_problem_is_listed_with_its_row(tmp_path):
    rows = [
        {"events": [{"type": "wait"}]},                                         # 1 fine
        {"events": [{"type": "teleport"}]},                                     # 2
        {"events": [{"type": "wait", "colour": "#fff"}]},                       # 3
        {"events": [{"type": "mouse_click", "button": "thumb", "count": "two"}]},  # 4
        {"events": [{"type": "wait", "duration_ms": True}]},                    # 5
        {"events": [{"type": "wait_pixel", "colour": "red"}]},                  # 6
        {"events": [{"type": "type_text", "humanise": "yes"}]},                 # 7
        {"events": [{"type": "mouse_click", "x": None}]},                       # 8
        {"events": [{"type": "mouse_click", "hold_ms": "x * 2"}]},              # 9 hold_ms has no expressions
        {"events": [{"type": "wait"}, {"type": "wait"}]},                       # 10
        {"events": [{"type": "mouse_move"}, {"type": "mouse_move", "dt": -0.1}]},  # 11
        {"events": [{"type": "mouse_move", "dt": 0.5}]},                        # 12
        {"events": []},                                                         # 13
        {"label": 5, "delay_ms": -1, "enabled": "no", "id": 3,
         "events": [{"type": "beep"}]},                                         # 14
        {"events": [{"type": "ocr_read", "monitor": 1.5}]},                     # 15
        {"events": [{"type": "goto", "target": 3}]},                            # 16
        {"events": [{"type": "key_combo", "keys": ["ctrl", "a"]}]},             # 17
        "not a row",                                                            # 18
        {"events": [{"type": "wait", "duration_ms": 1e400}]},                   # 19
    ]
    with pytest.raises(FormatError) as info:
        load(write_json(tmp_path, json.dumps(doc_with(rows)).replace("Infinity", "1e400")))
    problems = info.value.problems
    text = "\n".join(problems)

    def has(row: int, fragment: str) -> bool:
        return any(p.startswith("Row %d: " % row) and fragment in p for p in problems)

    assert not any(p.startswith("Row 1: ") for p in problems)
    assert has(2, "unknown event type \"teleport\"")
    assert has(3, "unknown key(s) colour")
    assert has(4, "button must be one of left, right, middle, x1, x2")
    assert has(4, "count must be a number")
    assert has(5, "duration_ms must be a number or an expression, not true")
    assert has(6, "colour must be a colour like #ff8800")
    assert has(7, "humanise must be true or false")
    assert has(8, "x needs a value")
    assert has(9, "hold_ms must be a number, not \"x * 2\"")
    assert has(10, "only mouse_move events may share a row")
    assert has(11, "dt must be a non-negative number")
    assert has(12, "dt must be 0 for a row's first event")
    assert has(13, "events must be a non-empty list")
    assert has(14, "unknown key(s) id")
    assert has(14, "label must be text")
    assert has(14, "delay_ms must be a whole number of ms")
    assert has(14, "enabled must be true or false")
    assert has(15, "monitor must be a whole number or null")
    assert has(16, "target must be a label or null")
    assert has(17, "keys must be text")
    assert has(18, "a row must be an object")
    assert has(19, "duration_ms must be a number")
    assert str(tmp_path) in str(info.value)
    assert isinstance(info.value, ValueError)
    assert len(text.splitlines()) == len(problems)


@pytest.mark.parametrize("content, fragment", [
    ("{not json", "bad JSON"),
    ('{"version": 3, "rows": [{"events": [{"type": "wait", "duration_ms": NaN}]}]}', "bad JSON"),
    ("[1, 2]", "top level must be an object"),
    ('{"version": 2, "rows": []}', "version must be 3, not 2"),
    ('{"version": "3", "rows": []}', "version must be 3"),
    ('{"rows": []}', "version must be 3, not null"),
    ('{"version": 3}', "rows is missing"),
    ('{"version": 3, "rows": {}}', "rows must be a list"),
    ('{"version": 3, "rows": [], "extra": 1}', "unknown key(s) extra"),
    ('{"version": 3, "rows": [], "filter": ["clicks"]}', "filter must be a list of"),
    ('{"version": 3, "rows": [], "path_mode": "zigzag"}', "path_mode must be one of"),
])
def test_file_level_errors(tmp_path, content, fragment):
    with pytest.raises(FormatError) as info:
        load(write_json(tmp_path, content))
    assert any(fragment in p for p in info.value.problems), info.value.problems


def test_non_utf8_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(b'{"version": 3, "rows": [\xff]}')
    with pytest.raises(FormatError, match="not UTF-8"):
        load(path)


def test_missing_file_is_os_error(tmp_path):
    with pytest.raises(OSError):
        load(tmp_path / "missing.json")


def test_semantic_problems_still_load(tmp_path):
    rows = [{"label": "a", "events": [{"type": "goto", "target": "nowhere"}]},
            {"label": "a", "events": [{"type": "var_set", "expr": "1 +"}]},
            {"events": [{"type": "match_image", "image_path": "missing.png"}]}]
    doc = load(write_json(tmp_path, doc_with(rows)))
    assert len(doc.rows) == 3


# ---------------------------------------------------------------------------
# Atomic save
# ---------------------------------------------------------------------------

def _doc(ms: int) -> MacroDocument:
    return MacroDocument([make_row(make("wait", duration_ms=ms))])


def test_failure_before_replace_leaves_the_original_intact(tmp_path, monkeypatch):
    target = tmp_path / "macro.json"
    save(_doc(1), target)
    original = target.read_bytes()

    def broken_replace(src, dst):
        raise OSError("disk on fire")
    monkeypatch.setattr(fileformat.os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk on fire"):
        save(_doc(2), target)
    assert target.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["macro.json"]


def test_failure_while_writing_leaves_the_original_intact(tmp_path, monkeypatch):
    target = tmp_path / "macro.json"
    save(_doc(1), target)
    original = target.read_bytes()

    def broken_fsync(fd):
        raise OSError("no space")
    monkeypatch.setattr(fileformat.os, "fsync", broken_fsync)
    with pytest.raises(OSError, match="no space"):
        save(_doc(2), target)
    assert target.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["macro.json"]


def test_save_flushes_file_and_directory(tmp_path, monkeypatch):
    synced: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        synced.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)
    monkeypatch.setattr(fileformat.os, "fsync", recording_fsync)
    save(_doc(1), tmp_path / "m.json")
    assert synced == ["file", "dir"]


def test_save_keeps_the_target_mode(tmp_path):
    target = tmp_path / "private.json"
    save(_doc(1), target)
    os.chmod(target, 0o640)
    save(_doc(2), target)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640
    assert load(target).rows[0].event["duration_ms"] == 2


def test_new_file_uses_the_umask(tmp_path):
    old = os.umask(0o027)
    try:
        save(_doc(1), tmp_path / "new.json")
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(tmp_path / "new.json").st_mode) == 0o640


def test_save_through_a_symlink_replaces_the_real_file(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real = real_dir / "macro.json"
    save(_doc(1), real)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    save(_doc(2), link)
    assert link.is_symlink()
    assert load(real).rows[0].event["duration_ms"] == 2
    assert sorted(p.name for p in real_dir.iterdir()) == ["macro.json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["link.json", "real"]


@pytest.mark.parametrize("row", [
    make_row(make("beep"), delay_ms=1.5),
    make_row(make("wait", duration_ms=True)),
    make_row(make("wait", duration_ms=10 ** 400)),
])
def test_save_refuses_what_load_would_reject(tmp_path, row):
    target = tmp_path / "m.json"
    save(_doc(1), target)
    original = target.read_bytes()
    with pytest.raises(FormatError) as info:
        save(MacroDocument([row]), target)
    assert info.value.problems and info.value.problems[0].startswith("Row 1: ")
    assert target.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.json"]


def test_failure_after_open_closes_the_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "m.json"
    save(_doc(1), target)
    before = len(os.listdir("/proc/self/fd"))

    def broken_fchmod(fd, mode):
        raise OSError("read-only")
    monkeypatch.setattr(fileformat.os, "fchmod", broken_fchmod)
    with pytest.raises(OSError, match="read-only"):
        save(_doc(2), target)
    assert len(os.listdir("/proc/self/fd")) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.json"]


def test_stale_temp_symlink_is_replaced_not_followed(tmp_path):
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("keep")
    (tmp_path / ".m.json.tmp").symlink_to(elsewhere)
    save(_doc(4), tmp_path / "m.json")
    assert load(tmp_path / "m.json").rows[0].event["duration_ms"] == 4
    assert elsewhere.read_text() == "keep"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["elsewhere.txt", "m.json"]


def test_stale_temp_file_is_overwritten(tmp_path):
    (tmp_path / ".m.json.tmp").write_text("junk")
    save(_doc(3), tmp_path / "m.json")
    assert load(tmp_path / "m.json").rows[0].event["duration_ms"] == 3
    assert not (tmp_path / ".m.json.tmp").exists()


def test_save_does_not_mark_the_document_saved(tmp_path):
    doc = MacroDocument()
    doc.append([make_row(make("beep"))])
    save(doc, tmp_path / "m.json")
    assert doc.dirty and doc.path is None


def test_written_file_has_one_row_per_line(tmp_path):
    doc = MacroDocument([make_row(make("beep")), make_row(make("wait"))])
    save(doc, tmp_path / "m.json")
    lines = (tmp_path / "m.json").read_text(encoding="utf-8").splitlines()
    assert lines == ['{"version": 3, "filter": [], "path_mode": "recorded", "rows": [',
                     '  {"events": [{"type": "beep"}]},',
                     '  {"events": [{"type": "wait"}]}',
                     ']}']


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

def test_clipboard_round_trip_gives_fresh_ids():
    rows = [make_row(make("mouse_click", x="win_w / 2", y=40), label="here", comment="c", delay_ms=5),
            make_row((make("mouse_move", x=0, y=0), make("mouse_move", x=3, y=4, dt=0.02))),
            make_row(make("window_focus", window="xed", rect=(1, 2, 3, 4)))]
    payload = json.loads(json.dumps(fileformat.rows_to_dicts(rows)))
    pasted = fileformat.rows_from_dicts(payload)
    assert [row_key(r) for r in pasted] == [row_key(r) for r in rows]
    assert {r.id for r in pasted}.isdisjoint({r.id for r in rows})
    assert len({r.id for r in pasted}) == 3


def test_clipboard_errors_name_rows():
    with pytest.raises(FormatError) as info:
        fileformat.rows_from_dicts([{"events": [{"type": "wait"}]}, {"events": [{"type": "nope"}]}])
    assert info.value.problems == ['Row 2: unknown event type "nope"']
    with pytest.raises(FormatError, match="rows must be a list"):
        fileformat.rows_from_dicts({"events": []})


def test_document_from_dict_sets_path():
    doc = fileformat.document_from_dict({"version": 3, "rows": [{"events": [{"type": "beep"}]}]},
                                        path="/tmp/x.json")
    assert doc.path == "/tmp/x.json" and doc.rows[0].type == "beep"
