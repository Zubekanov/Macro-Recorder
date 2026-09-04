"""Canonical macro event-type identifiers.

`EventType` is a ``str``-based ``Enum`` so each member compares equal to its
wire string (``EventType.MOUSE_MOVE == "mouse_move"``) and serializes to that
string in JSON. This gives the recorder, player, data model, and UI a single
source of truth for the event-type vocabulary without changing the on-disk
format or any existing string comparisons.
"""

from __future__ import annotations

from enum import Enum


class EventType(str, Enum):
    """The set of event types a MacroEvent may carry."""

    MOUSE_MOVE = "mouse_move"
    MOUSE_MOVE_TIMED = "mouse_move_timed"
    MOUSE_CLICK = "mouse_click"
    MOUSE_SCROLL = "mouse_scroll"
    KEY_PRESS = "key_press"
    KEY_RELEASE = "key_release"
    TYPE_TEXT = "type_text"
    WAIT = "wait"
    WINDOW_FOCUS = "window_focus"
    VAR_SET = "var_set"
    GOTO = "goto"
    GOTO_IF = "goto_if"
    OCR_READ = "ocr_read"
    MATCH_IMAGE = "match_image"
    MATCH_TEXT = "match_text"

    def __str__(self) -> str:  # keep str(member) == wire string (py3.11+ changed this)
        return self.value


# All valid wire strings, for fast membership validation at deserialization.
EVENT_TYPE_VALUES: frozenset[str] = frozenset(t.value for t in EventType)
