"""Semantic checks on a document.

The file reader rejects structural problems; this module reports what a
structurally valid document may still get wrong: labels, jump targets,
expression syntax, variable names, files, ranges, key names and a few
per-action rules.  The CLI's ``validate`` prints every problem and the GUI
marks the rows.  Warnings do not stop playback.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Literal, Mapping

from macro_recorder.engine import expressions
from macro_recorder.engine.expressions import ExpressionError
from macro_recorder.model.actions.variables import DYNAMIC_VALUES
from macro_recorder.model.catalogue import RESERVED_TARGETS, FieldKind, FieldSpec, spec
from macro_recorder.model.document import MacroDocument, Row, label_problem
from macro_recorder.model.events import EVENT_TYPE_VALUES, EventType, MacroEvent
from macro_recorder.model.fileformat import is_number, is_whole, param_problems
from macro_recorder.model.keys import parse_combo

Severity = Literal["error", "warning"]

DYNAMIC_NAMES: frozenset[str] = frozenset(name for name, _ in DYNAMIC_VALUES)

# TEXT fields that hold an expression, and COMMAND fields that are templates.
# The catalogue has no field kind for either, so they are named here.
EXPRESSION_TEXT_FIELDS: frozenset[tuple[str, str]] = frozenset({
    (EventType.VAR_SET.value, "expr"),
    (EventType.IF_ELSE.value, "expr"),
})
TEMPLATE_COMMAND_FIELDS: frozenset[tuple[str, str]] = frozenset({
    (EventType.EXEC_PROGRAM.value, "command"),
})

_RESERVED_LOWER = frozenset(t.lower() for t in RESERVED_TARGETS)


@dataclass(frozen=True)
class Problem:
    row_index: int              # 0-based position in the document
    row_id: int
    key: str | None             # field key, "label", "delay_ms", or None for the row
    message: str
    severity: Severity = "error"

    def __str__(self) -> str:
        return "Row %d: %s" % (self.row_index + 1, self.message)


def names_assigned(document: MacroDocument) -> set[str]:
    """Every variable name a VAR_NAME field of any row assigns."""
    names: set[str] = set()
    for row in document.rows:
        for event in row.events:
            if event.type not in EVENT_TYPE_VALUES:
                continue
            for field in spec(event.type).fields:
                if field.kind == FieldKind.VAR_NAME:
                    value = event.params.get(field.key)
                    if isinstance(value, str) and value:
                        names.add(value)
    return names


def check(document: MacroDocument,
          key_known: Callable[[str], bool] | None = None) -> list[Problem]:
    """Every problem in the document, in row order.

    ``key_known`` says whether a keysym name exists (the platform layer's
    keysym table; the model cannot import it).  When given, every key name
    in a KEY or KEY_COMBO field is checked against it.
    """
    checker = _Checker(document, key_known)
    return list(checker.run())


class _Checker:
    def __init__(self, document: MacroDocument,
                 key_known: Callable[[str], bool] | None = None) -> None:
        self.document = document
        self.key_known = key_known
        self.known = names_assigned(document) | DYNAMIC_NAMES
        self.label_rows: dict[str, int] = {}
        for i, row in enumerate(document.rows):
            if row.label and row.label.lower() not in self.label_rows:
                self.label_rows[row.label.lower()] = i
        base = os.path.dirname(os.path.abspath(document.path)) if document.path else None
        self.base_dir = base

    def run(self) -> Iterator[Problem]:
        for index, row in enumerate(self.document.rows):
            for key, message, severity in self._row(index, row):
                yield Problem(index, row.id, key, message, severity)

    # ------------------------------------------------------------------ rows

    def _row(self, index: int, row: Row) -> Iterator[tuple[str | None, str, Severity]]:
        if row.label:
            problem = label_problem(row.label)
            first = self.label_rows.get(row.label.lower())
            if problem:
                yield "label", problem, "error"
            elif first is not None and first != index:
                yield "label", "Label %r is already used by row %d" % (row.label, first + 1), "error"
            if not row.enabled:
                yield ("label", "Label %r is on a disabled row; jumps to it continue at the next "
                       "row that plays" % row.label, "warning")
        if not is_whole(row.delay_ms) or row.delay_ms < 0:
            yield "delay_ms", "Delay must be a whole number of ms, 0 or more", "error"
        if row.events[0].type not in EVENT_TYPE_VALUES:
            yield None, "Unknown action type %r" % row.events[0].type, "error"
            return
        if row.is_path:
            yield from self._path(row)
        else:
            yield from self._event(index, row.events[0])

    def _path(self, row: Row) -> Iterator[tuple[str | None, str, Severity]]:
        for number, event in enumerate(row.events, 1):
            for key in ("x", "y"):
                if not is_number(event.params.get(key)):
                    yield key, "Path point %d: %s must be a number" % (number, key), "error"
            if not is_number(event.dt) or event.dt < 0 or (number == 1 and event.dt != 0):
                yield None, "Path point %d has a bad time offset %r" % (number, event.dt), "error"

    def _event(self, index: int, event: MacroEvent) -> Iterator[tuple[str | None, str, Severity]]:
        action = spec(event.type)
        missing = [k for k in action.keys if k not in event.params]
        unknown = [k for k in event.params if k not in action.keys]
        if missing or unknown:
            yield None, "Fields do not match %s: missing %s, unknown %s" % (
                event.type, ", ".join(missing) or "none", ", ".join(unknown) or "none"), "error"
            return
        for field in action.fields:
            kind_problems = param_problems(field, event.params)
            if kind_problems:
                for message in kind_problems:
                    yield field.key, _capitalise(message), "error"
                continue
            yield from self._field(index, event, field)
        if event.type == EventType.TYPE_TEXT and _nonzero(event["char_delay_ms"]) \
                and _nonzero(event["duration_ms"]):
            yield ("duration_ms", "Set either the delay per character or the total duration, "
                   "not both", "error")

    # ---------------------------------------------------------------- fields

    def _field(self, index: int, event: MacroEvent,
               field: FieldSpec) -> Iterator[tuple[str | None, str, Severity]]:
        kind = field.kind
        params = event.params
        if kind in (FieldKind.NUMBER, FieldKind.DURATION):
            yield from self._numeric(field, field.key, params[field.key])
        elif kind == FieldKind.COORD_PAIR:
            yield from self._coord_pair(field, params)
        elif kind == FieldKind.REGION:
            *corners, monitor_key = field.keys
            for key in corners:
                yield from self._numeric(field, key, params[key])
            monitor = params[monitor_key]
            if monitor is not None and monitor < 1:
                yield monitor_key, "Monitor numbers start at 1", "error"
        elif kind == FieldKind.TEXT:
            if (event.type, field.key) in EXPRESSION_TEXT_FIELDS:
                yield from self._expression(field.key, params[field.key])
            elif (event.type == EventType.WINDOW_FOCUS and field.key == "window"
                  and params.get("match_mode") == "regex"):
                try:
                    re.compile(params[field.key])
                except re.error as e:
                    yield field.key, "%s: bad regular expression: %s" % (field.label, e), "error"
        elif kind == FieldKind.TEMPLATE:
            yield from self._template(field.key, params[field.key])
        elif kind == FieldKind.COMMAND:
            if (event.type, field.key) in TEMPLATE_COMMAND_FIELDS:
                yield from self._template(field.key, params[field.key])
        elif kind == FieldKind.FILE:
            yield from self._file(field, params[field.key])
        elif kind == FieldKind.LABEL_TARGET:
            yield from self._target(index, event, field)
        elif kind == FieldKind.VAR_NAME:
            yield from self._var_name(field, params[field.key])
        elif kind == FieldKind.KEY:
            yield from self._key(field.key, params[field.key], single=True)
        elif kind == FieldKind.KEY_COMBO:
            yield from self._key(field.key, params[field.key], single=False)

    def _numeric(self, field: FieldSpec, key: str, value: Any) -> Iterator[tuple[str | None, str, Severity]]:
        if value is None:
            return
        if isinstance(value, str):
            yield from self._expression(key, value)
            return
        if field.integer and not float(value).is_integer():
            yield key, "%s must be a whole number" % field.label, "error"
        if field.minimum is not None and value < field.minimum:
            yield key, "%s must be at least %s" % (field.label, _fmt(field.minimum)), "error"
        if field.maximum is not None and value > field.maximum:
            yield key, "%s must be at most %s" % (field.label, _fmt(field.maximum)), "error"

    def _coord_pair(self, field: FieldSpec, params: Mapping[str, Any]) -> Iterator[tuple[str | None, str, Severity]]:
        values = [params[k] for k in field.keys]
        kinds = {"none" if v is None else "expr" if isinstance(v, str) else "number" for v in values}
        if len(kinds) > 1:
            if "none" in kinds:
                yield field.keys[0], "%s: set both values or neither" % field.label, "error"
            else:
                yield field.keys[0], ("%s: both values must be numbers or both expressions"
                                      % field.label), "error"
        for key, value in zip(field.keys, values):
            yield from self._numeric(field, key, value)

    def _expression(self, key: str, expr: str) -> Iterator[tuple[str | None, str, Severity]]:
        try:
            names = expressions.names_in(expr)
        except ExpressionError as e:
            yield key, str(e), "error"
            return
        yield from self._unassigned(key, names)

    def _template(self, key: str, template: str) -> Iterator[tuple[str | None, str, Severity]]:
        try:
            names = expressions.template_names(template)
        except ExpressionError as e:
            yield key, str(e), "error"
            return
        yield from self._unassigned(key, names)

    def _unassigned(self, key: str, names: set[str]) -> Iterator[tuple[str | None, str, Severity]]:
        for name in sorted(names - self.known):
            yield key, "Variable %r is never assigned in this macro" % name, "warning"

    def _file(self, field: FieldSpec, value: str) -> Iterator[tuple[str | None, str, Severity]]:
        if not value:
            yield field.key, "%s: no file chosen" % field.label, "error"
            return
        path = os.path.expanduser(value)
        if not os.path.isabs(path) and self.base_dir is not None:
            path = os.path.join(self.base_dir, path)
        if not os.path.isfile(path):
            yield field.key, "%s: %s does not exist" % (field.label, value), "error"

    def _target(self, index: int, event: MacroEvent,
                field: FieldSpec) -> Iterator[tuple[str | None, str, Severity]]:
        if field.visible_when is not None and not field.visible_when(event.params):
            return
        value = event.params[field.key]
        if value is None or value == "":
            if field.none_label is None:
                yield field.key, "%s: choose a label" % field.label, "error"
            return
        lower = value.lower()
        if lower in _RESERVED_LOWER:
            target_index = -1 if lower == RESERVED_TARGETS[0].lower() else len(self.document.rows)
        elif lower in self.label_rows:
            target_index = self.label_rows[lower]
        else:
            yield field.key, "%s: label %r does not exist" % (field.label, value), "error"
            return
        if event.type == EventType.REPEAT and target_index > index:
            yield field.key, "%s: label %r is below this row" % (field.label, value), "error"

    def _var_name(self, field: FieldSpec, value: str) -> Iterator[tuple[str | None, str, Severity]]:
        if not value:
            if field.default:
                yield field.key, "%s: a variable name is required" % field.label, "error"
            return
        if not expressions.is_valid_variable_name(value):
            yield field.key, ("%s: %r is not a valid name (letters, digits and _, not starting "
                              "with a digit)" % (field.label, value)), "error"
        elif value in DYNAMIC_NAMES:
            yield field.key, "%s: %r is a read-only dynamic value" % (field.label, value), "error"

    def _key(self, key: str, value: str, single: bool) -> Iterator[tuple[str | None, str, Severity]]:
        try:
            names = parse_combo(value)
        except ValueError as e:
            yield key, str(e), "error"
            return
        if single and len(names) != 1:
            yield key, "%r is a combination; this field takes one key" % value, "error"
            return
        if self.key_known is not None:
            unknown = [name for name in names if not self.key_known(name)]
            if unknown:
                yield key, "Unknown key %s" % ", ".join(map(repr, unknown)), "error"


def _nonzero(value: Any) -> bool:
    """A non-zero number or any expression."""
    if isinstance(value, str):
        return value.strip() != ""
    return bool(value)


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _capitalise(text: str) -> str:
    return text[:1].upper() + text[1:]
