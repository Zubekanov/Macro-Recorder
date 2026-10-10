"""Wait actions: wait, wait for pixel colour.

A plain wait scales with the playback speed; the pixel poll interval and
timeout do not.
"""

from __future__ import annotations

import random
import re
import time
from typing import Callable

from macro_recorder.model.actions._common import (
    choice, duration, fmt_ms, fmt_point, number, on_timeout_fields,
)
from macro_recorder.model.catalogue import (
    Abort, ActionSpec, FieldKind, FieldSpec, Flow, Jump, Preset, make, register,
)
from macro_recorder.model.document import Row
from macro_recorder.model.events import EventType
from macro_recorder.model.protocols import OverlayShape, PlayContext

# Source of random wait lengths; tests replace it with a seeded or stub generator.
rng: random.Random = random.Random()

# Monotonic seconds for the pixel timeout; tests replace it with a fake clock.
clock: Callable[[], float] = time.monotonic

PIXEL_POLL_MS = 50

_COLOUR_RE = re.compile(r"^#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})$")


def parse_colour(text: str) -> tuple[int, int, int]:
    """``"#ff8000"`` -> ``(255, 128, 0)``.  Raises ValueError for anything else."""
    m = _COLOUR_RE.match(text.strip())
    if m is None:
        raise ValueError("Colour must be #rrggbb, not %r" % text)
    return (int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16))


def format_colour(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(rgb)


def colour_matches(a: tuple[int, int, int], b: tuple[int, int, int], tolerance: int) -> bool:
    """True when every channel differs by at most ``tolerance``."""
    return all(abs(int(p) - int(q)) <= tolerance for p, q in zip(a, b))


def _describe_wait(row: Row) -> str:
    e = row.event
    if e["random_max_ms"] and (not isinstance(e["random_max_ms"], (int, float))
                               or not isinstance(e["duration_ms"], (int, float))
                               or e["random_max_ms"] > e["duration_ms"]):
        return "%s to %s" % (fmt_ms(e["duration_ms"]), fmt_ms(e["random_max_ms"]))
    return fmt_ms(e["duration_ms"])


def _play_wait(ctx: PlayContext, row: Row) -> Flow:
    """Sleep the duration, or a uniform random time up to ``random_max_ms``."""
    e = row.event
    low = max(0.0, ctx.resolve(e["duration_ms"]))
    high = max(0.0, ctx.resolve(e["random_max_ms"]))
    ms = rng.uniform(low, high) if high > low else low
    if ms > 0 and ctx.sleep(ms):
        return Flow.STOP
    return Flow.NEXT


WAIT = register(ActionSpec(
    type=EventType.WAIT.value, name="Wait", category="Wait", filter_group="wait",
    fields=(
        duration("duration_ms", "Duration", 500),
        duration("random_max_ms", "Random up to", 0, help="0 for a fixed wait"),
    ),
    presets=(Preset("Wait", "W", lambda: make("wait", duration_ms=500)),),
    describe=_describe_wait, play=_play_wait,
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


def _play_pixel(ctx: PlayContext, row: Row) -> Flow | Jump | Abort:
    """Poll the pixel every PIXEL_POLL_MS until it matches or the timeout passes."""
    e = row.event
    try:
        wanted = parse_colour(e["colour"])
    except ValueError as err:
        return Abort(str(err))
    fx, fy = ctx.coord(e["x"], e["y"])
    x, y = int(round(fx)), int(round(fy))
    tolerance = int(e["colour_tolerance"])
    equals = e["mode"] == "equals"
    timeout_ms = max(0.0, ctx.resolve(e["timeout_ms"]))
    deadline = clock() + timeout_ms / 1000.0
    while True:
        if colour_matches(ctx.screen.pixel(x, y), wanted, tolerance) == equals:
            return Flow.NEXT
        remaining_ms = (deadline - clock()) * 1000.0
        if remaining_ms <= 0:
            break
        if ctx.sleep(min(PIXEL_POLL_MS, remaining_ms), scaled=False):
            return Flow.STOP
    message = "pixel (%d, %d) did not %s %s within %d ms" % (
        x, y, "become" if equals else "change from", format_colour(wanted), int(round(timeout_ms)))
    on_timeout = e["on_timeout"]
    if on_timeout == "continue":
        ctx.log(message + "; continuing")
        return Flow.NEXT
    if on_timeout == "goto":
        ctx.log(message + "; going to %s" % e["target"])
        return Jump(e["target"])
    return Abort(message)


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
    describe=_describe_pixel, overlay=_overlay_pixel, play=_play_pixel,
))
