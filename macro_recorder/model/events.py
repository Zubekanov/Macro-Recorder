"""Event types and the immutable MacroEvent."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class EventType(str, Enum):
    """Every action type the catalogue knows.  Values are the wire strings."""

    MOUSE_MOVE = "mouse_move"
    MOUSE_MOVE_TIMED = "mouse_move_timed"
    MOUSE_CLICK = "mouse_click"
    MOUSE_SCROLL = "mouse_scroll"
    KEY_COMBO = "key_combo"
    KEY_PRESS = "key_press"
    KEY_RELEASE = "key_release"
    TYPE_TEXT = "type_text"
    WAIT = "wait"
    WAIT_PIXEL = "wait_pixel"
    WINDOW_FOCUS = "window_focus"
    VAR_SET = "var_set"
    OCR_READ = "ocr_read"
    GOTO = "goto"
    IF_ELSE = "if_else"
    REPEAT = "repeat"
    MATCH_IMAGE = "match_image"
    MATCH_TEXT = "match_text"
    EXEC_PROGRAM = "exec_program"
    MESSAGE_BOX = "message_box"
    NOTIFY = "notify"
    BEEP = "beep"

    def __str__(self) -> str:
        return self.value


EVENT_TYPE_VALUES: frozenset[str] = frozenset(t.value for t in EventType)


@dataclass(frozen=True)
class MacroEvent:
    """One action.

    ``params`` holds exactly the keys of the type's FieldSpecs.  It is a plain
    dict for speed but is never mutated: build events through
    ``catalogue.make`` and derive new ones with :meth:`with_params`.
    ``dt`` is the delay in seconds after the previous event of the same row
    and is always 0 for a row's first event.
    """

    type: str
    params: dict = field(default_factory=dict)
    dt: float = 0.0

    def __getitem__(self, key: str) -> Any:
        return self.params[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def with_params(self, **changes: Any) -> "MacroEvent":
        """Return a copy with ``changes`` applied to the params."""
        new = dict(self.params)
        new.update(changes)
        return MacroEvent(self.type, new, self.dt)

    def with_dt(self, dt: float) -> "MacroEvent":
        return MacroEvent(self.type, self.params, dt)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MacroEvent):
            return NotImplemented
        return self.type == other.type and self.dt == other.dt and self.params == other.params

    def __hash__(self) -> int:
        return hash((self.type, self.dt, tuple(sorted(self.params.items(), key=lambda kv: kv[0]))))


def freeze_value(value: Any) -> Any:
    """Turn lists into tuples recursively so params hold only immutable values."""
    if isinstance(value, list):
        return tuple(freeze_value(v) for v in value)
    if isinstance(value, tuple):
        return tuple(freeze_value(v) for v in value)
    if isinstance(value, Mapping):
        return tuple((k, freeze_value(v)) for k, v in value.items())
    return value
