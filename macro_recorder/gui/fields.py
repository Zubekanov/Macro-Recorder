"""Field widgets for the Details panel, one class per FieldKind.

The first half of this module is Tk-free: every kind's parsing and
validation is a plain function (``parse_number_field``, ``parse_pair``,
``parse_colour`` ...) that turns the text in the widgets into param values
or raises :class:`FieldError`.  The widget classes below only move text in
and out of their StringVars and call those functions.

A widget's StringVars are traced on write and the trace is suppressed while
the widget populates itself.  A change parses only that field; when it
parses and the values differ from the last known ones the widget calls
``on_change(spec.key, values)`` once.  Text that does not parse turns red
and stays uncommitted until :meth:`FieldWidget.revert`, which also runs
when focus leaves the field (not when it moves between the field's own
inputs).  An expression that
reads a variable nothing in the document assigns turns amber and is still
committed.
"""

from __future__ import annotations

import math
import os
import re
import shlex
import tkinter as tk
from dataclasses import dataclass
from tkinter import colorchooser, filedialog, ttk
from typing import Any, Callable, Mapping

from macro_recorder.engine import expressions
from macro_recorder.engine.expressions import ExpressionError
from macro_recorder.model.actions.variables import DYNAMIC_NAMES
from macro_recorder.model.actions.wait import format_colour
from macro_recorder.model.actions.wait import parse_colour as _parse_rgb
from macro_recorder.model.catalogue import END_TARGET, START_TARGET, FieldKind, FieldSpec
from macro_recorder.model.fileformat import MAX_MAGNITUDE
from macro_recorder.model.keys import combo_from_names, parse_combo
from macro_recorder.model.validate import EXPRESSION_TEXT_FIELDS, TEMPLATE_COMMAND_FIELDS
from macro_recorder.platform.x11 import keysym_from_name

# A plain decimal number; any other text is an expression.
_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")

REGION_RECTANGLE = "Rectangle"
REGION_MONITOR = "Monitor"
REGION_MODES = (REGION_RECTANGLE, REGION_MONITOR)
MAX_MONITOR = 16


# ===========================================================================
# Tk-free parsing and validation
# ===========================================================================

class FieldError(ValueError):
    """Text in a field that cannot be committed; the message says why."""


@dataclass(frozen=True)
class Parsed:
    """The values a field's text stands for.

    ``names`` are the variable names its expressions read (for the amber
    state); ``warning`` is any other reason to show the field amber.
    """
    values: dict[str, Any]
    names: frozenset[str] = frozenset()
    warning: str = ""


def format_number(value: Any) -> str:
    """Text for a number-or-expression: ints as is, floats with up to three
    decimals and no trailing zeros, expressions verbatim, None blank."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        text = ("%.3f" % value).rstrip("0").rstrip(".")
        return "0" if text in ("-0", "") else text
    return str(value)


def expression_names(text: str) -> frozenset[str]:
    """The names an expression reads; FieldError when it is not valid."""
    try:
        return frozenset(expressions.names_in(text))
    except ExpressionError as e:
        raise FieldError(str(e)) from None


def template_names(text: str) -> frozenset[str]:
    """The names a template's ``{expr}`` parts read; FieldError for bad braces."""
    try:
        return frozenset(expressions.template_names(text))
    except ExpressionError as e:
        raise FieldError(str(e)) from None


def _names_of(value: Any) -> frozenset[str]:
    return expression_names(value) if isinstance(value, str) else frozenset()


def unknown_names(names: frozenset[str], assigned: set[str] | frozenset[str]) -> list[str]:
    """Names neither assigned in the document nor dynamic, sorted."""
    return sorted(set(names) - set(assigned) - DYNAMIC_NAMES)


def parse_number(text: str, spec: FieldSpec, key: str | None = None) -> int | float | str | None:
    """One numeric key: a number, an expression (when allowed) or None.

    Blank is None only when the key's default is None.  Whole numbers come
    back as int.  ``integer``, ``minimum`` and ``maximum`` apply to numbers;
    expressions are only checked for syntax.
    """
    key = key or spec.key
    default = spec.defaults().get(key)
    stripped = text.strip()
    if not stripped:
        if default is None:
            return None
        raise FieldError("%s needs a value" % spec.label)
    if _NUMBER_RE.match(stripped):
        number = float(stripped)
        if not math.isfinite(number) or abs(number) > MAX_MAGNITUDE:
            raise FieldError("%s is out of range" % spec.label)
        if number.is_integer() and ("." not in stripped or spec.integer):
            value: int | float = int(number)
        elif spec.integer:
            raise FieldError("%s must be a whole number" % spec.label)
        else:
            value = number
        if spec.minimum is not None and value < spec.minimum:
            raise FieldError("%s must be at least %s" % (spec.label, format_number(spec.minimum)))
        if spec.maximum is not None and value > spec.maximum:
            raise FieldError("%s must be at most %s" % (spec.label, format_number(spec.maximum)))
        return value
    if not spec.expr_allowed:
        raise FieldError("%s must be a number" % spec.label)
    expression_names(stripped)
    return stripped


def parse_number_field(text: str, spec: FieldSpec) -> Parsed:
    """NUMBER and DURATION."""
    value = parse_number(text, spec)
    return Parsed({spec.key: value}, _names_of(value))


def parse_pair(x_text: str, y_text: str, spec: FieldSpec) -> Parsed:
    """COORD_PAIR: both numbers, both expressions, or both blank when the default allows."""
    x_key, y_key = spec.keys
    x = parse_number(x_text, spec, x_key)
    y = parse_number(y_text, spec, y_key)
    kinds = {"none" if v is None else "expr" if isinstance(v, str) else "number" for v in (x, y)}
    if len(kinds) > 1:
        if "none" in kinds:
            raise FieldError("%s: set both values or neither" % spec.label)
        raise FieldError("%s: both values must be numbers or both expressions" % spec.label)
    return Parsed({x_key: x, y_key: y}, _names_of(x) | _names_of(y))


def region_mode(values: Mapping[str, Any], spec: FieldSpec) -> str:
    """Which REGION mode the five values show: Monitor when a monitor is set."""
    return REGION_MONITOR if values.get(spec.keys[-1]) is not None else REGION_RECTANGLE


def parse_region(mode: str, corner_texts: tuple[str, str, str, str], monitor_text: str,
                 spec: FieldSpec, current: Mapping[str, Any]) -> Parsed:
    """REGION: corners from the entries, or a monitor number.

    In Monitor mode the corners keep their current values.
    """
    *corner_keys, monitor_key = spec.keys
    if mode == REGION_MONITOR:
        text = monitor_text.strip()
        if not text.isdigit() or int(text) < 1:
            raise FieldError("Monitor numbers start at 1")
        values = {k: current.get(k) for k in corner_keys}
        values[monitor_key] = int(text)
        return Parsed(values)
    if mode != REGION_RECTANGLE:
        raise FieldError("Unknown region mode %r" % mode)
    values = {}
    names: frozenset[str] = frozenset()
    for key, text in zip(corner_keys, corner_texts):
        value = parse_number(text, spec, key)
        if value is None:
            raise FieldError("%s needs all four corner values" % spec.label)
        values[key] = value
        names |= _names_of(value)
    values[monitor_key] = None
    return Parsed(values, names)


def parse_colour(text: str) -> str:
    """``#rrggbb`` in lower case; FieldError otherwise."""
    try:
        return format_colour(_parse_rgb(text))
    except ValueError:
        raise FieldError("Colour must be #rrggbb") from None


def parse_colour_field(text: str, spec: FieldSpec) -> Parsed:
    return Parsed({spec.key: parse_colour(text)})


def parse_text_field(text: str, spec: FieldSpec, action_type: str) -> Parsed:
    """TEXT: verbatim, or a checked expression for the fields that hold one."""
    if (action_type, spec.key) in EXPRESSION_TEXT_FIELDS:
        if not text.strip():
            raise FieldError("%s needs an expression" % spec.label)
        return Parsed({spec.key: text}, expression_names(text))
    return Parsed({spec.key: text})


def parse_template_field(text: str, spec: FieldSpec) -> Parsed:
    return Parsed({spec.key: text}, template_names(text))


def parse_command_field(text: str, spec: FieldSpec, action_type: str) -> Parsed:
    """COMMAND: a template where the action renders one, else verbatim."""
    if (action_type, spec.key) in TEMPLATE_COMMAND_FIELDS:
        return Parsed({spec.key: text}, template_names(text))
    return Parsed({spec.key: text})


def choice_text(value: str) -> str:
    """Display text for a CHOICE value: ``ok_cancel`` -> ``ok cancel``."""
    return value.replace("_", " ")


def parse_choice(text: str, spec: FieldSpec) -> Parsed:
    for choice in spec.choices:
        if text in (choice, choice_text(choice)):
            return Parsed({spec.key: choice})
    raise FieldError("%s must be one of %s" % (spec.label, ", ".join(map(choice_text, spec.choices))))


def parse_bool(text: str, spec: FieldSpec) -> Parsed:
    return Parsed({spec.key: text == "1"})


def label_target_choices(labels: list[str], none_label: str | None, current: str | None) -> list[str]:
    """The LABEL_TARGET list: none_label (when set), Start, labels in row order,
    End, then the current value when no entry matches it."""
    out = ([none_label] if none_label is not None else []) + [START_TARGET] + list(labels) + [END_TARGET]
    if current and current.lower() not in {c.lower() for c in out}:
        out.append(current)
    return out


def label_target_text(value: str | None, none_label: str | None) -> str:
    if value is None or value == "":
        return none_label or ""
    return value


def parse_label_target(text: str, spec: FieldSpec, labels: list[str]) -> Parsed:
    """The chosen target; FieldError for no choice or a label that does not exist."""
    if spec.none_label is not None and text == spec.none_label:
        return Parsed({spec.key: None})
    if not text:
        raise FieldError("%s: choose a label" % spec.label)
    known = {t.lower() for t in (START_TARGET, END_TARGET, *labels)}
    if text.lower() not in known:
        raise FieldError("%s: label %r does not exist" % (spec.label, text))
    return Parsed({spec.key: text})


def parse_var_name(text: str, spec: FieldSpec) -> Parsed:
    """VAR_NAME: blank only when the default is blank; identifiers; no dynamic values."""
    name = text.strip()
    if not name:
        if spec.default:
            raise FieldError("%s: a variable name is required" % spec.label)
        return Parsed({spec.key: ""})
    if not expressions.is_valid_variable_name(name):
        raise FieldError("%s: letters, digits and _, not starting with a digit" % spec.label)
    if name in DYNAMIC_NAMES:
        raise FieldError("%s: %r is a read-only dynamic value" % (spec.label, name))
    return Parsed({spec.key: name})


def parse_key_field(text: str, spec: FieldSpec) -> Parsed:
    """KEY (one keysym name) or KEY_COMBO (stored as ``ctrl+shift+s``).

    Every name must be a known X keysym, so a typo is red here rather than
    an error at playback.
    """
    try:
        names = parse_combo(text)
    except ValueError as e:
        raise FieldError(str(e)) from None
    unknown = [name for name in names if not keysym_from_name(name)]
    if unknown:
        raise FieldError("%s: unknown key %s" % (spec.label, ", ".join(map(repr, unknown))))
    if spec.kind == FieldKind.KEY:
        if len(names) != 1:
            raise FieldError("%s takes one key, not a combination" % spec.label)
        return Parsed({spec.key: names[0]})
    return Parsed({spec.key: combo_from_names(names)})


def resolve_file(value: str, document_dir: str | None) -> str:
    """Where a FILE value points: relative paths are read from the document's folder."""
    path = os.path.expanduser(value)
    if not os.path.isabs(path) and document_dir:
        path = os.path.join(document_dir, path)
    return path


def parse_file_field(text: str, spec: FieldSpec, document_dir: str | None) -> Parsed:
    """FILE: kept as typed; amber when blank or the file does not exist."""
    if not text:
        return Parsed({spec.key: text}, warning="%s: no file chosen" % spec.label)
    if not os.path.isfile(resolve_file(text, document_dir)):
        return Parsed({spec.key: text}, warning="%s: %s does not exist" % (spec.label, text))
    return Parsed({spec.key: text})


def relative_to(path: str, document_dir: str | None) -> str:
    """A browsed path, relative when it lies inside the document's folder."""
    if document_dir:
        base = os.path.abspath(document_dir)
        full = os.path.abspath(path)
        if os.path.commonpath([base, full]) == base:
            return os.path.relpath(full, base)
    return path


def insert_quoted(text: str, index: int, path: str) -> tuple[str, int]:
    """Insert ``shlex.quote(path)`` at ``index``; returns the text and the new cursor."""
    index = max(0, min(index, len(text)))
    quoted = shlex.quote(path)
    return text[:index] + quoted + text[index:], index + len(quoted)


def format_info(value: Any) -> str:
    """INFO text: a recorded rect as ``left, top, width x height``."""
    if value is None or value == "":
        return "none"
    if isinstance(value, (tuple, list)) and len(value) == 4 and \
            all(isinstance(v, (int, float)) for v in value):
        left, top, width, height = (format_number(v) for v in value)
        return "%s, %s, %s x %s" % (left, top, width, height)
    return str(value)


def path_inside(path: str, container: str) -> bool:
    """True when Tk path ``path`` is ``container`` or one of its descendants."""
    return path == container or path.startswith(container.rstrip(".") + ".")


def widget_inside(widget: Any, container: Any) -> bool:
    """True when ``widget`` (None for no focus) is ``container`` or inside it."""
    return widget is not None and path_inside(str(widget), str(container))


def field_label(spec: FieldSpec) -> str:
    """The label shown beside a field; durations name their unit."""
    if spec.kind == FieldKind.DURATION:
        return "%s (ms)" % spec.label
    return spec.label


# ===========================================================================
# Widgets
# ===========================================================================

LABEL_WIDTH = 24
INVALID_COLOUR = "#c00000"
AMBER_COLOUR = "#b06000"
_STATE_PREFIX = {"invalid": "Invalid", "amber": "Amber"}
_STYLED_CLASSES = ("TEntry", "TCombobox", "TSpinbox")


@dataclass
class FieldContext:
    """What a field may ask of the panel.

    ``request_targeting(spec)`` starts a screen capture whose result the panel
    writes into ``spec.targeting_fills``.  ``request_key_capture(single,
    deliver)`` captures a key (``single``) or a combination and calls
    ``deliver(text)``; None when the shell offers no capture.
    """
    labels: Callable[[], list[str]]
    assigned_names: Callable[[], set[str]]
    document_dir: str | None = None
    request_targeting: Callable[[FieldSpec], None] | None = None
    request_key_capture: Callable[[bool, Callable[[str], None]], None] | None = None
    action_type: str = ""


def _ensure_styles(widget: tk.Misc) -> None:
    style = ttk.Style(widget)
    for cls in _STYLED_CLASSES:
        style.configure("Invalid.%s" % cls, foreground=INVALID_COLOUR)
        style.configure("Amber.%s" % cls, foreground=AMBER_COLOUR)


class FieldWidget:
    """Base class: a label, the inputs, their StringVars and the commit logic.

    Subclasses implement :meth:`build`, :meth:`texts` and :meth:`parse`.
    ``on_evaluate(widget, typed)``, when set, runs after every parse so the
    panel can show the field's message; ``typed`` is True for user edits.
    """

    def __init__(self, master: tk.Misc, spec: FieldSpec, values: Mapping[str, Any],
                 on_change: Callable[[str, dict], None], context: FieldContext) -> None:
        self.spec = spec
        self.context = context
        self._on_change = on_change
        self._vars: list[tk.StringVar] = []
        self.inputs: list[tk.Widget] = []
        self._populating = False
        self._pending = False
        self._enabled = True
        self._values: dict[str, Any] = {k: values.get(k) for k in spec.keys}
        self.state = "ok"
        self.message = ""
        self.on_evaluate: Callable[[FieldWidget, bool], None] | None = None
        _ensure_styles(master)
        self.frame = ttk.Frame(master)
        self.frame.columnconfigure(1, weight=1)
        ttk.Label(self.frame, text=self.label_text(), width=LABEL_WIDTH, anchor="w").grid(
            row=0, column=0, sticky="nw", padx=(0, 6), pady=2)
        body = ttk.Frame(self.frame)
        body.grid(row=0, column=1, sticky="ew", pady=2)
        self.build(body)
        for widget in self.inputs:
            widget.bind("<FocusOut>", self._on_focus_out, add="+")
        self._populate(self._values)

    # --------------------------------------------------------- subclass API

    def label_text(self) -> str:
        return field_label(self.spec)

    def build(self, body: ttk.Frame) -> None:
        raise NotImplementedError

    def texts(self, values: Mapping[str, Any]) -> list[str]:
        """Text for each StringVar, in creation order."""
        raise NotImplementedError

    def parse(self) -> Parsed:
        """The values the current text stands for; FieldError when it cannot be committed."""
        raise NotImplementedError

    def after_evaluate(self, parsed: Parsed | None) -> None:
        """Hook run after every parse (swatches, mode-dependent inputs)."""

    def update_choices(self) -> None:
        """Hook run before new values are shown (lists that depend on the document)."""

    # ------------------------------------------------------------- helpers

    def new_var(self) -> tk.StringVar:
        var = tk.StringVar(self.frame)
        var.trace_add("write", self._on_write)
        self._vars.append(var)
        return var

    def text(self, index: int = 0) -> str:
        return self._vars[index].get()

    # --------------------------------------------------------- public API

    @property
    def values(self) -> dict[str, Any]:
        """The last committed or shown values."""
        return dict(self._values)

    @property
    def pending(self) -> bool:
        """True while the field holds red text that was not committed."""
        return self._pending

    def set_values(self, values: Mapping[str, Any]) -> None:
        """Show new values; text that already stands for them is left alone."""
        new = {k: values.get(k) for k in self.spec.keys}
        self.update_choices()
        self._values = new
        try:
            same = self.parse().values == new
        except FieldError:
            same = False
        if same:
            self._evaluate(commit=False)
        else:
            self._populate(new)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        for widget in self.inputs:
            if isinstance(widget, ttk.Combobox):
                widget.state(["!disabled", "readonly"] if enabled else ["disabled"])
            else:
                widget.state(["!disabled"] if enabled else ["disabled"])

    def set_state(self, state: str) -> None:
        """'ok', 'invalid' (red) or 'amber'."""
        self.state = state
        prefix = _STATE_PREFIX.get(state)
        for widget in self.inputs:
            cls = widget.winfo_class()
            if cls in _STYLED_CLASSES:
                widget.configure(style="%s.%s" % (prefix, cls) if prefix else cls)

    def revert(self) -> None:
        """Drop red text, showing the last known values again."""
        if self._pending:
            self._populate(self._values)

    # ------------------------------------------------------------ internals

    def _on_focus_out(self, _event: tk.Event) -> None:
        """Revert once focus has settled outside the field.

        Moving between the inputs of one field (x to y of a pair) keeps the
        text, so a pair can pass through a red state while it is retyped.
        """
        self.frame.after_idle(self._revert_if_focus_left)

    def _revert_if_focus_left(self) -> None:
        try:
            if not self.frame.winfo_exists():
                return
            focused = self.frame.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if not widget_inside(focused, self.frame):
            self.revert()

    def _populate(self, values: Mapping[str, Any]) -> None:
        self._populating = True
        try:
            for var, text in zip(self._vars, self.texts(values)):
                if var.get() != text:
                    var.set(text)
        finally:
            self._populating = False
        self._evaluate(commit=False)

    def _on_write(self, *_args: Any) -> None:
        if not self._populating:
            self._evaluate(commit=True)

    def _evaluate(self, commit: bool) -> None:
        try:
            parsed = self.parse()
        except FieldError as e:
            self._pending = commit or self._pending
            self.message = str(e)
            self.set_state("invalid")
            self.after_evaluate(None)
            self._notify(commit)
            return
        self._pending = False
        unknown = unknown_names(parsed.names, self.context.assigned_names())
        if unknown:
            self.message = "Never assigned in this macro: %s" % ", ".join(unknown)
            self.set_state("amber")
        elif parsed.warning:
            self.message = parsed.warning
            self.set_state("amber")
        else:
            self.message = ""
            self.set_state("ok")
        self.after_evaluate(parsed)
        self._notify(commit)
        if commit and parsed.values != self._values:
            self._values = dict(parsed.values)
            self._on_change(self.spec.key, dict(parsed.values))

    def _notify(self, typed: bool) -> None:
        if self.on_evaluate is not None:
            self.on_evaluate(self, typed)


class _EntryField(FieldWidget):
    """One Entry holding one key."""

    width = 24

    def build(self, body: ttk.Frame) -> None:
        self.entry = ttk.Entry(body, textvariable=self.new_var(), width=self.width)
        self.entry.grid(row=0, column=0, sticky="ew")
        body.columnconfigure(0, weight=1)
        self.inputs.append(self.entry)

    def texts(self, values: Mapping[str, Any]) -> list[str]:
        value = values.get(self.spec.key)
        return ["" if value is None else str(value)]


class NumberField(_EntryField):
    """NUMBER and DURATION: a number, or an expression when allowed."""

    width = 12

    def texts(self, values: Mapping[str, Any]) -> list[str]:
        return [format_number(values.get(self.spec.key))]

    def parse(self) -> Parsed:
        return parse_number_field(self.text(), self.spec)


class CoordPairField(FieldWidget):
    """Two entries, validated together, plus Pick when targeting can fill them."""

    def build(self, body: ttk.Frame) -> None:
        for column in range(2):
            entry = ttk.Entry(body, textvariable=self.new_var(), width=8)
            entry.grid(row=0, column=column, sticky="ew", padx=(0, 4))
            body.columnconfigure(column, weight=1)
            self.inputs.append(entry)
        if self.spec.targeting_fills and self.context.request_targeting is not None:
            button = ttk.Button(body, text="Pick", width=6,
                                command=lambda: self.context.request_targeting(self.spec))
            button.grid(row=0, column=2)
            self.inputs.append(button)

    def texts(self, values: Mapping[str, Any]) -> list[str]:
        return [format_number(values.get(k)) for k in self.spec.keys]

    def parse(self) -> Parsed:
        return parse_pair(self.text(0), self.text(1), self.spec)


class RegionField(FieldWidget):
    """A mode selector driving corner entries or a monitor spinbox."""

    def build(self, body: ttk.Frame) -> None:
        body.columnconfigure(0, weight=1)
        self._mode = ttk.Combobox(body, textvariable=self.new_var(), values=REGION_MODES,
                                  state="readonly", width=14)
        self._mode.grid(row=0, column=0, sticky="w")
        self.inputs.append(self._mode)
        self._corners = ttk.Frame(body)
        for index in range(4):
            entry = ttk.Entry(self._corners, textvariable=self.new_var(), width=7)
            entry.grid(row=index // 2, column=index % 2, sticky="ew", padx=(0, 4), pady=(2, 0))
            self._corners.columnconfigure(index % 2, weight=1)
            self.inputs.append(entry)
        if self.spec.targeting_fills and self.context.request_targeting is not None:
            button = ttk.Button(self._corners, text="Pick", width=6,
                                command=lambda: self.context.request_targeting(self.spec))
            button.grid(row=0, column=2, rowspan=2)
            self.inputs.append(button)
        self._monitor = ttk.Spinbox(body, textvariable=self.new_var(), from_=1, to=MAX_MONITOR,
                                    width=5)
        self.inputs.append(self._monitor)

    def texts(self, values: Mapping[str, Any]) -> list[str]:
        *corner_keys, monitor_key = self.spec.keys
        monitor = values.get(monitor_key)
        return ([region_mode(values, self.spec)]
                + [format_number(values.get(k)) for k in corner_keys]
                + [str(monitor if monitor is not None else 1)])

    def set_values(self, values: Mapping[str, Any]) -> None:
        """Corners and monitor are hidden in some modes, so their text always follows the values."""
        if self.text(0) == REGION_RECTANGLE:
            super().set_values(values)
        else:
            self._values = {k: values.get(k) for k in self.spec.keys}
            self._populate(self._values)

    def parse(self) -> Parsed:
        corners = (self.text(1), self.text(2), self.text(3), self.text(4))
        return parse_region(self.text(0), corners, self.text(5), self.spec, self._values)

    def after_evaluate(self, parsed: Parsed | None) -> None:
        mode = self.text(0)
        if mode == REGION_RECTANGLE:
            self._corners.grid(row=1, column=0, sticky="ew")
        else:
            self._corners.grid_remove()
        if mode == REGION_MONITOR:
            self._monitor.grid(row=1, column=0, sticky="w", pady=(2, 0))
        else:
            self._monitor.grid_remove()


class TextField(_EntryField):
    """TEXT: verbatim, or an expression for var_set and if_else."""

    def parse(self) -> Parsed:
        return parse_text_field(self.text(), self.spec, self.context.action_type)


class TemplateField(_EntryField):
    """TEMPLATE: text with ``{expr}`` parts; braces are checked."""

    def parse(self) -> Parsed:
        return parse_template_field(self.text(), self.spec)


class ChoiceField(FieldWidget):
    """CHOICE: a readonly Combobox."""

    def build(self, body: ttk.Frame) -> None:
        self.combo = ttk.Combobox(body, textvariable=self.new_var(), state="readonly",
                                  values=[choice_text(c) for c in self.spec.choices], width=14)
        self.combo.grid(row=0, column=0, sticky="w")
        self.inputs.append(self.combo)

    def texts(self, values: Mapping[str, Any]) -> list[str]:
        value = values.get(self.spec.key)
        return [choice_text(value) if isinstance(value, str) else ""]

    def parse(self) -> Parsed:
        return parse_choice(self.text(), self.spec)


class FileField(_EntryField):
    """FILE: an Entry plus Browse; paths inside the macro's folder are stored relative."""

    def build(self, body: ttk.Frame) -> None:
        super().build(body)
        button = ttk.Button(body, text="Browse", command=self._browse)
        button.grid(row=0, column=1, padx=(4, 0))
        self.inputs.append(button)

    def _browse(self) -> None:
        options: dict[str, Any] = {"parent": self.frame}
        if self.spec.file_types:
            options["filetypes"] = list(self.spec.file_types)
        if self.context.document_dir:
            options["initialdir"] = self.context.document_dir
        path = filedialog.askopenfilename(**options)
        if path:
            self._vars[0].set(relative_to(path, self.context.document_dir))

    def parse(self) -> Parsed:
        return parse_file_field(self.text(), self.spec, self.context.document_dir)


class CommandField(_EntryField):
    """COMMAND: an Entry plus Browse, which inserts a shell-quoted path at the cursor."""

    def build(self, body: ttk.Frame) -> None:
        super().build(body)
        button = ttk.Button(body, text="Browse", command=self._browse)
        button.grid(row=0, column=1, padx=(4, 0))
        self.inputs.append(button)

    def _browse(self) -> None:
        options: dict[str, Any] = {"parent": self.frame}
        if self.context.document_dir:
            options["initialdir"] = self.context.document_dir
        path = filedialog.askopenfilename(**options)
        if not path:
            return
        text, cursor = insert_quoted(self.text(), self.entry.index(tk.INSERT), path)
        self._vars[0].set(text)
        self.entry.icursor(cursor)

    def parse(self) -> Parsed:
        return parse_command_field(self.text(), self.spec, self.context.action_type)


class BoolField(FieldWidget):
    """BOOL: a Checkbutton on a StringVar holding "1" or "0"."""

    def label_text(self) -> str:
        return ""

    def build(self, body: ttk.Frame) -> None:
        check = ttk.Checkbutton(body, text=self.spec.label, variable=self.new_var(),
                                onvalue="1", offvalue="0")
        check.grid(row=0, column=0, sticky="w")
        self.inputs.append(check)

    def texts(self, values: Mapping[str, Any]) -> list[str]:
        return ["1" if values.get(self.spec.key) else "0"]

    def parse(self) -> Parsed:
        return parse_bool(self.text(), self.spec)


class ColourField(_EntryField):
    """COLOUR: ``#rrggbb`` with a swatch and a colour chooser."""

    width = 9

    def build(self, body: ttk.Frame) -> None:
        self.entry = ttk.Entry(body, textvariable=self.new_var(), width=self.width)
        self.entry.grid(row=0, column=0, sticky="w")
        self.inputs.append(self.entry)
        self._swatch = tk.Label(body, width=3, relief="sunken", borderwidth=1)
        self._swatch.grid(row=0, column=1, padx=4, sticky="ns")
        button = ttk.Button(body, text="Pick", width=6, command=self._choose)
        button.grid(row=0, column=2)
        self.inputs.append(button)

    def _choose(self) -> None:
        try:
            initial = parse_colour(self.text())
        except FieldError:
            initial = self.spec.default
        _rgb, chosen = colorchooser.askcolor(color=initial, parent=self.frame)
        if chosen:
            self._vars[0].set(chosen.lower())

    def parse(self) -> Parsed:
        return parse_colour_field(self.text(), self.spec)

    def after_evaluate(self, parsed: Parsed | None) -> None:
        if parsed is not None:
            self._swatch.configure(background=parsed.values[self.spec.key])


class LabelTargetField(FieldWidget):
    """LABEL_TARGET: a readonly Combobox of the jump targets; a missing label shows red."""

    def build(self, body: ttk.Frame) -> None:
        self.combo = ttk.Combobox(body, textvariable=self.new_var(), state="readonly", width=18,
                                  postcommand=self.update_choices)
        self.combo.grid(row=0, column=0, sticky="w")
        self.inputs.append(self.combo)
        self.update_choices()

    def update_choices(self) -> None:
        current = self._values.get(self.spec.key)
        self.combo.configure(values=label_target_choices(
            self.context.labels(), self.spec.none_label, current))

    def texts(self, values: Mapping[str, Any]) -> list[str]:
        return [label_target_text(values.get(self.spec.key), self.spec.none_label)]

    def parse(self) -> Parsed:
        return parse_label_target(self.text(), self.spec, self.context.labels())


class VarNameField(_EntryField):
    """VAR_NAME: an identifier that is not a dynamic value."""

    width = 16

    def parse(self) -> Parsed:
        return parse_var_name(self.text(), self.spec)


class KeyField(_EntryField):
    """KEY and KEY_COMBO: typed names, plus Capture when the shell offers key capture."""

    width = 18

    def build(self, body: ttk.Frame) -> None:
        super().build(body)
        if self.context.request_key_capture is not None:
            button = ttk.Button(body, text="Capture", command=self._capture)
            button.grid(row=0, column=1, padx=(4, 0))
            self.inputs.append(button)

    def _capture(self) -> None:
        single = self.spec.kind == FieldKind.KEY
        self.context.request_key_capture(single, self._vars[0].set)

    def parse(self) -> Parsed:
        return parse_key_field(self.text(), self.spec)


class InfoField(FieldWidget):
    """INFO: read-only text."""

    def build(self, body: ttk.Frame) -> None:
        ttk.Label(body, textvariable=self.new_var(), anchor="w").grid(row=0, column=0, sticky="w")

    def texts(self, values: Mapping[str, Any]) -> list[str]:
        return [format_info(values.get(self.spec.key))]

    def set_values(self, values: Mapping[str, Any]) -> None:
        self._values = {k: values.get(k) for k in self.spec.keys}
        self._populate(self._values)

    def parse(self) -> Parsed:
        return Parsed(dict(self._values))


FIELD_CLASSES: dict[FieldKind, type[FieldWidget]] = {
    FieldKind.NUMBER: NumberField,
    FieldKind.DURATION: NumberField,
    FieldKind.COORD_PAIR: CoordPairField,
    FieldKind.REGION: RegionField,
    FieldKind.TEXT: TextField,
    FieldKind.TEMPLATE: TemplateField,
    FieldKind.CHOICE: ChoiceField,
    FieldKind.FILE: FileField,
    FieldKind.COMMAND: CommandField,
    FieldKind.BOOL: BoolField,
    FieldKind.COLOUR: ColourField,
    FieldKind.LABEL_TARGET: LabelTargetField,
    FieldKind.VAR_NAME: VarNameField,
    FieldKind.KEY: KeyField,
    FieldKind.KEY_COMBO: KeyField,
    FieldKind.INFO: InfoField,
}


def make_field(master: tk.Misc, spec: FieldSpec, values: Mapping[str, Any],
               on_change: Callable[[str, dict], None], context: FieldContext) -> FieldWidget:
    """The widget for ``spec``'s kind, showing ``values``."""
    return FIELD_CLASSES[spec.kind](master, spec, values, on_change, context)
