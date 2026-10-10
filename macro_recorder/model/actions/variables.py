"""Variable actions: set variable, read screen text."""

from __future__ import annotations

import keyword
from typing import Any

from macro_recorder.model.actions._common import region, text, var_name
from macro_recorder.model.catalogue import Abort, ActionSpec, Flow, Preset, make, register
from macro_recorder.model.document import Row
from macro_recorder.model.events import EventType
from macro_recorder.model.protocols import OverlayShape, PlayContext

DYNAMIC_VALUES: tuple[tuple[str, str], ...] = (
    ("rt_ms", "ms since playback started, excluding pauses"),
    ("op_num", "current row number"),
    ("ex_num", "rows executed so far"),
    ("iteration", "current repeat pass (1-based)"),
    ("loop", "pass of the innermost Repeat"),
    ("mouse_x", "pointer X in the current frame"),
    ("mouse_y", "pointer Y in the current frame"),
    ("win_x", "current window client left"),
    ("win_y", "current window client top"),
    ("win_w", "current window client width"),
    ("win_h", "current window client height"),
    ("timestamp_ms", "wall-clock Unix epoch ms"),
    ("random", "new random float in [0, 1) per read"),
)

DYNAMIC_HELP = "Dynamic values: " + ", ".join(name for name, _ in DYNAMIC_VALUES)

DYNAMIC_NAMES: frozenset[str] = frozenset(name for name, _ in DYNAMIC_VALUES)


def is_valid_variable_name(name: Any) -> bool:
    """True for a legal identifier that is not a Python keyword."""
    return isinstance(name, str) and name.isidentifier() and not keyword.iskeyword(name)


def evaluate(ctx: PlayContext, expr: str) -> Any:
    """Evaluate an expression against the playback variables.

    The result may be a number or a string.  Errors propagate as the
    evaluator's ExpressionError, which the engine reports as "Row N: message".
    """
    from macro_recorder.engine.expressions import evaluate as evaluate_expr
    return evaluate_expr(expr, ctx.variables)


def name_problem(name: Any) -> Abort | None:
    """An Abort when ``name`` cannot be assigned, else None."""
    if not is_valid_variable_name(name):
        return Abort('"%s" is not a valid variable name' % name)
    if name in DYNAMIC_NAMES:
        return Abort('"%s" is a dynamic value and cannot be assigned' % name)
    return None


def assign(ctx: PlayContext, name: str, value: Any) -> Abort | None:
    """Store ``value`` in variable ``name``; an Abort when the name cannot be assigned."""
    problem = name_problem(name)
    if problem is None:
        ctx.variables[name] = value
    return problem


def region_overlay(row: Row) -> OverlayShape | None:
    e = row.event
    if e["monitor"] is not None:
        return None
    vals = (e["x"], e["y"], e["x2"], e["y2"])
    if not all(isinstance(v, (int, float)) for v in vals):
        return None
    return OverlayShape("rect", ((float(vals[0]), float(vals[1])), (float(vals[2]), float(vals[3]))))


def region_text(row: Row) -> str:
    e = row.event
    if e["monitor"] is not None:
        return "monitor %s" % e["monitor"]
    return "(%s, %s)-(%s, %s)" % (e["x"], e["y"], e["x2"], e["y2"])


def _play_var_set(ctx: PlayContext, row: Row) -> Flow | Abort:
    e = row.event
    problem = name_problem(e["var_name"])
    if problem is not None:
        return problem
    ctx.variables[e["var_name"]] = evaluate(ctx, e["expr"])
    return Flow.NEXT


def _play_ocr_read(ctx: PlayContext, row: Row) -> Flow | Abort:
    e = row.event
    problem = name_problem(e["var_name"])
    if problem is not None:
        return problem
    ctx.variables[e["var_name"]] = ctx.screen.ocr(ctx.region(e.params)).strip()
    return Flow.NEXT


VAR_SET = register(ActionSpec(
    type=EventType.VAR_SET.value, name="Set Variable", category="Variable",
    fields=(
        var_name("var_name", "Variable", "x"),
        text("expr", "Expression", "0", help=DYNAMIC_HELP),
    ),
    presets=(Preset("Set variable", None, lambda: make("var_set", var_name="x", expr="0")),),
    describe=lambda row: "%s = %s" % (row.event["var_name"], row.event["expr"]),
    play=_play_var_set,
))

OCR_READ = register(ActionSpec(
    type=EventType.OCR_READ.value, name="Read Screen Text", category="Variable",
    fields=(var_name("var_name", "Variable", "text"), region()),
    presets=(Preset("Read screen text (OCR)", None,
                    lambda: make("ocr_read", var_name="text", x=100, y=100, x2=300, y2=200)),),
    describe=lambda row: "%s = text in %s" % (row.event["var_name"], region_text(row)),
    overlay=region_overlay, play=_play_ocr_read,
))
