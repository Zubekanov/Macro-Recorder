"""The safe expression evaluator and template rendering."""

from __future__ import annotations

import pytest

from macro_recorder.engine import expressions
from macro_recorder.engine.expressions import (
    ExpressionError, evaluate, evaluate_condition, is_valid_variable_name, names_in,
    render_template, resolve_number, template_names, truthy,
)


@pytest.mark.parametrize("expr, expected", [
    ("42", 42),
    ("2.5", 2.5),
    ("'hi'", "hi"),
    ('"hi"', "hi"),
    ("2 + 3 * 4", 14),
    ("(2 + 3) * 4", 20),
    ("10 - 3", 7),
    ("10 / 4", 2.5),
    ("10 // 3", 3),
    ("10 % 3", 1),
    ("2 ** 10", 1024),
    ("2 ** -1", 0.5),
    ("-x", -5),
    ("+x", 5),
    ("--x", 5),
    ("'a' + 'b'", "ab"),
    ("  x + 1  ", 6),
])
def test_arithmetic_and_literals(expr, expected):
    assert evaluate(expr, {"x": 5}) == expected


@pytest.mark.parametrize("expr, expected", [
    ("1 < 2", 1),
    ("2 < 1", 0),
    ("1 <= 1", 1),
    ("2 > 1", 1),
    ("1 >= 2", 0),
    ("1 == 1.0", 1),
    ("'a' != 'b'", 1),
    ("1 < 2 < 3", 1),
    ("1 < 3 < 2", 0),
    ("0 < x <= 5", 1),
    ("'a' == 1", 0),
])
def test_comparisons_give_one_or_zero(expr, expected):
    result = evaluate(expr, {"x": 5})
    assert result == expected and type(result) is int


def test_boolean_operators_short_circuit_and_return_operands():
    assert evaluate("0 or 'x'", {}) == "x"
    assert evaluate("'' or 0", {}) == 0
    assert evaluate("3 and 4", {}) == 4
    assert evaluate("0 and missing", {}) == 0         # right side never evaluated
    assert evaluate("1 or missing", {}) == 1
    assert evaluate("not 0", {}) == 1
    assert evaluate("not 'text'", {}) == 0
    assert evaluate("not ''", {}) == 1
    assert evaluate("x > 1 and x < 9", {"x": 5}) == 1


def test_truthiness():
    assert truthy(1) and truthy(-0.5) and truthy("a") and truthy("0")
    assert not truthy(0) and not truthy(0.0) and not truthy("")
    assert evaluate_condition("x", {"x": 2})
    assert not evaluate_condition("x - 2", {"x": 2})
    assert evaluate_condition("name", {"name": "bob"})
    assert not evaluate_condition("name", {"name": ""})


@pytest.mark.parametrize("expr, expected", [
    ("abs(-3)", 3),
    ("round(2.6)", 3),
    ("round(2.345, 2)", 2.35),
    ("floor(2.7)", 2),
    ("ceil(2.1)", 3),
    ("floor(-2.5)", -3),
    ("min(4, 2, 8)", 2),
    ("max(4, 2, 8)", 8),
    ("min('b', 'a')", "a"),
    ("int(3.9)", 3),
    ("int('42')", 42),
    ("int(' 7.8 ')", 7),
    ("str(12) + 'px'", "12px"),
    ("len('hello')", 5),
    ("len(str(1234))", 4),
])
def test_functions(expr, expected):
    assert evaluate(expr, {}) == expected


def test_rnd_uses_uniform(monkeypatch):
    calls = []

    def fake_uniform(a, b):
        calls.append((a, b))
        return 7.5
    monkeypatch.setattr(expressions.random, "uniform", fake_uniform)
    assert evaluate("rnd(5, 10)", {}) == 7.5
    assert calls == [(5, 10)]


def test_rnd_stays_in_range():
    for _ in range(50):
        assert 1 <= evaluate("rnd(1, 2)", {}) <= 2


@pytest.mark.parametrize("expr", [
    "min(1)", "max(1)", "abs()", "abs(1, 2)", "rnd(1)", "round(1, 2, 3)",
    "len(5)", "int('abc')", "abs('a')", "min(1, 'a')", "round(1.5, 0.5)",
])
def test_function_misuse(expr):
    with pytest.raises(ExpressionError):
        evaluate(expr, {})


@pytest.mark.parametrize("expr", [
    "__import__('os')", "x.y", "x[0]", "[1, 2]", "lambda: 1", "x if 1 else 2",
    "open('f')", "abs(x=1)", "abs(*[1])", "True", "None", "1j", "a := 1",
    "f'{x}'", "x is 1", "1 in 2", "~1", "1 << 2", "1 & 2", "", "   ", "1 +", "(",
])
def test_rejected_syntax(expr):
    with pytest.raises(ExpressionError):
        evaluate(expr, {"x": 1})


@pytest.mark.parametrize("expr, variables", [
    ("y + 1", {}),
    ("1 / 0", {}),
    ("1 // 0", {}),
    ("1 % 0", {}),
    ("0 ** -1", {}),
    ("'a' + 1", {}),
    ("'a' * 3", {}),
    ("'a' - 'b'", {}),
    ("-'a'", {}),
    ("'a' < 1", {}),
    ("10.0 ** 1000", {}),
    ("(-8) ** 0.5", {}),
    ("int(1e400)", {}),
])
def test_evaluation_errors(expr, variables):
    with pytest.raises(ExpressionError):
        evaluate(expr, variables)


def test_power_cap():
    assert evaluate("2 ** 1000", {}) == 2 ** 1000
    assert evaluate("2 ** -1000", {}) == 2 ** -1000
    with pytest.raises(ExpressionError, match="larger than 1000"):
        evaluate("2 ** 1001", {})
    with pytest.raises(ExpressionError, match="larger than 1000"):
        evaluate("2 ** -1001", {})
    with pytest.raises(ExpressionError, match="larger than 1000"):
        evaluate("2 ** x", {"x": 10 ** 6})
    with pytest.raises(ExpressionError):
        evaluate("'ab' ** 2", {})
    with pytest.raises(ExpressionError, match="too large"):
        evaluate("(9 ** 1000) ** 1000", {})


def test_runaway_input_is_an_expression_error():
    with pytest.raises(ExpressionError):
        evaluate("(" * 5000 + "1" + ")" * 5000, {})
    with pytest.raises(ExpressionError):
        evaluate("-" * 100000 + "1", {})
    with pytest.raises(ExpressionError):
        evaluate("+".join(["1"] * 200000), {})


def test_errors_are_value_errors():
    assert issubclass(ExpressionError, ValueError)


def test_dynamic_mapping_is_read_on_each_lookup():
    class Dynamic(dict):
        def __init__(self):
            super().__init__()
            self.reads = 0

        def __getitem__(self, key):
            if key == "random":
                self.reads += 1
                return 0.25
            return super().__getitem__(key)

    values = Dynamic()
    assert evaluate("random + random", values) == 0.5
    assert values.reads == 2


def test_resolve_number():
    assert resolve_number(5, {}) == 5
    assert resolve_number(2.5, {}) == 2.5
    assert resolve_number("x * 2", {"x": 4}) == 8
    assert resolve_number("x > 1", {"x": 4}) == 1
    with pytest.raises(ExpressionError, match="Expected a number"):
        resolve_number("name", {"name": "bob"})
    with pytest.raises(ExpressionError):
        resolve_number(None, {})
    with pytest.raises(ExpressionError):
        resolve_number([1], {})
    with pytest.raises(ExpressionError, match="finite"):
        resolve_number("1e308 * 10", {})


def test_render_template():
    v = {"x": 3, "name": "Ann"}
    assert render_template("plain text", v) == "plain text"
    assert render_template("x is {x}", v) == "x is 3"
    assert render_template("{x * 2}{x + 1}", v) == "64"
    assert render_template("Hi {name}!", v) == "Hi Ann!"
    assert render_template("{{literal}} and {{{x}}}", v) == "{literal} and {3}"
    assert render_template("{ x / 2 }", v) == "1.5"
    assert render_template("", v) == ""


@pytest.mark.parametrize("template", ["{x", "x}", "{}", "{ }", "{1 +}", "{y}"])
def test_bad_templates(template):
    with pytest.raises(ExpressionError):
        render_template(template, {"x": 1})


def test_template_str_of_huge_int_is_an_expression_error():
    with pytest.raises(ExpressionError):
        render_template("{9 ** 1000 * 9 ** 1000 * 9 ** 1000 * 9 ** 1000 * 9 ** 1000}", {})


def test_names_in():
    assert names_in("a + b * min(c, 2) - abs(a)") == {"a", "b", "c"}
    assert names_in("'min' + str(x)") == {"x"}
    assert names_in("3") == set()
    with pytest.raises(ExpressionError):
        names_in("a +")
    with pytest.raises(ExpressionError):
        names_in("foo(1)")


def test_template_names():
    assert template_names("At {x}, {y + z} {{w}}") == {"x", "y", "z"}
    assert template_names("no braces") == set()
    with pytest.raises(ExpressionError):
        template_names("{a")


@pytest.mark.parametrize("name, ok", [
    ("x", True), ("_x1", True), ("Count_2", True), ("1x", False), ("", False),
    ("a b", False), ("if", False), ("True", False), ("x-y", False), ("caf\u00e9", False),
])
def test_is_valid_variable_name(name, ok):
    assert is_valid_variable_name(name) is ok
