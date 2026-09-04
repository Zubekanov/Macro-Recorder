"""Safe expression evaluation for macro variables and conditions.

Expressions are evaluated at playback time against a variable mapping, e.g.
``evaluate("var1 + 3", {"var1": 5})`` → 8, ``evaluate("x > 3", {"x": 5})`` →
True, or ``evaluate('a + "!"', {"a": "hi"})`` → "hi!".  The grammar is a safe
subset of Python parsed with ``ast``: numeric, boolean, and string literals,
variable names, arithmetic (+ - * / // % **), unary +/-, ``not``, comparisons
(< > <= >= == !=, chained), and boolean ``and``/``or``, with parentheses.
There is deliberately no support for function calls, attribute access,
indexing, or any other construct, so evaluating a macro can never execute
arbitrary code.  Values may be numbers, booleans, or strings; a type mismatch
(e.g. ``"a" + 1``) and referencing an undefined variable both raise
ExpressionError, so they are errors rather than silent surprises.

Helpers
-------
- ``evaluate``           — evaluate to a Python value (number or bool).
- ``evaluate_condition`` — evaluate and coerce to bool (Python truthiness).
- ``resolve_number``     — resolve a field that may be a number or an
                           expression string to a float.
- ``is_valid_variable_name`` — check an assignment target is a legal name.
"""

from __future__ import annotations

import ast
import keyword
import operator
from typing import Mapping

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_CMP_OPS = {
    ast.Lt: operator.lt,
    ast.Gt: operator.gt,
    ast.LtE: operator.le,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}


class ExpressionError(ValueError):
    """Raised when an expression cannot be parsed or contains a disallowed element."""


def evaluate(expr: str, variables: Mapping[str, float]):
    """Evaluate an expression against ``variables`` to a number, bool, or string.

    Raises ExpressionError for invalid syntax, disallowed constructs, a
    reference to an undefined variable, or a type mismatch between operands;
    arithmetic errors (e.g. division by zero) propagate as the usual
    ArithmeticError subclasses.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExpressionError("Invalid expression %r: %s" % (expr, e)) from e
    try:
        return _eval_node(tree.body, variables)
    except TypeError as e:
        # e.g. "a" + 1, or comparing a string with a number.
        raise ExpressionError("Type error in %r: %s" % (expr, e)) from e


def evaluate_condition(expr: str, variables: Mapping[str, float]) -> bool:
    """Evaluate ``expr`` and coerce the result with Python truthiness."""
    return bool(evaluate(expr, variables))


def resolve_number(value, variables: Mapping[str, float]):
    """Resolve a numeric field that is either a number or an expression string.

    Numbers (and None) pass through unchanged; an expression string is evaluated
    and must yield a number — a string result (e.g. a text variable used where a
    coordinate is expected) raises ExpressionError so the caller can report it.
    """
    if value is None or isinstance(value, (int, float)):
        return value
    result = evaluate(str(value), variables)
    if isinstance(result, (int, float)):   # bool is an int subclass → 0/1
        return float(result)
    raise ExpressionError("expected a number but %r evaluated to a %s"
                          % (value, type(result).__name__))


def render_template(template: str, variables: Mapping[str, float]) -> str:
    """Render an f-string-style template against ``variables``.

    Literal text is kept as-is; each ``{ expr }`` segment is evaluated and its
    result interpolated (e.g. ``"x is {x}"`` with ``{"x": 3}`` → ``"x is 3"``).
    Use ``{{`` and ``}}`` for literal braces.  A bad/undefined expression inside
    a brace raises ExpressionError.
    """
    out: list[str] = []
    i, n = 0, len(template)
    while i < n:
        c = template[i]
        if c == "{":
            if i + 1 < n and template[i + 1] == "{":
                out.append("{")
                i += 2
                continue
            close = template.find("}", i + 1)
            if close == -1:
                raise ExpressionError("Unmatched '{' in text template")
            out.append(str(evaluate(template[i + 1:close], variables)))
            i = close + 1
        elif c == "}":
            if i + 1 < n and template[i + 1] == "}":
                out.append("}")
                i += 2
                continue
            raise ExpressionError("Unmatched '}' in text template")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def is_valid_variable_name(name: str) -> bool:
    """True if ``name`` is a legal, non-numeric variable identifier."""
    return bool(name) and name.isidentifier() and not keyword.iskeyword(name)


def _eval_node(node: ast.AST, variables: Mapping[str, float]):
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left, variables),
                                       _eval_node(node.right, variables))
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return -_eval_node(node.operand, variables)
        if isinstance(node.op, ast.UAdd):
            return +_eval_node(node.operand, variables)
        if isinstance(node.op, ast.Not):
            return not _eval_node(node.operand, variables)
        raise ExpressionError("Unsupported unary operator")
    if isinstance(node, ast.BoolOp):
        # Short-circuit with Python semantics, returning the operand value.
        result = _eval_node(node.values[0], variables)
        for operand in node.values[1:]:
            if isinstance(node.op, ast.And):
                if not result:
                    return result
            else:  # Or
                if result:
                    return result
            result = _eval_node(operand, variables)
        return result
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, variables)
        for op, comparator in zip(node.ops, node.comparators):
            if type(op) not in _CMP_OPS:
                raise ExpressionError("Unsupported comparison operator")
            right = _eval_node(comparator, variables)
            if not _CMP_OPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool, str)):
            return node.value
        raise ExpressionError("Unsupported literal: %r" % (node.value,))
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise ExpressionError("Undefined variable %r" % node.id)
        return variables[node.id]
    raise ExpressionError("Unsupported expression element: %s" % type(node).__name__)
