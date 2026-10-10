"""Window focus action."""

from __future__ import annotations

from macro_recorder.model.actions._common import boolean, choice, duration, label_target, text
from macro_recorder.model.catalogue import (
    Abort, ActionSpec, FieldKind, FieldSpec, Flow, Jump, Preset, make, register,
)
from macro_recorder.model.document import Row
from macro_recorder.model.events import EventType
from macro_recorder.model.protocols import OverlayShape, PlayContext


def _describe(row: Row) -> str:
    e = row.event
    out = e["window"] or "(any window)"
    if e["match_mode"] == "regex":
        out += " (regex)"
    if e["launch"]:
        out += ", launch: %s" % e["launch"]
    return out


def _overlay(row: Row) -> OverlayShape | None:
    rect = row.event["rect"]
    if not rect or len(rect) != 4:
        return None
    l, t, w, h = rect
    return OverlayShape("rect", ((float(l), float(t)), (float(l + w), float(t + h))))


def _play(ctx: PlayContext, row: Row) -> Flow | Jump | Abort:
    """The engine owns window matching, activation, restore and the context."""
    return ctx.enter_window(row)


WINDOW_FOCUS = register(ActionSpec(
    type=EventType.WINDOW_FOCUS.value, name="Window Focus", category="Window",
    fields=(
        text("window", "Window title", ""),
        choice("match_mode", "Match", ("substring", "regex"), "substring"),
        FieldSpec(keys=("rect",), label="Recorded frame", kind=FieldKind.INFO, default=None),
        FieldSpec(keys=("client_rect",), label="Recorded client area", kind=FieldKind.INFO, default=None),
        boolean("maximised", "Maximised", False),
        FieldSpec(keys=("launch",), label="Launch if missing", kind=FieldKind.COMMAND, default=""),
        duration("timeout_ms", "Timeout", None, help="Blank uses the Playback tab setting"),
        choice("on_missing", "If missing", ("default", "skip", "halt", "goto"), "default"),
        label_target("target", "Go to", none_label=None,
                     visible_when=lambda v: v.get("on_missing") == "goto"),
        choice("restore_rect", "Restore position", ("default", "yes", "no"), "default"),
    ),
    presets=(Preset("Window focus", None, lambda: make("window_focus", window="")),),
    describe=_describe, overlay=_overlay, play=_play,
))
