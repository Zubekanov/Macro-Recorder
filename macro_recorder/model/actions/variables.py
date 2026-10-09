"""Variable actions: set variable, read screen text."""

from __future__ import annotations

from macro_recorder.model.actions._common import region, text, var_name
from macro_recorder.model.catalogue import ActionSpec, Preset, make, register
from macro_recorder.model.document import Row
from macro_recorder.model.events import EventType
from macro_recorder.model.protocols import OverlayShape

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


VAR_SET = register(ActionSpec(
    type=EventType.VAR_SET.value, name="Set Variable", category="Variable",
    fields=(
        var_name("var_name", "Variable", "x"),
        text("expr", "Expression", "0", help=DYNAMIC_HELP),
    ),
    presets=(Preset("Set variable", None, lambda: make("var_set", var_name="x", expr="0")),),
    describe=lambda row: "%s = %s" % (row.event["var_name"], row.event["expr"]),
))

OCR_READ = register(ActionSpec(
    type=EventType.OCR_READ.value, name="Read Screen Text", category="Variable",
    fields=(var_name("var_name", "Variable", "text"), region()),
    presets=(Preset("Read screen text (OCR)", None,
                    lambda: make("ocr_read", var_name="text", x=100, y=100, x2=300, y2=200)),),
    describe=lambda row: "%s = text in %s" % (row.event["var_name"], region_text(row)),
    overlay=region_overlay,
))
