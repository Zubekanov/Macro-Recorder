"""The Details panel: the selected row's fields, generated from its ActionSpec.

Enabled and Delay are always at the top; below them is one widget per
FieldSpec of ``spec.layout_for(row)``.  Every change commits only
the edited field's keys, inside the document's edit session for
``(row id, field key)``, so a run of keystrokes is one undo step.  The
session ends on selection change, FocusOut of the panel, undo or redo
(the document does that), targeting completion and locking for a mode
change.

The helpers above :class:`DetailsPanel` are Tk-free and unit-tested.
"""

from __future__ import annotations

import os
import tkinter as tk
from dataclasses import replace
from tkinter import ttk
from typing import Any, Callable, Mapping

from macro_recorder.gui.fields import (
    AMBER_COLOUR, INVALID_COLOUR, FieldContext, FieldWidget, make_field, widget_inside,
)
from macro_recorder.gui.settings import Settings
from macro_recorder.gui.targeting import Capture
from macro_recorder.model.catalogue import FieldKind, FieldSpec, spec_for
from macro_recorder.model.document import MacroDocument, Row
from macro_recorder.model.events import EventType
from macro_recorder.model.protocols import Change, Mode, OverlayShape
from macro_recorder.model.validate import names_assigned

ENABLED_SPEC = FieldSpec(keys=("enabled",), label="Enabled", kind=FieldKind.BOOL, default=True)
DELAY_SPEC = FieldSpec(keys=("delay_ms",), label="Delay", kind=FieldKind.DURATION, default=0,
                       allow_expr=False, minimum=0, integer=True, help="Waited before the row plays")
ROW_SPECS = (ENABLED_SPEC, DELAY_SPEC)

TargetingRequest = Callable[[tuple[str, ...], Callable[[Capture], None]], None]
KeyCaptureRequest = Callable[[bool, Callable[[str], None]], None]


# ---------------------------------------------------------------------------
# Tk-free helpers
# ---------------------------------------------------------------------------

def row_values(row: Row) -> dict[str, Any]:
    """The row-level values the top of the panel edits."""
    return {"enabled": row.enabled, "delay_ms": row.delay_ms}


def targeting_steps(spec: FieldSpec) -> int:
    """How many captures fill ``spec``: one per coordinate pair in targeting_fills."""
    coords = [k for k in spec.targeting_fills if k != "colour"]
    return max(1, len(coords) // 2)


def targeting_keys(spec: FieldSpec, step: int) -> tuple[str, ...]:
    """The keys capture number ``step`` fills (the colour comes with the first)."""
    coords = [k for k in spec.targeting_fills if k != "colour"]
    keys = tuple(coords[2 * step:2 * step + 2])
    if step == 0 and "colour" in spec.targeting_fills:
        keys += ("colour",)
    return keys


def targeting_values(spec: FieldSpec, capture: Capture, step: int) -> dict[str, Any]:
    """Map one capture onto the keys of ``step``."""
    keys = targeting_keys(spec, step)
    values: dict[str, Any] = {keys[0]: int(capture.x), keys[1]: int(capture.y)}
    if "colour" in keys:
        values["colour"] = capture.colour.lower()
    return values


def field_visible(spec: FieldSpec, values: Mapping[str, Any]) -> bool:
    return spec.visible_when is None or bool(spec.visible_when(values))


def field_enabled(spec: FieldSpec, values: Mapping[str, Any]) -> bool:
    return spec.enabled_when is None or bool(spec.enabled_when(values))


def panel_title(row: Row, number: int) -> str:
    name = "Mouse Path" if row.is_path else spec_for(row).name
    return "Row %d - %s" % (number, name)


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class DetailsPanel(ttk.Frame):
    """Editor for one row of ``document``.

    ``request_targeting(keys, deliver)`` asks the shell for a screen capture
    and ``deliver(capture)`` with a :class:`~macro_recorder.gui.targeting.Capture`
    on the main thread.
    ``request_key_capture(single, deliver)``, when given, adds Capture
    buttons to the key fields; without it they are typed only.
    """

    def __init__(self, master: tk.Misc, document: MacroDocument, settings: Settings,
                 request_targeting: TargetingRequest,
                 on_shape: Callable[[OverlayShape | None], None],
                 mode_provider: Callable[[], Mode],
                 request_key_capture: KeyCaptureRequest | None = None) -> None:
        super().__init__(master, padding=6)
        self._document = document
        self._settings = settings
        self._request_targeting = request_targeting
        self._on_shape = on_shape
        self._mode = mode_provider
        self._row_id: int | None = None
        self._locked = False
        self._layout_key: tuple[str, tuple[FieldSpec, ...]] | None = None
        self._row_fields: list[FieldWidget] = []
        self._fields: list[tuple[FieldSpec, FieldWidget]] = []
        self._message_field: FieldWidget | None = None
        self._context = FieldContext(labels=document.labels, assigned_names=self._assigned,
                                     folder=self._folder,
                                     request_targeting=self._start_targeting,
                                     request_key_capture=request_key_capture)

        self.columnconfigure(0, weight=1)
        self._title = ttk.Label(self, anchor="w")
        self._title.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self._top = ttk.Frame(self)
        self._top.columnconfigure(0, weight=1)
        self._top.grid(row=1, column=0, sticky="ew")
        self._separator = ttk.Separator(self)
        self._body = ttk.Frame(self)
        self._body.columnconfigure(0, weight=1)
        self._body.grid(row=3, column=0, sticky="ew")
        self._message = ttk.Label(self, anchor="w", justify="left", wraplength=260)
        self._message.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self.bind("<Configure>", self._on_resize, add="+")

        document.subscribe(self._on_document_change)
        self.bind("<Destroy>", self._on_destroy, add="+")

    # ------------------------------------------------------------ public API

    def show(self, row_id: int | None) -> None:
        """Show ``row_id`` (None clears the panel)."""
        if row_id != self._row_id:
            self.revert_pending()
            self._document.end_edit()
        row = self._row(row_id)
        self._row_id = row.id if row is not None else None
        if row is None:
            self._clear()
            self._on_shape(None)
            return
        self._show_row(row)
        self._on_shape(spec_for(row).overlay(row))

    def set_locked(self, locked: bool) -> None:
        """Disable every widget (recording, playing, paused) or enable them again."""
        if locked:
            self.revert_pending()
            self._document.end_edit()
        self._locked = locked
        row = self._row(self._row_id)
        if row is not None:
            self._apply_conditions(spec_for(row).read_row(row))

    def revert_pending(self) -> None:
        """Drop red text from every field."""
        for widget in self._all_fields():
            widget.revert()

    def refresh(self) -> None:
        """Re-read the shown row after a document change."""
        row = self._row(self._row_id)
        if row is None:
            self._row_id = None
            self._clear()
            self._on_shape(None)
            return
        self._show_row(row)
        if not self._locked:
            self._on_shape(spec_for(row).overlay(row))

    # ------------------------------------------------------------- building

    def _row(self, row_id: int | None) -> Row | None:
        if row_id is None:
            return None
        try:
            return self._document.row(row_id)
        except KeyError:
            return None

    def _folder(self) -> str | None:
        """The macro's folder, from the document's current path."""
        path = self._document.path
        return os.path.dirname(os.path.abspath(path)) if path else None

    def _assigned(self) -> set[str]:
        return names_assigned(self._document)

    def _all_fields(self) -> list[FieldWidget]:
        return self._row_fields + [widget for _spec, widget in self._fields]

    def _clear(self) -> None:
        for widget in self._all_fields():
            widget.frame.destroy()
        self._row_fields = []
        self._fields = []
        self._layout_key = None
        self._message_field = None
        self._title.configure(text="")
        self._message.configure(text="")
        self._separator.grid_remove()

    def _show_row(self, row: Row) -> None:
        action = spec_for(row)
        layout = action.layout_for(row)
        values = action.read_row(row)
        self._context.action_type = row.type
        key = (row.type, layout)
        if key != self._layout_key:
            self._build(row, layout, values)
            self._layout_key = key
        else:
            for widget in self._row_fields:
                widget.set_values(row_values(row))
            for _spec, widget in self._fields:
                widget.set_values(values)
        self._title.configure(text=panel_title(row, self._document.index_of(row.id) + 1))
        self._apply_conditions(values)
        if self._message_field is not None:
            self._update_message(self._message_field)

    def _build(self, row: Row, layout: tuple[FieldSpec, ...], values: Mapping[str, Any]) -> None:
        self._clear()
        top_values = row_values(row)
        for spec in ROW_SPECS:
            widget = make_field(self._top, spec, top_values, self._commit_row_field,
                                self._context)
            self._add(widget, len(self._row_fields))
            self._row_fields.append(widget)
        if layout:
            self._separator.grid(row=2, column=0, sticky="ew", pady=6)
        for spec in layout:
            widget = make_field(self._body, spec, values, self._commit_field, self._context)
            self._add(widget, len(self._fields))
            self._fields.append((spec, widget))

    def _add(self, widget: FieldWidget, index: int) -> None:
        widget.frame.grid(row=index, column=0, sticky="ew")
        widget.on_evaluate = self._field_evaluated
        for child in widget.inputs:
            child.bind("<FocusIn>", lambda _e, w=widget: self._update_message(w), add="+")
            child.bind("<FocusOut>", self._on_focus_out, add="+")

    def _apply_conditions(self, values: Mapping[str, Any]) -> None:
        for widget in self._row_fields:
            widget.set_enabled(not self._locked)
        for spec, widget in self._fields:
            if field_visible(spec, values):
                widget.frame.grid()
            else:
                widget.frame.grid_remove()
            widget.set_enabled(not self._locked and field_enabled(spec, values))

    # -------------------------------------------------------------- messages

    def _field_evaluated(self, widget: FieldWidget, typed: bool) -> None:
        if typed or widget is self._message_field:
            self._update_message(widget)

    def _update_message(self, widget: FieldWidget) -> None:
        self._message_field = widget
        if widget.state == "invalid":
            text, colour = widget.message, INVALID_COLOUR
        elif widget.state == "amber":
            text, colour = widget.message, AMBER_COLOUR
        else:
            text, colour = self._help(widget.spec), ""
        self._message.configure(text=text, foreground=colour)

    def _help(self, spec: FieldSpec) -> str:
        text = spec.help
        if self._context.action_type == EventType.WINDOW_FOCUS.value and spec.key == "timeout_ms":
            text += " (%d ms)" % self._settings.playback.window_timeout_ms
        return text

    # --------------------------------------------------------------- commits

    def _can_edit(self) -> bool:
        return not self._locked and self._mode() == Mode.IDLE

    def _commit_field(self, key: str, values: dict) -> None:
        row = self._row(self._row_id)
        if row is None or not self._can_edit():
            return
        self._replace(spec_for(row).write_row(row, values), key)

    def _commit_row_field(self, key: str, values: dict) -> None:
        row = self._row(self._row_id)
        if row is None or not self._can_edit():
            return
        if key == "enabled":
            self._document.end_edit()
            self._document.replace_row(replace(row, **values))
            self._document.end_edit()
            return
        self._replace(replace(row, **values), key)

    def _replace(self, new: Row, key: str) -> None:
        self._document.begin_edit(new.id, key)
        self._document.replace_row(new)

    # ------------------------------------------------------------- targeting

    def _start_targeting(self, spec: FieldSpec) -> None:
        if self._row_id is None or not self._can_edit():
            return
        self.revert_pending()
        self._document.end_edit()
        self._target_step(spec, self._row_id, 0, {})

    def _target_step(self, spec: FieldSpec, row_id: int, step: int, collected: dict) -> None:
        def deliver(capture: Capture) -> None:
            row = self._row(row_id)
            if row is None or self._row_id != row_id or \
                    self._mode() not in (Mode.IDLE, Mode.TARGETING):
                return
            values = dict(collected)
            values.update(targeting_values(spec, capture, step))
            if step + 1 < targeting_steps(spec):
                # Start the next capture once the shell has finished with this one.
                self.after_idle(lambda: self._target_step(spec, row_id, step + 1, values))
                return
            if spec.kind == FieldKind.REGION:
                values[spec.keys[-1]] = None
            self._document.end_edit()
            self._replace(spec_for(row).write_row(row, values), spec.key)
            self._document.end_edit()

        self._request_targeting(targeting_keys(spec, step), deliver)

    # ---------------------------------------------------------------- events

    def _on_document_change(self, change: Change) -> None:
        if self._row_id is None:
            return
        if change.kind == "reset":
            self.revert_pending()
        self.refresh()

    def _on_resize(self, event: tk.Event) -> None:
        """Wrap the field message to the panel's width."""
        self._message.configure(wraplength=max(120, event.width - 16))

    def _on_focus_out(self, _event: tk.Event) -> None:
        self.after_idle(self._end_edit_if_focus_left)

    def _end_edit_if_focus_left(self) -> None:
        try:
            focused = self.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if not widget_inside(focused, self):
            self._document.end_edit()

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self:
            try:
                self._document.unsubscribe(self._on_document_change)
            except ValueError:
                pass
