"""var_set and ocr_read play, and the variable-name helpers."""

import pytest

from macro_recorder.model.actions.variables import (
    DYNAMIC_NAMES, assign, is_valid_variable_name, name_problem,
)
from macro_recorder.model.catalogue import Abort, Flow
from macro_recorder.model.protocols import Rect
from tests._fake_context import ctx, play, row  # noqa: F401


@pytest.mark.parametrize("expr, expected", [
    ("42", 42),
    ("2.5 * 4", 10.0),
    ("'hello'", "hello"),
    ("a + 1", 8),
    ("s + ' world'", "hello world"),
    ("a > 3", 1),
])
def test_var_set_values(ctx, expr, expected):
    ctx.variables["a"] = 7
    ctx.variables["s"] = "hello"
    assert play(ctx, row("var_set", var_name="x", expr=expr)) is Flow.NEXT
    assert ctx.variables["x"] == expected


def test_var_set_reads_dynamic_values(ctx):
    ctx.variables.dynamic["loop"] = 3
    play(ctx, row("var_set", var_name="n", expr="loop * 10"))
    assert ctx.variables["n"] == 30


@pytest.mark.parametrize("name", sorted(DYNAMIC_NAMES))
def test_var_set_reserved_names_abort(ctx, name):
    before = ctx.variables[name]
    result = play(ctx, row("var_set", var_name=name, expr="5"))
    assert isinstance(result, Abort)
    assert name in result.message and "dynamic" in result.message
    assert ctx.variables[name] == before


@pytest.mark.parametrize("name", ["", "1x", "if", "a-b", "two words"])
def test_var_set_invalid_names_abort(ctx, name):
    result = play(ctx, row("var_set", var_name=name, expr="5"))
    assert isinstance(result, Abort)
    assert "not a valid variable name" in result.message
    assert ctx.variables.user == {}


def test_var_set_expression_error_propagates(ctx):
    with pytest.raises(ValueError):
        play(ctx, row("var_set", var_name="x", expr="undefined_name + 1"))


def test_name_helpers(ctx):
    assert is_valid_variable_name("x_1")
    assert not is_valid_variable_name("for")
    assert not is_valid_variable_name(None)
    assert name_problem("total") is None
    assert isinstance(name_problem("random"), Abort)
    assert assign(ctx, "total", 3) is None
    assert ctx.variables["total"] == 3
    assert isinstance(assign(ctx, "rt_ms", 3), Abort)


def test_ocr_read_stores_stripped_text_from_framed_region(ctx):
    ctx.origin = (100, 110)
    ctx.screen.text = "  Total: 42\n"
    result = play(ctx, row("ocr_read", var_name="text", x=10, y=20, x2=110, y2=70))
    assert result is Flow.NEXT
    assert ctx.variables["text"] == "Total: 42"
    assert ctx.screen.captures == [Rect(110, 130, 100, 50)]


def test_ocr_read_monitor_region(ctx):
    ctx.screen.text = "abc"
    play(ctx, row("ocr_read", var_name="t", monitor=1))
    assert ctx.screen.captures == [Rect(0, 0, 1920, 1080)]
    assert ctx.variables["t"] == "abc"


def test_ocr_read_reserved_name_aborts_before_ocr(ctx):
    result = play(ctx, row("ocr_read", var_name="mouse_x"))
    assert isinstance(result, Abort)
    assert ctx.screen.captures == []
