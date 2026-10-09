"""Wait actions: wait, wait for pixel colour."""

from __future__ import annotations

from macro_recorder.model.actions._common import (
    choice, coord_pair, duration, fmt_ms, fmt_point, number, on_timeout_fields,
)
from macro_recorder.model.catalogue import ActionSpec, FieldKind, FieldSpec, Preset, make, register
from macro_recorder.model.document import Row
from macro_recorder.model.events import EventType
from macro_recorder.model.protocols import OverlayShape


def _describe_wait(row: Row) -> str:
    e = row.event
    if e["random_max_ms"] and (not isinstance(e["random_max_ms"], (int, float))
                               or not isinstance(e["duration_ms"], (int, float))
                               or e["random_max_ms"] > e["duration_ms"]):
        return "%s to %s" % (fmt_ms(e["duration_ms"]), fmt_ms(e["random_max_ms"]))
    return fmt_ms(e["duration_ms"])


WAIT = register(ActionSpec(
    type=EventType.WAIT.value, name="Wait", category="Wait", filter_group="wait",
    fields=(
        duration("duration_ms", "Duration", 500),
        duration("random_max_ms", "Random up to", 0, help="0 for a fixed wait"),
    ),
    presets=(Preset("Wait", "W", lambda: make("wait", duration_ms=500)),),
    describe=_describe_wait,
))


def _describe_pixel(row: Row) -> str:
    e = row.event
    return "pixel %s %s %s within %s" % (
        fmt_point(e["x"], e["y"]), "is" if e["mode"] == "equals" else "is not", e["colour"],
        fmt_ms(e["timeout_ms"]))


def _overlay_pixel(row: Row) -> OverlayShape | None:
    e = row.event
    if not isinstance(e["x"], (int, float)) or not isinstance(e["y"], (int, float)):
        return None
    return OverlayShape("point", ((float(e["x"]), float(e["y"])),))


WAIT_PIXEL = register(ActionSpec(
    type=EventType.WAIT_PIXEL.value, name="Wait for Pixel", category="Wait",
    fields=(
        FieldSpec(keys=("x", "y"), label="Pixel", kind=FieldKind.COORD_PAIR, default=(100, 100),
                  targeting_fills=("x", "y", "colour")),
        FieldSpec(keys=("colour",), label="Colour", kind=FieldKind.COLOUR, default="#ffffff"),
        choice("mode", "Wait until", ("equals", "differs"), "equals"),
        number("colour_tolerance", "Tolerance", 0, minimum=0, maximum=255, integer=True,
               allow_expr=False, help="Max difference per channel"),
        duration("timeout_ms", "Timeout", 10000),
    ) + on_timeout_fields("abort"),
    presets=(Preset("Pixel", "P", lambda: make("wait_pixel", x=100, y=100)),),
    describe=_describe_pixel, overlay=_overlay_pixel,
))
