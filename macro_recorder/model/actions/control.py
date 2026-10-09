"""Control actions: goto, if, repeat."""

from __future__ import annotations

from macro_recorder.model.actions._common import label_target, number, text
from macro_recorder.model.catalogue import END_TARGET, ActionSpec, Preset, make, register
from macro_recorder.model.document import Row
from macro_recorder.model.events import EventType

GOTO = register(ActionSpec(
    type=EventType.GOTO.value, name="Go To", category="Control",
    fields=(label_target("target", "Go to", END_TARGET),),
    presets=(Preset("Goto", "G", lambda: make("goto", target=END_TARGET)),),
    describe=lambda row: "goto %s" % row.event["target"],
))


def _describe_if(row: Row) -> str:
    e = row.event
    out = "if %s goto %s" % (e["expr"], e["target"])
    if e["else_target"]:
        out += " else %s" % e["else_target"]
    return out


IF_ELSE = register(ActionSpec(
    type=EventType.IF_ELSE.value, name="If", category="Control",
    fields=(
        text("expr", "Condition", "x > 0"),
        label_target("target", "Then go to", END_TARGET),
        label_target("else_target", "Else go to", None, none_label="(next row)"),
    ),
    presets=(Preset("If", None, lambda: make("if_else", expr="x > 0", target=END_TARGET)),),
    describe=_describe_if,
))

REPEAT = register(ActionSpec(
    type=EventType.REPEAT.value, name="Repeat", category="Control",
    fields=(
        label_target("target", "Repeat from", None, none_label=None,
                     help="A label at or above this row"),
        number("count", "Times", 3, minimum=0, integer=True),
    ),
    presets=(Preset("Repeat", None, lambda: make("repeat", target=None, count=3)),),
    describe=lambda row: "repeat from %s %s times" % (row.event["target"] or "?", row.event["count"]),
))
