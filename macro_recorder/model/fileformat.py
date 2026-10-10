"""Version 3 macro files: read, write and structural validation.

A file is a JSON object::

    {"version": 3, "filter": ["mouse_move"], "path_mode": "recorded",
     "rows": [{"label": "start", "events": [{"type": "wait", "duration_ms": 900}]}]}

An event dict holds ``type``, the params whose value differs from the
FieldSpec default and ``dt`` when non-zero.  Row fields with default values
are omitted and there are no ids.  Reading rejects anything structurally
wrong and lists every problem with its row number; semantic problems
(expression syntax, labels, missing files) are left to ``validate``.

The clipboard carries rows in the same dict form (:func:`rows_to_dicts`,
:func:`rows_from_dicts`), so paste works across documents and instances.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from macro_recorder.model.catalogue import FILTER_GROUPS, ActionSpec, FieldKind, FieldSpec, spec
from macro_recorder.model.document import PATH_MODES, MacroDocument, Row, new_row_id
from macro_recorder.model.events import EVENT_TYPE_VALUES, EventType, MacroEvent, freeze_value

FORMAT_VERSION = 3

COLOUR_RE = re.compile(r"#[0-9a-fA-F]{6}\Z")

_TOP_KEYS = ("version", "filter", "path_mode", "rows")
# Row fields in the order they are written, with their defaults.
_ROW_DEFAULTS: dict[str, Any] = {
    "label": "", "delay_ms": 0, "enabled": True, "breakpoint": False, "comment": "",
}
_ROW_KEYS = tuple(_ROW_DEFAULTS) + ("events",)


class FormatError(ValueError):
    """A file or clipboard payload is structurally invalid.

    ``problems`` lists every problem found, each naming its row.
    """

    def __init__(self, problems: Sequence[str], source: str | None = None) -> None:
        self.problems = list(problems)
        self.source = source
        head = "%s is not a valid macro file" % source if source else "Not valid macro data"
        super().__init__("%s:\n%s" % (head, "\n".join(self.problems)))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _same(value: Any, default: Any) -> bool:
    """Equal and of the same type, so 1.0 and True are not taken for 1."""
    return type(value) is type(default) and value == default


def _plain(value: Any) -> Any:
    """Tuples to lists for JSON."""
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    return value


def event_to_dict(event: MacroEvent) -> dict[str, Any]:
    """``type``, the non-default params in field order, then ``dt`` when non-zero."""
    out: dict[str, Any] = {"type": str(event.type)}
    defaults = spec(event.type).defaults()
    for key, default in defaults.items():
        value = event.params[key]
        if not _same(value, default):
            out[key] = _plain(value)
    if event.dt:
        out["dt"] = event.dt
    return out


def row_to_dict(row: Row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, default in _ROW_DEFAULTS.items():
        value = getattr(row, key)
        if not _same(value, default):
            out[key] = value
    out["events"] = [event_to_dict(e) for e in row.events]
    return out


def rows_to_dicts(rows: Iterable[Row]) -> list[dict[str, Any]]:
    """Rows in file form, for the clipboard."""
    return [row_to_dict(r) for r in rows]


def document_to_dict(document: MacroDocument) -> dict[str, Any]:
    return {
        "version": FORMAT_VERSION,
        "filter": list(document.filter),
        "path_mode": document.path_mode,
        "rows": rows_to_dicts(document.rows),
    }


def dumps(data: Mapping[str, Any]) -> str:
    """JSON text with one row per line so diffs stay readable."""
    def enc(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)

    head = ", ".join("%s: %s" % (enc(k), enc(v)) for k, v in data.items() if k != "rows")
    rows = data.get("rows", [])
    if not rows:
        return "{%s, \"rows\": []}\n" % head
    body = ",\n".join("  " + enc(r) for r in rows)
    return "{%s, \"rows\": [\n%s\n]}\n" % (head, body)


def save(document: MacroDocument, path: str | os.PathLike[str]) -> None:
    """Write the document atomically.

    Symlinks are resolved so the link survives.  The text goes to
    ``.<name>.tmp`` beside the target, created with the target's mode when it
    exists, then is flushed, fsynced and moved over the target with
    ``os.replace``; the directory is fsynced last.  On failure the target is
    untouched and the temporary file removed.  The caller marks the document
    saved only when this returns.
    """
    target = Path(os.path.realpath(path))
    text = dumps(document_to_dict(document))
    tmp = target.with_name(".%s.tmp" % target.name)
    try:
        mode: int | None = target.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = None
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(tmp, flags, 0o666)
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(target.parent)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _describe(value: Any) -> str:
    """Short JSON-like text for a value in a problem message."""
    try:
        text = json.dumps(_plain(value), ensure_ascii=True)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _number_problem(field: FieldSpec, key: str, value: Any, default: Any) -> str | None:
    """Why a value is not a number, an allowed expression or an allowed None."""
    if value is None:
        return None if default is None else "%s needs a value" % key
    if _is_number(value):
        return None
    if isinstance(value, str) and field.expr_allowed:
        return None
    wanted = "a number or an expression" if field.expr_allowed else "a number"
    return "%s must be %s, not %s" % (key, wanted, _describe(value))


def param_problems(field: FieldSpec, params: Mapping[str, Any]) -> list[str]:
    """Kind problems for one field's keys in ``params`` (all keys present)."""
    kind = field.kind
    defaults = field.defaults()
    out: list[str] = []
    if kind in (FieldKind.NUMBER, FieldKind.DURATION, FieldKind.COORD_PAIR):
        for key in field.keys:
            problem = _number_problem(field, key, params[key], defaults[key])
            if problem:
                out.append(problem)
    elif kind == FieldKind.REGION:
        *corners, monitor_key = field.keys
        for key in corners:
            problem = _number_problem(field, key, params[key], defaults[key])
            if problem:
                out.append(problem)
        monitor = params[monitor_key]
        if monitor is not None and (isinstance(monitor, bool) or not isinstance(monitor, int)):
            out.append("%s must be a whole number or null, not %s" % (monitor_key, _describe(monitor)))
    elif kind == FieldKind.CHOICE:
        value = params[field.key]
        if not isinstance(value, str) or value not in field.choices:
            out.append("%s must be one of %s, not %s" % (
                field.key, ", ".join(field.choices), _describe(value)))
    elif kind == FieldKind.BOOL:
        if not isinstance(params[field.key], bool):
            out.append("%s must be true or false, not %s" % (field.key, _describe(params[field.key])))
    elif kind == FieldKind.COLOUR:
        value = params[field.key]
        if not isinstance(value, str) or not COLOUR_RE.match(value):
            out.append("%s must be a colour like #ff8800, not %s" % (field.key, _describe(value)))
    elif kind == FieldKind.LABEL_TARGET:
        value = params[field.key]
        if value is not None and not isinstance(value, str):
            out.append("%s must be a label or null, not %s" % (field.key, _describe(value)))
    elif kind != FieldKind.INFO:
        value = params[field.key]
        if not isinstance(value, str) and not (value is None and field.default is None):
            out.append("%s must be text, not %s" % (field.key, _describe(value)))
    return out


def _event_from_dict(data: Any, first: bool, problems: list[str]) -> MacroEvent | None:
    """One event, or None after appending its problems (without row prefix)."""
    if not isinstance(data, dict):
        problems.append("an event must be an object, not %s" % _describe(data))
        return None
    type_ = data.get("type")
    if not isinstance(type_, str) or type_ not in EVENT_TYPE_VALUES:
        problems.append("unknown event type %s" % _describe(type_))
        return None
    action: ActionSpec = spec(type_)
    before = len(problems)
    params = action.defaults()
    unknown = [k for k in data if k not in params and k not in ("type", "dt")]
    if unknown:
        problems.append("%s has unknown key(s) %s" % (type_, ", ".join(sorted(unknown))))
    for key in params:
        if key in data:
            params[key] = freeze_value(data[key])
    dt = data.get("dt", 0.0)
    if not _is_number(dt) or dt < 0:
        problems.append("%s dt must be a non-negative number, not %s" % (type_, _describe(dt)))
    elif first and dt != 0:
        problems.append("%s dt must be 0 for a row's first event" % type_)
    for field in action.fields:
        for problem in param_problems(field, params):
            problems.append("%s %s" % (type_, problem))
    if len(problems) != before:
        return None
    return MacroEvent(type_, params, float(dt))


def _row_from_dict(data: Any, problems: list[str]) -> Row | None:
    """One row with a fresh id, or None after appending its problems."""
    if not isinstance(data, dict):
        problems.append("a row must be an object, not %s" % _describe(data))
        return None
    before = len(problems)
    unknown = [k for k in data if k not in _ROW_KEYS]
    if unknown:
        problems.append("unknown key(s) %s" % ", ".join(sorted(unknown)))
    fields: dict[str, Any] = {}
    for key, default in _ROW_DEFAULTS.items():
        value = data.get(key, default)
        if key == "delay_ms":
            ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
            wanted = "a whole number of ms, 0 or more"
        else:
            ok = type(value) is type(default)
            wanted = "text" if isinstance(default, str) else "true or false"
        if not ok:
            problems.append("%s must be %s, not %s" % (key, wanted, _describe(value)))
        fields[key] = value
    raw_events = data.get("events")
    events: list[MacroEvent] = []
    if not isinstance(raw_events, list) or not raw_events:
        problems.append("events must be a non-empty list")
    else:
        for i, raw in enumerate(raw_events):
            event = _event_from_dict(raw, i == 0, problems)
            if event is not None:
                events.append(event)
        if len(raw_events) > 1 and any(isinstance(e, dict) and e.get("type") != EventType.MOUSE_MOVE
                                       for e in raw_events):
            problems.append("only mouse_move events may share a row")
    if len(problems) != before:
        return None
    return Row(id=new_row_id(), events=tuple(events), **fields)


def rows_from_dicts(dicts: Any) -> list[Row]:
    """Rows from file-form dicts (the clipboard), each with a fresh id.

    Raises FormatError listing every problem, rows numbered from 1.
    """
    problems: list[str] = []
    rows = _rows_from_list(dicts, problems)
    if problems:
        raise FormatError(problems)
    return rows


def _rows_from_list(dicts: Any, problems: list[str]) -> list[Row]:
    if not isinstance(dicts, list):
        problems.append("rows must be a list")
        return []
    rows: list[Row] = []
    for number, data in enumerate(dicts, 1):
        row_problems: list[str] = []
        row = _row_from_dict(data, row_problems)
        problems.extend("Row %d: %s" % (number, p) for p in row_problems)
        if row is not None:
            rows.append(row)
    return rows


def document_from_dict(data: Any, path: str | None = None) -> MacroDocument:
    """A document from a parsed file.  Raises FormatError listing every problem."""
    if not isinstance(data, dict):
        raise FormatError(["the top level must be an object"], path)
    version = data.get("version")
    if not _same(version, FORMAT_VERSION):
        raise FormatError(["version must be %d, not %s" % (FORMAT_VERSION, _describe(version))], path)
    problems: list[str] = []
    unknown = [k for k in data if k not in _TOP_KEYS]
    if unknown:
        problems.append("unknown key(s) %s" % ", ".join(sorted(unknown)))
    groups = data.get("filter", [])
    if not isinstance(groups, list) or any(g not in FILTER_GROUPS for g in groups):
        problems.append("filter must be a list of %s, not %s" % (", ".join(FILTER_GROUPS), _describe(groups)))
        groups = []
    path_mode = data.get("path_mode", "recorded")
    if path_mode not in PATH_MODES:
        problems.append("path_mode must be one of %s, not %s" % (", ".join(PATH_MODES), _describe(path_mode)))
    if "rows" not in data:
        problems.append("rows is missing")
    rows = _rows_from_list(data.get("rows", []), problems)
    if problems:
        raise FormatError(problems, path)
    ordered = tuple(g for g in FILTER_GROUPS if g in groups)
    return MacroDocument(rows, filter=ordered, path_mode=path_mode, path=path)


def load(path: str | os.PathLike[str]) -> MacroDocument:
    """Read a version 3 file into a clean document whose ``path`` is ``path``.

    Raises OSError when the file cannot be read and FormatError when it is
    not a valid macro file.
    """
    name = os.fspath(path)
    with open(name, "rb") as f:
        raw = f.read()
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except UnicodeDecodeError as e:
        raise FormatError(["the file is not UTF-8 text (byte %d)" % e.start], name) from None
    except ValueError as e:
        raise FormatError(["bad JSON: %s" % e], name) from None
    return document_from_dict(data, name)


def _reject_constant(name: str) -> Any:
    raise ValueError("%s is not allowed" % name)
