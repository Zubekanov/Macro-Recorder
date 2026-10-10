"""goto, if_else and repeat play."""

from typing import Any

import pytest

from macro_recorder.model.catalogue import END_TARGET, START_TARGET, Abort, Flow, Jump, make
from macro_recorder.model.document import Row, make_row
from tests._fake_context import FakePlayContext, ctx, play, row  # noqa: F401


def test_goto_jumps(ctx):
    assert play(ctx, row("goto", target="loop")) == Jump("loop")
    assert play(ctx, row("goto")) == Jump(END_TARGET)


def test_goto_without_target_aborts(ctx):
    assert isinstance(play(ctx, row("goto", target=None)), Abort)


@pytest.mark.parametrize("x, else_target, expected", [
    (5, None, Jump("then")),
    (0, None, Flow.NEXT),
    (0, "other", Jump("other")),
    (-1, START_TARGET, Jump(START_TARGET)),
])
def test_if_else_branches(ctx, x, else_target, expected):
    ctx.variables["x"] = x
    r = row("if_else", expr="x > 0", target="then", else_target=else_target)
    assert play(ctx, r) == expected


@pytest.mark.parametrize("value, taken", [("'text'", True), ("''", False), ("2", True), ("0", False)])
def test_if_else_truth(ctx, value, taken):
    result = play(ctx, row("if_else", expr=value, target="yes"))
    assert result == (Jump("yes") if taken else Flow.NEXT)


def test_if_else_without_then_target_falls_through(ctx):
    assert play(ctx, row("if_else", expr="1", target=None)) is Flow.NEXT


def test_repeat_counts(ctx):
    r = row("repeat", target="body", count=3)
    assert play(ctx, r) == Jump("body")
    assert ctx.row_state(r.id) == {"counter": 1, "count": 3, "pass": 2}
    assert play(ctx, r) == Jump("body")
    assert ctx.row_state(r.id)["pass"] == 3
    assert play(ctx, r) is Flow.NEXT
    assert ctx.row_state(r.id) == {}
    # Reached again (an outer loop): a fresh run of three passes.
    assert [play(ctx, r) for _ in range(3)] == [Jump("body"), Jump("body"), Flow.NEXT]


@pytest.mark.parametrize("count", [1, 0, -2])
def test_repeat_count_at_most_one_falls_through(ctx, count):
    r = row("repeat", target="body", count=count)
    assert play(ctx, r) is Flow.NEXT
    assert ctx.row_state(r.id) == {}


def test_repeat_count_expression_read_once_per_run(ctx):
    ctx.variables["n"] = 3
    r = row("repeat", target="body", count="n")
    assert play(ctx, r) == Jump("body")
    ctx.variables["n"] = 10           # ignored until the loop finishes
    assert play(ctx, r) == Jump("body")
    assert play(ctx, r) is Flow.NEXT
    ctx.variables["n"] = 2
    assert play(ctx, r) == Jump("body")
    assert play(ctx, r) is Flow.NEXT


def test_repeat_without_target_aborts(ctx):
    assert isinstance(play(ctx, row("repeat", target=None, count=3)), Abort)


# ---------------------------------------------------------------------------
# Nested repeats driven by a minimal program counter
# ---------------------------------------------------------------------------

def _run(ctx: FakePlayContext, rows: list[Row], passes: int, trace: list[Any]) -> None:
    """Walk the rows like the engine: clear row state per pass, follow jumps."""
    labels = {r.label: i for i, r in enumerate(rows) if r.label}
    for _ in range(passes):
        ctx.states.clear()
        pc = 0
        while pc < len(rows):
            r = rows[pc]
            result = play(ctx, r)
            if r.type == "var_set":
                trace.append((r.event["var_name"], _passes(ctx, rows)))
            if result is Flow.NEXT:
                pc += 1
            else:
                assert isinstance(result, Jump), result
                pc = len(rows) if result.target == END_TARGET else labels[result.target]


def _passes(ctx: FakePlayContext, rows: list[Row]) -> tuple[int, ...]:
    """The 'pass' of every repeat row (1 when not mid-loop)."""
    return tuple(ctx.row_state(r.id).get("pass", 1) for r in rows if r.type == "repeat")


def test_nested_repeats_across_program_passes(ctx):
    ctx.variables["outer_n"] = 0
    ctx.variables["inner_n"] = 0
    rows = [
        make_row(make("var_set", var_name="outer_n", expr="outer_n + 1"), label="outer"),
        make_row(make("var_set", var_name="inner_n", expr="inner_n + 1"), label="inner"),
        make_row(make("repeat", target="inner", count=3)),
        make_row(make("repeat", target="outer", count=2)),
    ]
    trace: list[Any] = []
    _run(ctx, rows, passes=2, trace=trace)
    assert ctx.variables["outer_n"] == 4
    assert ctx.variables["inner_n"] == 12
    # One program pass: (inner pass, outer pass) seen by each body row.
    one_pass = [
        ("outer_n", (1, 1)), ("inner_n", (1, 1)), ("inner_n", (2, 1)), ("inner_n", (3, 1)),
        ("outer_n", (1, 2)), ("inner_n", (1, 2)), ("inner_n", (2, 2)), ("inner_n", (3, 2)),
    ]
    assert trace == one_pass * 2


def test_counter_left_mid_loop_resets_at_next_program_pass(ctx):
    ctx.variables["n"] = 0
    rows = [
        make_row(make("var_set", var_name="n", expr="n + 1"), label="body"),
        make_row(make("if_else", expr="n == 2", target=END_TARGET)),
        make_row(make("repeat", target="body", count=5)),
    ]
    _run(ctx, rows, passes=1, trace=[])
    assert ctx.variables["n"] == 2
    ctx.variables["n"] = 10
    _run(ctx, rows, passes=1, trace=[])
    assert ctx.variables["n"] == 15
