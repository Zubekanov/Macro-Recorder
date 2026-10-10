"""Pure pointer path generators: linear, spline and natural.

Each generator returns ``(t_ms, x, y)`` samples from ``start`` to ``end``,
roughly one every 10 ms and never fewer than two.  The first sample is the
start at ``t = 0`` and the last is the end at ``t = duration_ms``.  Given the
same ``rng`` state the output is the same.
"""

from __future__ import annotations

import math
import random
from typing import Sequence

Point = tuple[float, float]
Sample = tuple[float, float, float]

MODES = ("linear", "spline", "natural")
SAMPLE_MS = 10.0
SPLINE_SPACING_PX = 40.0
NATURAL_OFFSET = 0.15


def generate(mode: str, start: Point, end: Point, duration_ms: float, rng: random.Random,
             via: Sequence[Point] | None = None) -> list[Sample]:
    """Samples of a pointer path from ``start`` to ``end`` over ``duration_ms``.

    ``linear`` is a straight line at constant speed.  ``spline`` is a
    Catmull-Rom curve through ``via`` thinned to one point per 40 px (a
    straight line without ``via``).  ``natural`` is a cubic Bezier whose two
    control points sit up to 15% of the distance off the line, with
    ease-in-out timing.  Raises ValueError for an unknown mode.
    """
    if mode not in MODES:
        raise ValueError("Unknown path mode %r" % mode)
    duration_ms = max(0.0, float(duration_ms))
    count = max(2, int(math.ceil(duration_ms / SAMPLE_MS)) + 1)
    times = [duration_ms * i / (count - 1) for i in range(count)]
    start = (float(start[0]), float(start[1]))
    end = (float(end[0]), float(end[1]))
    if mode == "linear":
        points = [_lerp(start, end, i / (count - 1)) for i in range(count)]
    elif mode == "spline":
        points = _spline(_thin(start, end, via or ()), count)
    else:
        points = _natural(start, end, count, rng)
    points[0], points[-1] = start, end
    return [(t, p[0], p[1]) for t, p in zip(times, points)]


def _lerp(a: Point, b: Point, u: float) -> Point:
    return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)


def _distance(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _thin(start: Point, end: Point, via: Sequence[Point]) -> list[Point]:
    """``start``, the via points at least 40 px apart, then ``end``."""
    kept = [start]
    for p in via:
        p = (float(p[0]), float(p[1]))
        if _distance(kept[-1], p) >= SPLINE_SPACING_PX:
            kept.append(p)
    if len(kept) > 1 and _distance(kept[-1], end) < SPLINE_SPACING_PX:
        kept.pop()
    kept.append(end)
    return kept


def _catmull_rom(p0: Point, p1: Point, p2: Point, p3: Point, u: float) -> Point:
    u2, u3 = u * u, u * u * u
    return tuple(
        0.5 * (2 * p1[k] + (-p0[k] + p2[k]) * u + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * u2
               + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * u3)
        for k in (0, 1))  # type: ignore[return-value]


def _spline(knots: list[Point], count: int) -> list[Point]:
    """``count`` points along a Catmull-Rom curve through ``knots``, spaced by chord length."""
    if len(knots) == 2:
        return [_lerp(knots[0], knots[1], i / (count - 1)) for i in range(count)]
    padded = [knots[0]] + knots + [knots[-1]]
    lengths = [_distance(knots[i], knots[i + 1]) for i in range(len(knots) - 1)]
    total = sum(lengths)
    out: list[Point] = []
    for i in range(count):
        along = total * i / (count - 1)
        seg = 0
        while seg < len(lengths) - 1 and along > lengths[seg]:
            along -= lengths[seg]
            seg += 1
        u = along / lengths[seg] if lengths[seg] else 0.0
        u = min(1.0, max(0.0, u))
        out.append(_catmull_rom(padded[seg], padded[seg + 1], padded[seg + 2], padded[seg + 3], u))
    return out


def _natural(start: Point, end: Point, count: int, rng: random.Random) -> list[Point]:
    """A cubic Bezier with randomly offset control points and ease-in-out timing."""
    dist = _distance(start, end)
    if dist == 0:
        return [start] * count
    nx, ny = -(end[1] - start[1]) / dist, (end[0] - start[0]) / dist
    off1 = rng.uniform(-NATURAL_OFFSET, NATURAL_OFFSET) * dist
    off2 = rng.uniform(-NATURAL_OFFSET, NATURAL_OFFSET) * dist
    a, b = _lerp(start, end, 1 / 3), _lerp(start, end, 2 / 3)
    c1 = (a[0] + nx * off1, a[1] + ny * off1)
    c2 = (b[0] + nx * off2, b[1] + ny * off2)
    out: list[Point] = []
    for i in range(count):
        u = 0.5 - 0.5 * math.cos(math.pi * i / (count - 1))
        v = 1 - u
        out.append(tuple(
            v * v * v * start[k] + 3 * v * v * u * c1[k] + 3 * v * u * u * c2[k] + u * u * u * end[k]
            for k in (0, 1)))  # type: ignore[arg-type]
    return out
