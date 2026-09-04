"""Unit tests for the safe expression evaluator."""

import unittest

from src.expressions import (
    ExpressionError,
    evaluate,
    evaluate_condition,
    is_valid_variable_name,
    render_template,
    resolve_number,
)


class TestEvaluate(unittest.TestCase):
    def test_literal(self):
        self.assertEqual(evaluate("42", {}), 42)

    def test_variable_plus_constant(self):
        self.assertEqual(evaluate("varname + 3", {"varname": 5}), 8)

    def test_two_variables(self):
        self.assertEqual(evaluate("var1 * var2", {"var1": 3, "var2": 4}), 12)

    def test_undefined_variable_raises(self):
        with self.assertRaises(ExpressionError):
            evaluate("y + 1", {})

    def test_operator_precedence(self):
        self.assertEqual(evaluate("2 + 3 * 4", {}), 14)

    def test_parentheses(self):
        self.assertEqual(evaluate("(2 + 3) * 4", {}), 20)

    def test_unary_minus(self):
        self.assertEqual(evaluate("-x", {"x": 5}), -5)

    def test_all_operators(self):
        v = {"a": 10, "b": 3}
        self.assertEqual(evaluate("a - b", v), 7)
        self.assertAlmostEqual(evaluate("a / b", v), 10 / 3)
        self.assertEqual(evaluate("a // b", v), 3)
        self.assertEqual(evaluate("a % b", v), 1)
        self.assertEqual(evaluate("b ** 2", v), 9)

    def test_self_reference_increment(self):
        # The "modify variable" use case: varname = varname + 3
        self.assertEqual(evaluate("varname + 3", {"varname": 10}), 13)

    def test_rejects_function_call(self):
        with self.assertRaises(ExpressionError):
            evaluate("foo()", {})

    def test_rejects_attribute_access(self):
        with self.assertRaises(ExpressionError):
            evaluate("x.attr", {"x": 1})

    def test_rejects_invalid_syntax(self):
        with self.assertRaises(ExpressionError):
            evaluate("3 +", {})

    def test_division_by_zero_propagates(self):
        with self.assertRaises(ZeroDivisionError):
            evaluate("1 / x", {"x": 0})


class TestConditions(unittest.TestCase):
    def test_comparisons(self):
        self.assertTrue(evaluate_condition("x > 3", {"x": 5}))
        self.assertFalse(evaluate_condition("x > 3", {"x": 1}))
        self.assertTrue(evaluate_condition("x == 0", {"x": 0}))

    def test_chained_comparison(self):
        self.assertTrue(evaluate_condition("0 < x < 10", {"x": 5}))
        self.assertFalse(evaluate_condition("0 < x < 10", {"x": 20}))

    def test_boolean_ops(self):
        self.assertTrue(evaluate_condition("x > 0 and y > 0", {"x": 1, "y": 2}))
        self.assertFalse(evaluate_condition("x > 0 and y > 0", {"x": 1, "y": 0}))
        self.assertTrue(evaluate_condition("x > 0 or y > 0", {"x": 0, "y": 2}))
        self.assertTrue(evaluate_condition("not x", {"x": 0}))

    def test_truthiness_of_bare_number(self):
        self.assertTrue(evaluate_condition("x", {"x": 5}))
        self.assertFalse(evaluate_condition("x", {"x": 0}))

    def test_boolean_literal_allowed(self):
        self.assertTrue(evaluate_condition("True", {}))
        self.assertFalse(evaluate_condition("False", {}))


class TestResolveNumber(unittest.TestCase):
    def test_passthrough_number(self):
        self.assertEqual(resolve_number(100, {}), 100)
        self.assertEqual(resolve_number(1.5, {}), 1.5)

    def test_none_passthrough(self):
        self.assertIsNone(resolve_number(None, {}))

    def test_expression_string(self):
        self.assertEqual(resolve_number("x + 5", {"x": 10}), 15.0)


class TestVariableName(unittest.TestCase):
    def test_valid_names(self):
        for n in ("x", "var1", "_tmp", "counter"):
            self.assertTrue(is_valid_variable_name(n), n)

    def test_invalid_names(self):
        for n in ("", "1x", "3", "a b", "x+1", "for"):
            self.assertFalse(is_valid_variable_name(n), n)


class TestStrings(unittest.TestCase):
    def test_string_literal(self):
        self.assertEqual(evaluate('"hi"', {}), "hi")

    def test_concatenation(self):
        self.assertEqual(evaluate("a + b", {"a": "foo", "b": "bar"}), "foobar")

    def test_string_comparison(self):
        self.assertTrue(evaluate_condition('name == "x"', {"name": "x"}))
        self.assertFalse(evaluate_condition('name == "x"', {"name": "y"}))

    def test_type_mismatch_raises(self):
        with self.assertRaises(ExpressionError):
            evaluate('"a" + 1', {})

    def test_resolve_number_rejects_string(self):
        with self.assertRaises(ExpressionError):
            resolve_number("name", {"name": "hello"})


class TestRenderTemplate(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(render_template("hello world", {}), "hello world")

    def test_interpolation(self):
        self.assertEqual(render_template("x is {x}", {"x": 3}), "x is 3")

    def test_expression_in_braces(self):
        self.assertEqual(render_template("{x + 1}", {"x": 4}), "5")

    def test_string_variable(self):
        self.assertEqual(render_template("hi {name}!", {"name": "Sam"}), "hi Sam!")

    def test_escaped_braces(self):
        self.assertEqual(render_template("{{literal}}", {}), "{literal}")

    def test_unmatched_brace_raises(self):
        with self.assertRaises(ExpressionError):
            render_template("oops {x", {"x": 1})

    def test_undefined_in_template_raises(self):
        with self.assertRaises(ExpressionError):
            render_template("{missing}", {})


if __name__ == "__main__":
    unittest.main()
