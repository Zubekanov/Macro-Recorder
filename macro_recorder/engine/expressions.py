"""Safe expression evaluation and ``{expr}`` template rendering.

Expressions are a small subset of Python parsed with :mod:`ast` and checked
node by node before anything is evaluated, so a macro can never run
arbitrary code.  The grammar:

- numbers, ``'strings'`` (single or double quotes) and variable names;
- ``+ - * / // % **`` on numbers, ``+`` also joins two strings; an exponent
  whose absolute value exceeds 1000 is rejected, as is a string base;
- comparisons ``< <= > >= == !=``, chained as in Python;
- ``and``, ``or``, ``not``, parentheses, unary ``+`` and ``-``;
- the functions ``abs round floor ceil min max int str len rnd(a, b)``.

Comparisons and ``not`` give 1 or 0.  ``and`` and ``or`` short-circuit and
give the deciding operand, as in Python.  Truth is a non-zero number or a
non-empty string.  Every failure, including division by zero, overflow and
runaway recursion, raises :class:`ExpressionError`.

Templates are text with ``{expr}`` segments rendered with ``str()``;
``{{`` and ``}}`` are literal braces.
"""

from __future__ import annotations

import ast
import functools
import keyword
import math
import operator
import random
import re
from typing import Any, Callable, Mapping

Value = int | float | str

MAX_EXPONENT = 1000
# Integer powers whose result would exceed this many bits are refused so a
# nested power such as (9 ** 1000) ** 1000 cannot exhaust memory.
MAX_POWER_BITS = 1_000_000
# The largest digit count round() takes, in either sign.
MAX_ROUND_DIGITS = 1000


class ExpressionError(ValueError):
    """An expression or template cannot be parsed or evaluated."""


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------

_ARITHMETIC: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_OP_TEXT = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//",
            ast.Mod: "%", ast.Pow: "**"}

_COMPARISONS: dict[type, Callable[[Any, Any], bool]] = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}


def _number(value: Any, name: str) -> int | float:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    raise ExpressionError("%s() needs a number, not %r" % (name, value))


def _fn_int(value: Value) -> int:
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return int(float(text))
        except ValueError:
            raise ExpressionError("int() cannot convert %r" % value) from None
    return int(_number(value, "int"))


def _fn_round(value: Value, digits: Value | None = None) -> int | float:
    number = _number(value, "round")
    if digits is None:
        return round(number)
    places = _number(digits, "round")
    if not isinstance(places, int):
        raise ExpressionError("round() needs a whole number of digits")
    # round(int, -n) builds 10 ** n in one C call that Stop cannot interrupt.
    if abs(places) > MAX_ROUND_DIGITS:
        raise ExpressionError("round() digits must be between -%d and %d"
                              % (MAX_ROUND_DIGITS, MAX_ROUND_DIGITS))
    return round(number, places)


def _fn_len(value: Value) -> int:
    if not isinstance(value, str):
        raise ExpressionError("len() needs a string, not %r" % (value,))
    return len(value)


def _fn_minmax(fn: Callable[..., Any], name: str) -> Callable[..., Value]:
    def apply(*values: Value) -> Value:
        if all(isinstance(v, str) for v in values):
            return fn(values)
        return fn(_number(v, name) for v in values)
    return apply


def _fn_rnd(low: Value, high: Value) -> float:
    return random.uniform(_number(low, "rnd"), _number(high, "rnd"))


# name -> (callable, minimum arguments, maximum arguments or None for any)
FUNCTIONS: dict[str, tuple[Callable[..., Value], int, int | None]] = {
    "abs": (lambda v: abs(_number(v, "abs")), 1, 1),
    "round": (_fn_round, 1, 2),
    "floor": (lambda v: math.floor(_number(v, "floor")), 1, 1),
    "ceil": (lambda v: math.ceil(_number(v, "ceil")), 1, 1),
    "min": (_fn_minmax(min, "min"), 2, None),
    "max": (_fn_minmax(max, "max"), 2, None),
    "int": (_fn_int, 1, 1),
    "str": (lambda v: _to_text(v), 1, 1),
    "len": (_fn_len, 1, 1),
    "rnd": (_fn_rnd, 2, 2),
}


def _check(node: ast.AST) -> None:
    """Reject every node outside the grammar."""
    if isinstance(node, ast.Expression):
        _check(node.body)
    elif isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float, str)):
            raise ExpressionError("Unsupported value %r" % (node.value,))
    elif isinstance(node, ast.Name):
        if not isinstance(node.ctx, ast.Load):
            raise ExpressionError("Assignment is not allowed")
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in _ARITHMETIC:
            raise ExpressionError("Unsupported operator %s" % type(node.op).__name__)
        _check(node.left)
        _check(node.right)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
            raise ExpressionError("Unsupported operator %s" % type(node.op).__name__)
        _check(node.operand)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            _check(value)
    elif isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _COMPARISONS:
                raise ExpressionError("Unsupported comparison %s" % type(op).__name__)
        _check(node.left)
        for comparator in node.comparators:
            _check(comparator)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
            raise ExpressionError("Unknown function %s" % ast.unparse(node.func))
        name = node.func.id
        if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
            raise ExpressionError("%s() takes plain arguments only" % name)
        _, low, high = FUNCTIONS[name]
        count = len(node.args)
        if count < low or (high is not None and count > high):
            if high is None:
                wanted = "at least %d" % low
            elif low == high:
                wanted = str(low)
            else:
                wanted = "%d to %d" % (low, high)
            raise ExpressionError("%s() takes %s arguments, not %d" % (name, wanted, count))
        for arg in node.args:
            _check(arg)
    else:
        raise ExpressionError("Unsupported expression element: %s" % type(node).__name__)


@functools.lru_cache(maxsize=1024)
def _compile(expr: str) -> ast.Expression:
    """Parse and check an expression once; the tree is never mutated."""
    if not isinstance(expr, str):
        raise ExpressionError("An expression must be text, not %r" % (expr,))
    if not expr.strip():
        raise ExpressionError("Empty expression")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
        _check(tree)
    except SyntaxError as e:
        raise ExpressionError("Invalid expression %r: %s" % (expr, e.msg)) from None
    except (RecursionError, MemoryError):
        raise ExpressionError("Expression %r is nested too deeply" % expr) from None
    return tree


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def truthy(value: Any) -> bool:
    """A non-zero number or a non-empty string."""
    if isinstance(value, str):
        return value != ""
    return value != 0


def _to_text(value: Any) -> str:
    try:
        return str(value)
    except ValueError as e:          # an integer too long to print
        raise ExpressionError(str(e)) from None


def _power(base: Any, exponent: Any) -> Any:
    if isinstance(base, str) or isinstance(exponent, str):
        raise ExpressionError("** needs numbers")
    if abs(exponent) > MAX_EXPONENT:
        raise ExpressionError("Exponent %r is larger than %d" % (exponent, MAX_EXPONENT))
    if isinstance(base, int) and isinstance(exponent, int) and abs(base) > 1 and exponent > 0:
        if base.bit_length() * exponent > MAX_POWER_BITS:
            raise ExpressionError("Result of ** is too large")
    result = base ** exponent
    if isinstance(result, complex):
        raise ExpressionError("%r ** %r has no real value" % (base, exponent))
    return result


def _arithmetic(op: ast.operator, left: Any, right: Any) -> Any:
    if isinstance(op, ast.Pow):
        return _power(left, right)
    if isinstance(op, ast.Add) and isinstance(left, str) and isinstance(right, str):
        return left + right
    if isinstance(left, str) or isinstance(right, str):
        raise ExpressionError("Cannot apply %s to %r and %r" % (_OP_TEXT[type(op)], left, right))
    return _ARITHMETIC[type(op)](left, right)


def _eval(node: ast.AST, variables: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        try:
            return variables[node.id]
        except KeyError:
            raise ExpressionError("Undefined variable %r" % node.id) from None
    if isinstance(node, ast.BinOp):
        return _arithmetic(node.op, _eval(node.left, variables), _eval(node.right, variables))
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, variables)
        if isinstance(node.op, ast.Not):
            return 0 if truthy(operand) else 1
        if isinstance(operand, str):
            raise ExpressionError("Unary minus or plus needs a number, not %r" % operand)
        return -operand if isinstance(node.op, ast.USub) else +operand
    if isinstance(node, ast.BoolOp):
        result = _eval(node.values[0], variables)
        for operand in node.values[1:]:
            if truthy(result) != isinstance(node.op, ast.And):
                return result
            result = _eval(operand, variables)
        return result
    if isinstance(node, ast.Compare):
        left = _eval(node.left, variables)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, variables)
            if not _COMPARISONS[type(op)](left, right):
                return 0
            left = right
        return 1
    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)       # guaranteed by _check
        fn = FUNCTIONS[node.func.id][0]
        return fn(*(_eval(a, variables) for a in node.args))
    raise ExpressionError("Unsupported expression element: %s" % type(node).__name__)


def evaluate(expr: str, variables: Mapping[str, Any]) -> Value:
    """Evaluate ``expr`` against ``variables`` to a number or a string.

    ``variables`` may compute values on read (the player's dynamic values);
    a missing name raises KeyError from it and is reported as undefined.
    """
    tree = _compile(expr)
    try:
        result = _eval(tree.body, variables)
    except ExpressionError:
        raise
    except ZeroDivisionError:
        raise ExpressionError("Division by zero in %r" % expr) from None
    except (TypeError, ValueError, OverflowError) as e:
        raise ExpressionError("%s in %r" % (e, expr)) from None
    except (RecursionError, MemoryError):
        raise ExpressionError("Expression %r is too large to evaluate" % expr) from None
    if isinstance(result, bool):
        return int(result)
    return result


def evaluate_condition(expr: str, variables: Mapping[str, Any]) -> bool:
    """Evaluate ``expr`` and apply the truth rule."""
    return truthy(evaluate(expr, variables))


def resolve_number(value: Any, variables: Mapping[str, Any]) -> int | float:
    """A number field's value: numbers pass through, strings are evaluated.

    The result must be a finite number; a string result (a text variable
    where a number is expected) raises ExpressionError.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        result: Any = value
    elif isinstance(value, str):
        result = evaluate(value, variables)
        if isinstance(result, str):
            raise ExpressionError("Expected a number but %r gave %r" % (value, result))
    else:
        raise ExpressionError("Expected a number or an expression, not %r" % (value,))
    if isinstance(result, float) and not math.isfinite(result):
        raise ExpressionError("%r is not a finite number" % (value,))
    try:
        float(result)
    except OverflowError:
        raise ExpressionError("%r is too large to use as a number" % (value,)) from None
    return result


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=512)
def _split_template(template: str) -> tuple[tuple[bool, str], ...]:
    """Split into ``(is_expression, text)`` parts, checking every expression."""
    parts: list[tuple[bool, str]] = []
    literal: list[str] = []
    i, n = 0, len(template)
    while i < n:
        c = template[i]
        if c == "{" and template.startswith("{{", i):
            literal.append("{")
            i += 2
        elif c == "}" and template.startswith("}}", i):
            literal.append("}")
            i += 2
        elif c == "{":
            close = template.find("}", i + 1)
            if close == -1:
                raise ExpressionError("Unmatched '{' in %r" % template)
            expr = template[i + 1:close]
            if not expr.strip():
                raise ExpressionError("Empty {} in %r" % template)
            _compile(expr)
            if literal:
                parts.append((False, "".join(literal)))
                literal = []
            parts.append((True, expr))
            i = close + 1
        elif c == "}":
            raise ExpressionError("Unmatched '}' in %r; write }} for a brace" % template)
        else:
            literal.append(c)
            i += 1
    if literal:
        parts.append((False, "".join(literal)))
    return tuple(parts)


def render_template(template: str, variables: Mapping[str, Any]) -> str:
    """Replace each ``{expr}`` with ``str()`` of its value."""
    return "".join(_to_text(evaluate(text, variables)) if is_expr else text
                   for is_expr, text in _split_template(template))


# ---------------------------------------------------------------------------
# Static inspection
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def is_valid_variable_name(name: str) -> bool:
    """An ASCII identifier that is not a Python keyword."""
    return isinstance(name, str) and bool(_NAME_RE.match(name)) and not keyword.iskeyword(name)


def names_in(expr: str) -> set[str]:
    """The variable names ``expr`` reads (function names excluded).

    Raises ExpressionError when the expression is not valid, so this doubles
    as a syntax check.
    """
    tree = _compile(expr)
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in called}


def template_names(template: str) -> set[str]:
    """The variable names a template's ``{expr}`` segments read.

    Raises ExpressionError when a brace or an expression is not valid.
    """
    names: set[str] = set()
    for is_expr, text in _split_template(template):
        if is_expr:
            names |= names_in(text)
    return names
