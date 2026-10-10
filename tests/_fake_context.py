"""A fake PlayContext for testing action ``play`` functions without the engine.

The ``ctx`` fixture builds a :class:`FakePlayContext` over the conftest
fakes, drives the find module's poll clock from a FakeClock, and, when
``macro_recorder.engine.expressions`` is not importable (it is written by
another work package), installs a small stand-in evaluator under that name
for the duration of the test.
"""

from __future__ import annotations

import ast
import importlib
import operator
import sys
import types
from collections.abc import MutableMapping
from typing import Any, Iterator, Mapping

import pytest

from macro_recorder.model import catalogue
from macro_recorder.model.actions.variables import DYNAMIC_NAMES
from macro_recorder.model.catalogue import Flow
from macro_recorder.model.document import Row, make_row
from macro_recorder.model.protocols import Rect
from tests.conftest import FakeClock, FakeHost, FakeInput, FakeScreen, FakeWindows


# ---------------------------------------------------------------------------
# Stand-in evaluator
# ---------------------------------------------------------------------------

class StandInExpressionError(ValueError):
    """Raised by the stand-in evaluator."""


_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
_CMP = {ast.Lt: operator.lt, ast.Gt: operator.gt, ast.LtE: operator.le, ast.GtE: operator.ge,
        ast.Eq: operator.eq, ast.NotEq: operator.ne}


def _standin_evaluate(expr: str, variables: Mapping[str, Any]) -> Any:
    """Numbers, strings, names, arithmetic, comparisons, and/or/not."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise StandInExpressionError("invalid expression %r" % expr) from exc

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise StandInExpressionError("unknown variable %r" % node.id)
            return variables[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            return _BIN[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -ev(node.operand)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return int(not ev(node.operand))
        if isinstance(node, ast.BoolOp):
            values = [ev(v) for v in node.values]
            return int(all(values) if isinstance(node.op, ast.And) else any(values))
        if isinstance(node, ast.Compare):
            left = ev(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = ev(comp)
                if not _CMP[type(op)](left, right):
                    return 0
                left = right
            return 1
        raise StandInExpressionError("unsupported expression %r" % expr)

    return ev(tree.body)


def _evaluator_module() -> types.ModuleType | None:
    """The stand-in module when the real evaluator is missing, else None."""
    try:
        importlib.import_module("macro_recorder.engine.expressions")
        return None
    except ImportError:
        module = types.ModuleType("macro_recorder.engine.expressions")
        module.evaluate = _standin_evaluate
        module.ExpressionError = StandInExpressionError
        return module


def evaluate(expr: str, variables: Mapping[str, Any]) -> Any:
    """Whichever evaluator play uses: the real one when present."""
    from macro_recorder.engine.expressions import evaluate as real
    return real(expr, variables)


# ---------------------------------------------------------------------------
# Variables and context
# ---------------------------------------------------------------------------

class FakeVariables(MutableMapping):
    """User variables plus read-only dynamic values; writing a dynamic name raises."""

    def __init__(self, dynamic: Mapping[str, Any] | None = None) -> None:
        self.user: dict[str, Any] = {}
        self.dynamic: dict[str, Any] = {name: 0 for name in DYNAMIC_NAMES}
        self.dynamic.update(dynamic or {})

    def __getitem__(self, key: str) -> Any:
        if key in self.dynamic:
            return self.dynamic[key]
        return self.user[key]

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self.dynamic:
            raise ValueError("%s is read-only" % key)
        self.user[key] = value

    def __delitem__(self, key: str) -> None:
        del self.user[key]

    def __iter__(self) -> Iterator[str]:
        yield from self.dynamic
        yield from self.user

    def __len__(self) -> int:
        return len(self.dynamic) + len(self.user)


class FakePlayContext:
    """Implements the PlayContext Protocol over the conftest fakes.

    ``origin`` is the current frame origin: ``coord`` adds it, ``to_frame``
    subtracts it.  ``stop_after`` makes the n-th ``sleep`` report a stop.
    """

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.row_id = 0
        self.speed = 1.0
        self.variables = FakeVariables()
        self.input = FakeInput()
        self.screen = FakeScreen()
        self.windows = FakeWindows()
        self.host = FakeHost()
        self.settings = None
        self.document_dir: str | None = None
        self.clock = clock or FakeClock()
        self.origin = (0, 0)
        self.logs: list[str] = []
        self.sleeps: list[tuple[float, bool]] = []
        self.stop_after: int | None = None
        self.is_stopped = False
        self.reactivations = 0
        self.entered: list[Row] = []
        self.enter_result: Any = Flow.NEXT
        self.commands: list[tuple[str, bool, float]] = []
        self.exit_code: int | None = 0
        self.launch_error: OSError | None = None
        self.states: dict[int, dict] = {}

    def resolve(self, value: Any, default: float = 0.0) -> float:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        return float(evaluate(str(value), self.variables))

    def resolve_int(self, value: Any, default: int = 0) -> int:
        return int(round(self.resolve(value, default)))

    def coord(self, x: Any, y: Any) -> tuple[float, float]:
        return self.resolve(x) + self.origin[0], self.resolve(y) + self.origin[1]

    def to_frame(self, x: float, y: float) -> tuple[float, float]:
        return x - self.origin[0], y - self.origin[1]

    def region(self, values: Mapping[str, Any]) -> Rect:
        if values.get("monitor") is not None:
            return self.screen.monitors()[int(values["monitor"]) - 1].rect
        x1, y1 = self.coord(values["x"], values["y"])
        x2, y2 = self.coord(values["x2"], values["y2"])
        return Rect(int(min(x1, x2)), int(min(y1, y2)), int(abs(x2 - x1)), int(abs(y2 - y1)))

    def render(self, template: str) -> str:
        out, i = [], 0
        while i < len(template):
            if template.startswith(("{{", "}}"), i):
                out.append(template[i])
                i += 2
            elif template[i] == "{":
                close = template.index("}", i)
                out.append(str(evaluate(template[i + 1:close], self.variables)))
                i = close + 1
            else:
                out.append(template[i])
                i += 1
        return "".join(out)

    def sleep(self, ms: float, scaled: bool = True) -> bool:
        self.sleeps.append((ms, scaled))
        self.clock.advance(ms / 1000.0)
        if self.stop_after is not None and len(self.sleeps) >= self.stop_after:
            self.is_stopped = True
        return self.is_stopped

    def stopped(self) -> bool:
        return self.is_stopped

    def log(self, text: str) -> None:
        self.logs.append(text)

    def hold_key(self, name: str) -> None:
        self.input.press_key(name)

    def unhold_key(self, name: str) -> None:
        self.input.release_key(name)

    def hold_button(self, name: str) -> None:
        self.input.press_button(name)

    def unhold_button(self, name: str) -> None:
        self.input.release_button(name)

    def reactivate_context(self) -> None:
        self.reactivations += 1

    def enter_window(self, row: Row) -> Any:
        self.entered.append(row)
        return self.enter_result

    def move_pointer(self, points, duration_ms, dts=None) -> bool:
        x, y = points[-1]
        self.input.move(x, y)
        return self.is_stopped

    def row_state(self, row_id: int) -> dict:
        return self.states.setdefault(row_id, {})

    def now(self) -> float:
        return self.clock.now()

    def run_command(self, command: str, wait: bool, timeout_ms: float = 0) -> int | None:
        self.commands.append((command, wait, timeout_ms))
        if self.launch_error is not None:
            raise self.launch_error
        return self.exit_code if wait else None


def play(ctx: FakePlayContext, row: Row) -> Any:
    """Run a row's spec play as the engine would."""
    ctx.row_id = row.id
    return catalogue.spec_for(row).play(ctx, row)


def row(type_: str, **params: Any) -> Row:
    return make_row(catalogue.make(type_, **params))


@pytest.fixture
def ctx(monkeypatch) -> FakePlayContext:
    module = _evaluator_module()
    if module is not None:
        monkeypatch.setitem(sys.modules, "macro_recorder.engine.expressions", module)
    return FakePlayContext()
