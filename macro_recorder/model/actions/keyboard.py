"""Keyboard actions: key combo, key press, key release, type text."""

from __future__ import annotations

from macro_recorder.model.actions._common import boolean, duration, fmt_ms, template
from macro_recorder.model.catalogue import ActionSpec, FieldKind, FieldSpec, Preset, make, register
from macro_recorder.model.document import Row
from macro_recorder.model.events import EventType
from macro_recorder.model.keys import display_key, format_combo, parse_combo


def _describe_combo(row: Row) -> str:
    try:
        return format_combo(parse_combo(row.event["keys"]))
    except ValueError:
        return row.event["keys"]


KEY_COMBO = register(ActionSpec(
    type=EventType.KEY_COMBO.value, name="Key", category="Keyboard", filter_group="keyboard",
    fields=(
        FieldSpec(keys=("keys",), label="Keys", kind=FieldKind.KEY_COMBO, default="a",
                  help="A key or a combination such as ctrl+shift+s"),
        duration("hold_ms", "Hold", 50, allow_expr=False),
    ),
    presets=(Preset("Key", "H", lambda: make("key_combo", keys="a")),),
    describe=_describe_combo,
))


def _describe_key(verb: str):
    return lambda row: "%s %s" % (verb, display_key(row.event["key"]))


KEY_PRESS = register(ActionSpec(
    type=EventType.KEY_PRESS.value, name="Key Down", category="Keyboard", filter_group="keyboard",
    fields=(FieldSpec(keys=("key",), label="Key", kind=FieldKind.KEY, default="Shift_L"),),
    presets=(Preset("Key down", None, lambda: make("key_press", key="Shift_L")),),
    describe=_describe_key("hold"),
))

KEY_RELEASE = register(ActionSpec(
    type=EventType.KEY_RELEASE.value, name="Key Up", category="Keyboard", filter_group="keyboard",
    fields=(FieldSpec(keys=("key",), label="Key", kind=FieldKind.KEY, default="Shift_L"),),
    presets=(Preset("Key up", None, lambda: make("key_release", key="Shift_L")),),
    describe=_describe_key("release"),
))


def _describe_text(row: Row) -> str:
    e = row.event
    text = 'type "%s"' % e["text"]
    if e["char_delay_ms"]:
        text += " (%s per char)" % fmt_ms(e["char_delay_ms"])
    elif e["duration_ms"]:
        text += " (over %s)" % fmt_ms(e["duration_ms"])
    if e["humanise"]:
        text += " humanised"
    return text


TYPE_TEXT = register(ActionSpec(
    type=EventType.TYPE_TEXT.value, name="Type Text", category="Keyboard", filter_group="keyboard",
    fields=(
        template("text", "Text", "text", help="{expr} inserts a value; {{ and }} are literal braces"),
        duration("char_delay_ms", "Delay per character", 0,
                 enabled_when=lambda v: not v.get("duration_ms")),
        duration("duration_ms", "Total duration", 0,
                 enabled_when=lambda v: not v.get("char_delay_ms")),
        boolean("humanise", "Humanise timing", False),
    ),
    presets=(Preset("Text", "T", lambda: make("type_text", text="text")),),
    describe=_describe_text,
))
