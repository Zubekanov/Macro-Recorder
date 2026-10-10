"""Mouse actions: move, path, timed move, click, scroll.

Coordinates go through ``ctx.coord`` so the window frame applies; timings go
through ``ctx.sleep`` so they scale with the playback speed.  Presses go
through the context's hold bookkeeping so a stop or a pause never leaves a
button down.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Mapping

from macro_recorder.model.actions._common import (
    choice, coord_pair, duration, fmt_ms, fmt_num, fmt_point, number,
)
from macro_recorder.model.catalogue import ActionSpec, FieldKind, FieldSpec, Flow, Preset, make, register
from macro_recorder.model.document import Row
from macro_recorder.model.events import EventType
from macro_recorder.model.protocols import OverlayShape, PlayContext

# A click moves the pointer first only when it is further away than this.
CLICK_MOVE_THRESHOLD_PX = 2.0


# ---------------------------------------------------------------- mouse_move

def _path_layout(row: Row) -> tuple[FieldSpec, ...]:
    if not row.is_path:
        return MOUSE_MOVE.fields
    return (
        coord_pair("x", "y", "Start", allow_expr=False, help="Editing moves every point"),
        FieldSpec(keys=("end",), label="End", kind=FieldKind.INFO),
        FieldSpec(keys=("points",), label="Points", kind=FieldKind.INFO),
        duration("duration_ms", "Duration", 0, allow_expr=False, help="Editing rescales the path"),
    )


def _path_read(row: Row) -> dict[str, Any]:
    first, last = row.events[0], row.events[-1]
    values = dict(first.params)
    if row.is_path:
        values["end"] = fmt_point(last["x"], last["y"])
        values["points"] = str(len(row.events))
        values["duration_ms"] = int(round(row.span_s * 1000))
    return values


def _path_write(row: Row, values: Mapping[str, Any]) -> Row:
    if not row.is_path:
        return replace(row, events=(row.events[0].with_params(x=values["x"], y=values["y"]),))
    first = row.events[0]
    dx = float(values.get("x", first["x"])) - float(first["x"])
    dy = float(values.get("y", first["y"])) - float(first["y"])
    events = list(row.events)
    if dx or dy:
        events = [e.with_params(x=type(e["x"])(e["x"] + dx), y=type(e["y"])(e["y"] + dy)) for e in events]
    new_ms = values.get("duration_ms")
    old_ms = row.span_s * 1000
    if new_ms is not None and old_ms > 0 and float(new_ms) != old_ms:
        scale = float(new_ms) / old_ms
        events = [e.with_dt(e.dt * scale) for e in events]
    return replace(row, events=tuple(events))


def _describe_move(row: Row) -> str:
    first, last = row.events[0], row.events[-1]
    if row.is_path:
        return "%s -> %s, %d points over %s" % (
            fmt_point(first["x"], first["y"]), fmt_point(last["x"], last["y"]),
            len(row.events), fmt_ms(int(round(row.span_s * 1000))))
    return fmt_point(first["x"], first["y"])


def _overlay_move(row: Row) -> OverlayShape | None:
    pts = []
    for e in row.events:
        if not isinstance(e["x"], (int, float)) or not isinstance(e["y"], (int, float)):
            return None
        pts.append((float(e["x"]), float(e["y"])))
    return OverlayShape("path" if len(pts) > 1 else "point", tuple(pts))


def _play_move(ctx: PlayContext, row: Row) -> Flow:
    """Set the pointer, or replay a path with its timing (frame applied to every point)."""
    if not row.is_path:
        ctx.input.move(*ctx.coord(row.event["x"], row.event["y"]))
        return Flow.NEXT
    points = [ctx.coord(e["x"], e["y"]) for e in row.events]
    dts = [e.dt for e in row.events]
    stopped = ctx.move_pointer(points, row.span_s * 1000.0, dts)
    return Flow.STOP if stopped else Flow.NEXT


MOUSE_MOVE = register(ActionSpec(
    type=EventType.MOUSE_MOVE.value, name="Mouse Move", category="Mouse", filter_group="mouse_move",
    fields=(coord_pair("x", "y", "Position"),),
    presets=(Preset("Move", "M", lambda: make("mouse_move", x=100, y=100)),),
    layout=_path_layout, read=_path_read, write=_path_write,
    describe=_describe_move, overlay=_overlay_move, play=_play_move, multi_event=True,
))


# ---------------------------------------------------------- mouse_move_timed

def _describe_timed(row: Row) -> str:
    e = row.event
    return "%s -> %s over %s" % (fmt_point(e["x"], e["y"]), fmt_point(e["x2"], e["y2"]), fmt_ms(e["duration_ms"]))


def _overlay_timed(row: Row) -> OverlayShape | None:
    e = row.event
    vals = (e["x"], e["y"], e["x2"], e["y2"])
    if not all(isinstance(v, (int, float)) for v in vals):
        return None
    return OverlayShape("line", ((float(vals[0]), float(vals[1])), (float(vals[2]), float(vals[3]))))


def _play_timed(ctx: PlayContext, row: Row) -> Flow:
    """Jump to the first point, then move to the second along the path mode."""
    e = row.event
    start = ctx.coord(e["x"], e["y"])
    end = ctx.coord(e["x2"], e["y2"])
    duration_ms = max(0.0, ctx.resolve(e["duration_ms"]))
    ctx.input.move(*start)
    stopped = ctx.move_pointer([start, end], duration_ms)
    return Flow.STOP if stopped else Flow.NEXT


MOUSE_MOVE_TIMED = register(ActionSpec(
    type=EventType.MOUSE_MOVE_TIMED.value, name="Mouse Move Point-to-Point", category="Mouse",
    filter_group="mouse_move",
    fields=(coord_pair("x", "y", "From"), coord_pair("x2", "y2", "To", default=(200, 200)),
            duration("duration_ms", "Duration", 500)),
    presets=(Preset("Move point-to-point", None,
                    lambda: make("mouse_move_timed", x=100, y=100, x2=200, y2=200, duration_ms=500)),),
    describe=_describe_timed, overlay=_overlay_timed, play=_play_timed,
))


# --------------------------------------------------------------- mouse_click

BUTTONS = ("left", "right", "middle", "x1", "x2")


def _describe_click(row: Row) -> str:
    e = row.event
    action = e["action"]
    if action == "click":
        count = e["count"]
        verb = {1: "click", 2: "double-click", 3: "triple-click"}.get(count, "%d clicks" % count)
    else:
        verb = action
    return "%s %s at %s" % (e["button"], verb, fmt_point(e["x"], e["y"]))


def _overlay_click(row: Row) -> OverlayShape | None:
    e = row.event
    if not isinstance(e["x"], (int, float)) or not isinstance(e["y"], (int, float)):
        return None
    return OverlayShape("click", ((float(e["x"]), float(e["y"])),))


def _move_if_away(ctx: PlayContext, x: float, y: float) -> None:
    px, py = ctx.input.position()
    if math.hypot(x - px, y - py) > CLICK_MOVE_THRESHOLD_PX:
        ctx.input.move(x, y)


def _play_click(ctx: PlayContext, row: Row) -> Flow:
    """Press and release ``count`` times, or do the down or up half alone."""
    e = row.event
    button = e["button"]
    _move_if_away(ctx, *ctx.coord(e["x"], e["y"]))
    action = e["action"]
    if action == "down":
        ctx.hold_button(button)
        return Flow.NEXT
    if action == "up":
        ctx.unhold_button(button)
        return Flow.NEXT
    hold_ms = max(0.0, ctx.resolve(e["hold_ms"]))
    interval_ms = max(0.0, ctx.resolve(e["interval_ms"]))
    count = max(1, int(e["count"]))
    for i in range(count):
        ctx.hold_button(button)
        stopped = hold_ms > 0 and ctx.sleep(hold_ms)
        ctx.unhold_button(button)
        if stopped:
            return Flow.STOP
        if i < count - 1 and interval_ms > 0 and ctx.sleep(interval_ms):
            return Flow.STOP
    return Flow.NEXT


MOUSE_CLICK = register(ActionSpec(
    type=EventType.MOUSE_CLICK.value, name="Mouse Click", category="Mouse", filter_group="mouse_click",
    fields=(
        coord_pair("x", "y", "Position"),
        choice("button", "Button", BUTTONS, "left"),
        choice("action", "Action", ("click", "down", "up"), "click"),
        number("count", "Count", 1, minimum=1, maximum=3, integer=True, allow_expr=False,
               visible_when=lambda v: v.get("action") == "click"),
        duration("hold_ms", "Hold", 50, allow_expr=False),
        duration("interval_ms", "Interval", 80, allow_expr=False,
                 visible_when=lambda v: v.get("action") == "click" and (v.get("count") or 1) > 1),
    ),
    presets=(
        Preset("Click", "C", lambda: make("mouse_click", x=100, y=100)),
        Preset("Right-click", "R", lambda: make("mouse_click", x=100, y=100, button="right")),
        Preset("Double-click", None, lambda: make("mouse_click", x=100, y=100, count=2)),
        Preset("Click down", None, lambda: make("mouse_click", x=100, y=100, action="down")),
        Preset("Click up", None, lambda: make("mouse_click", x=100, y=100, action="up")),
    ),
    describe=_describe_click, overlay=_overlay_click, play=_play_click,
))


# -------------------------------------------------------------- mouse_scroll

def _describe_scroll(row: Row) -> str:
    e = row.event
    dx, dy = e["dx"], e["dy"]
    parts = []
    if isinstance(dy, (int, float)) and dy:
        parts.append("%s %s" % ("down" if dy > 0 else "up", fmt_num(abs(dy))))
    elif not isinstance(dy, (int, float)) and dy not in (None, 0, ""):
        parts.append("dy=%s" % dy)
    if isinstance(dx, (int, float)) and dx:
        parts.append("%s %s" % ("right" if dx > 0 else "left", fmt_num(abs(dx))))
    elif not isinstance(dx, (int, float)) and dx not in (None, 0, ""):
        parts.append("dx=%s" % dx)
    text = "scroll " + (", ".join(parts) if parts else "0")
    if e["x"] is not None and e["y"] is not None:
        text += " at %s" % fmt_point(e["x"], e["y"])
    return text


def _sign(value: int) -> int:
    return 1 if value > 0 else -1


def _play_scroll(ctx: PlayContext, row: Row) -> Flow:
    """Move when a position is set, then one notch at a time: vertical, then horizontal.

    The stop flag is checked before every notch, so a huge count from an
    expression stays interruptible even with no notch interval.
    """
    e = row.event
    if e["x"] is not None and e["y"] is not None:
        ctx.input.move(*ctx.coord(e["x"], e["y"]))
    dx = ctx.resolve_int(e["dx"])
    dy = ctx.resolve_int(e["dy"])
    notch_ms = max(0.0, ctx.resolve(e["notch_ms"]))
    first = True
    for count, notch in ((abs(dy), (0, _sign(dy))), (abs(dx), (_sign(dx), 0))):
        for _ in range(count):
            if ctx.stopped() or (not first and notch_ms > 0 and ctx.sleep(notch_ms)):
                return Flow.STOP
            ctx.input.scroll(*notch)
            first = False
    return Flow.NEXT


MOUSE_SCROLL = register(ActionSpec(
    type=EventType.MOUSE_SCROLL.value, name="Mouse Scroll", category="Mouse", filter_group="mouse_scroll",
    fields=(
        coord_pair("x", "y", "Position", default=(None, None), help="Blank keeps the pointer where it is"),
        number("dx", "Horizontal", 0, integer=True),
        number("dy", "Vertical", 3, integer=True, help="Positive scrolls down"),
        duration("notch_ms", "Notch interval", 30, allow_expr=False),
    ),
    presets=(Preset("Scroll", "S", lambda: make("mouse_scroll", dy=3)),),
    describe=_describe_scroll, overlay=_overlay_click, play=_play_scroll,
))
