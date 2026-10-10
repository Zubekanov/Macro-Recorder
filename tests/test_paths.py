"""Path generators: linear, spline, natural."""

import math
import random

import pytest

from macro_recorder.engine.paths import generate


def _close(a, b, tol=1e-6):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


@pytest.mark.parametrize("mode", ["linear", "spline", "natural"])
def test_endpoints_count_and_timing(mode):
    samples = generate(mode, (10, 20), (310, 420), 500, random.Random(1), via=[(100, 300)])
    assert len(samples) == 51
    assert samples[0] == (0.0, 10.0, 20.0)
    assert samples[-1] == (500.0, 310.0, 420.0)
    times = [t for t, _, _ in samples]
    assert times == sorted(times)
    assert all(abs((b - a) - 10.0) < 1e-9 for a, b in zip(times, times[1:]))


@pytest.mark.parametrize("duration", [0, 3, 10])
def test_short_durations_give_at_least_two_samples(duration):
    samples = generate("linear", (0, 0), (100, 0), duration, random.Random(0))
    assert len(samples) >= 2
    assert samples[0][1:] == (0.0, 0.0) and samples[-1][1:] == (100.0, 0.0)
    assert samples[-1][0] == float(duration)


def test_linear_is_evenly_spaced_on_the_line():
    samples = generate("linear", (0, 0), (100, 50), 100, random.Random(0))
    for i, (t, x, y) in enumerate(samples):
        assert _close((x, y), (i * 10.0, i * 5.0))


def test_natural_is_deterministic_per_rng():
    a = generate("natural", (0, 0), (400, 0), 300, random.Random(7))
    b = generate("natural", (0, 0), (400, 0), 300, random.Random(7))
    c = generate("natural", (0, 0), (400, 0), 300, random.Random(8))
    assert a == b
    assert a != c


def test_natural_stays_near_the_line_and_eases():
    for seed in range(20):
        samples = generate("natural", (0, 0), (400, 0), 300, random.Random(seed))
        # Control points are at most 15% of the distance off the line, and a
        # cubic Bezier stays inside their hull.
        assert all(abs(y) <= 0.15 * 400 + 1e-9 for _, _, y in samples)
    steps = [math.dist(a[1:], b[1:]) for a, b in zip(samples, samples[1:])]
    middle = steps[len(steps) // 2]
    assert steps[0] < middle and steps[-1] < middle


def test_natural_zero_distance():
    samples = generate("natural", (5, 5), (5, 5), 50, random.Random(0))
    assert all((x, y) == (5.0, 5.0) for _, x, y in samples)


def test_spline_passes_through_thinned_via_points():
    via = [(0, 0), (5, 5), (200, 200), (205, 202), (400, 0)]
    samples = generate("spline", (0, 0), (400, 0), 1000, random.Random(0), via=via)
    nearest = min(math.dist((x, y), (200, 200)) for _, x, y in samples)
    assert nearest < 5
    # (5, 5) and (205, 202) are within 40 px of a kept point, so the curve
    # does not bend to them separately: it stays smooth near the peak.
    assert max(y for _, _, y in samples) < 230


def test_spline_without_via_is_a_line():
    samples = generate("spline", (0, 0), (100, 0), 100, random.Random(0))
    assert all(abs(y) < 1e-9 for _, _, y in samples)


def test_unknown_mode():
    with pytest.raises(ValueError):
        generate("recorded", (0, 0), (1, 1), 10, random.Random(0))
