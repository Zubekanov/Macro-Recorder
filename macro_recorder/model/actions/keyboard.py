"""Keyboard actions: key combo, key press, key release, type text.

Presses go through the context's hold bookkeeping so a stop or a pause never
leaves a key down.  Delays go through ``ctx.sleep`` so they scale with the
playback speed.
"""

from __future__ import annotations

import random

from macro_recorder.model.actions._common import boolean, duration, fmt_ms, template
from macro_recorder.model.catalogue import (
    Abort, ActionSpec, FieldKind, FieldSpec, Flow, Preset, make, register,
)
from macro_recorder.model.document import Row
from macro_recorder.model.events import EventType
from macro_recorder.model.keys import display_key, format_combo, normalise_key, parse_combo
from macro_recorder.model.protocols import PlayContext

# Source of the humanise jitter; tests replace it with a seeded or stub generator.
rng: random.Random = random.Random()

# Humanised delays vary uniformly by this fraction either way.
HUMANISE_JITTER = 0.5


def _describe_combo(row: Row) -> str:
    try:
        return format_combo(parse_combo(row.event["keys"]))
    except ValueError:
        return row.event["keys"]


def _play_combo(ctx: PlayContext, row: Row) -> Flow | Abort:
    """Press the modifiers then the key, hold, and release in reverse order."""
    try:
        names = parse_combo(row.event["keys"])
    except ValueError as e:
        return Abort(str(e))
    hold_ms = max(0.0, ctx.resolve(row.event["hold_ms"]))
    for name in names:
        ctx.hold_key(name)
    stopped = hold_ms > 0 and ctx.sleep(hold_ms)
    for name in reversed(names):
        ctx.unhold_key(name)
    return Flow.STOP if stopped else Flow.NEXT


KEY_COMBO = register(ActionSpec(
    type=EventType.KEY_COMBO.value, name="Key", category="Keyboard", filter_group="keyboard",
    fields=(
        FieldSpec(keys=("keys",), label="Keys", kind=FieldKind.KEY_COMBO, default="a",
                  help="A key or a combination such as ctrl+shift+s"),
        duration("hold_ms", "Hold", 50, allow_expr=False),
    ),
    presets=(Preset("Key", "H", lambda: make("key_combo", keys="a")),),
    describe=_describe_combo, play=_play_combo,
))


def _describe_key(verb: str):
    return lambda row: "%s %s" % (verb, display_key(row.event["key"]))


def _key_name(row: Row) -> str | None:
    try:
        return normalise_key(row.event["key"])
    except ValueError:
        return None


def _play_press(ctx: PlayContext, row: Row) -> Flow | Abort:
    """Press the key and leave it held until a key_release or the end of playback."""
    name = _key_name(row)
    if name is None:
        return Abort("No key to press")
    ctx.hold_key(name)
    return Flow.NEXT


def _play_release(ctx: PlayContext, row: Row) -> Flow | Abort:
    """Release a key held by an earlier key_press."""
    name = _key_name(row)
    if name is None:
        return Abort("No key to release")
    ctx.unhold_key(name)
    return Flow.NEXT


KEY_PRESS = register(ActionSpec(
    type=EventType.KEY_PRESS.value, name="Key Down", category="Keyboard", filter_group="keyboard",
    fields=(FieldSpec(keys=("key",), label="Key", kind=FieldKind.KEY, default="Shift_L"),),
    presets=(Preset("Key down", None, lambda: make("key_press", key="Shift_L")),),
    describe=_describe_key("hold"), play=_play_press,
))

KEY_RELEASE = register(ActionSpec(
    type=EventType.KEY_RELEASE.value, name="Key Up", category="Keyboard", filter_group="keyboard",
    fields=(FieldSpec(keys=("key",), label="Key", kind=FieldKind.KEY, default="Shift_L"),),
    presets=(Preset("Key up", None, lambda: make("key_release", key="Shift_L")),),
    describe=_describe_key("release"), play=_play_release,
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


def char_delays(count: int, char_delay_ms: float, duration_ms: float, humanise: bool,
                source: random.Random) -> list[float]:
    """Milliseconds to wait after each of ``count`` characters, before speed scaling.

    ``char_delay_ms`` wins when set; otherwise ``duration_ms`` is spread
    evenly.  Humanising multiplies each delay by a factor drawn uniformly
    from 1 - HUMANISE_JITTER to 1 + HUMANISE_JITTER.
    """
    if count <= 0:
        return []
    base = char_delay_ms if char_delay_ms > 0 else (duration_ms / count if duration_ms > 0 else 0.0)
    if base <= 0:
        return [0.0] * count
    if not humanise:
        return [base] * count
    return [base * source.uniform(1 - HUMANISE_JITTER, 1 + HUMANISE_JITTER) for _ in range(count)]


def _play_text(ctx: PlayContext, row: Row) -> Flow:
    """Render the template and type it one character at a time."""
    e = row.event
    text = ctx.render(e["text"])
    delays = char_delays(len(text), max(0.0, ctx.resolve(e["char_delay_ms"])),
                         max(0.0, ctx.resolve(e["duration_ms"])), bool(e["humanise"]), rng)
    for char, delay in zip(text, delays):
        if ctx.stopped():
            return Flow.STOP
        ctx.input.type_char(char)
        if delay > 0 and ctx.sleep(delay):
            return Flow.STOP
    return Flow.NEXT


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
    describe=_describe_text, play=_play_text,
))
