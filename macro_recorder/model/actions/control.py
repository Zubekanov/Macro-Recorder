"""Control actions: goto, if, repeat."""

from __future__ import annotations

from macro_recorder.model.actions._common import label_target, number, text
from macro_recorder.model.actions.variables import evaluate
from macro_recorder.model.catalogue import (
    END_TARGET, Abort, ActionSpec, Flow, Jump, Preset, make, register,
)
from macro_recorder.model.document import Row
from macro_recorder.model.events import EventType
from macro_recorder.model.protocols import PlayContext


def _play_goto(ctx: PlayContext, row: Row) -> Jump | Abort:
    target = row.event["target"]
    if not target:
        return Abort("Go To has no target")
    return Jump(target)


GOTO = register(ActionSpec(
    type=EventType.GOTO.value, name="Go To", category="Control",
    fields=(label_target("target", "Go to", END_TARGET),),
    presets=(Preset("Goto", "G", lambda: make("goto", target=END_TARGET)),),
    describe=lambda row: "goto %s" % row.event["target"],
    play=_play_goto,
))


def _describe_if(row: Row) -> str:
    e = row.event
    out = "if %s goto %s" % (e["expr"], e["target"])
    if e["else_target"]:
        out += " else %s" % e["else_target"]
    return out


def _play_if(ctx: PlayContext, row: Row) -> Flow | Jump:
    """Truth is a non-zero number or a non-empty string; a None target is the next row."""
    e = row.event
    target = e["target"] if evaluate(ctx, e["expr"]) else e["else_target"]
    return Jump(target) if target else Flow.NEXT


IF_ELSE = register(ActionSpec(
    type=EventType.IF_ELSE.value, name="If", category="Control",
    fields=(
        text("expr", "Condition", "x > 0"),
        label_target("target", "Then go to", END_TARGET),
        label_target("else_target", "Else go to", None, none_label="(next row)"),
    ),
    presets=(Preset("If", None, lambda: make("if_else", expr="x > 0", target=END_TARGET)),),
    describe=_describe_if, play=_play_if,
))

def _play_repeat(ctx: PlayContext, row: Row) -> Flow | Jump | Abort:
    """Loop back to the target until this row has been reached ``count`` times.

    Row state: ``counter`` is how many times the row has been reached in the
    current run of the loop, ``count`` the count read when it started, and
    ``pass`` the 1-based pass of the body now running (absent on the first
    pass).  The engine reads ``pass`` for the ``loop`` dynamic value and clears
    the state at each program pass.
    """
    e = row.event
    if not e["target"]:
        return Abort("Repeat has no target")
    state = ctx.row_state(row.id)
    counter = state.get("counter", 0)
    if counter == 0:
        count = ctx.resolve_int(e["count"])
        if count <= 1:
            return Flow.NEXT
        state["count"] = count
    counter += 1
    if counter < state["count"]:
        state["counter"] = counter
        state["pass"] = counter + 1
        return Jump(e["target"])
    state.clear()
    return Flow.NEXT


REPEAT = register(ActionSpec(
    type=EventType.REPEAT.value, name="Repeat", category="Control",
    fields=(
        label_target("target", "Repeat from", None, none_label=None,
                     help="A label at or above this row"),
        number("count", "Times", 3, minimum=0, integer=True),
    ),
    presets=(Preset("Repeat", None, lambda: make("repeat", target=None, count=3)),),
    describe=lambda row: "repeat from %s %s times" % (row.event["target"] or "?", row.event["count"]),
    play=_play_repeat,
))
