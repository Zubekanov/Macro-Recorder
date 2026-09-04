"""Data model, serialization helpers, and file I/O for macro recordings."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button

from macro_recorder.event_types import EVENT_TYPE_VALUES
from macro_recorder.window_manager import MATCH_SUBSTRING


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MacroEvent:
    """A single recorded input event.

    Fields
    ------
    type:    Event category — one of mouse_move, mouse_click, mouse_scroll,
             key_press, key_release.
    ts:      Seconds elapsed since the start of the recording.
    x, y:    Cursor position (mouse events).
    button:  Serialized mouse button name ('left', 'right', 'middle').
    pressed: True = button/key down, False = up (mouse_click only).
    dx, dy:  Scroll delta (mouse_scroll only).
    key:     Serialized key string (key events only).
    """

    type: str
    ts: float
    x: Optional[int] = None
    y: Optional[int] = None
    button: Optional[str] = None
    pressed: Optional[bool] = None
    dx: Optional[int] = None
    dy: Optional[int] = None
    key: Optional[str] = None
    label: Optional[str] = None
    duration: Optional[float] = None   # seconds; used by wait events
    window: Optional[str] = None       # window title; used by window_focus events
    rect: Optional[list[int]] = None   # [left, top, width, height]; window_focus only
    var_name: Optional[str] = None     # target variable name; used by var_set events
    expr: Optional[str] = None         # expression; used by var_set/goto_if/type_text
    target: Optional[str] = None       # jump target label; used by goto/goto_if events
    target_index: Optional[int] = None  # jump target instruction number (1-based);
                                        # XOR with target — when set, goto jumps by
                                        # instruction number instead of by label
    char_delay: Optional[float] = None  # seconds between chars; type_text only
    image_path: Optional[str] = None   # reference image; match_image only
    tolerance: Optional[float] = None  # 0..1 confidence (image) / similarity (text)
    monitor: Optional[int] = None      # 1-based monitor index; region source for match/ocr
    capture_var: Optional[str] = None  # variable for the captured value (text / match x)
    capture_var_y: Optional[str] = None  # variable for the match centre y (match_image)
    match_mode: Optional[str] = None   # window_focus rows: how the title is matched
    launch: Optional[str] = None       # window_focus rows: command run if window missing

    # Transient: the 1-based instruction (row) number this event begins, set when
    # building playback groups so the control-flow engine can resolve goto-by-
    # instruction-number jumps.  Never serialized and excluded from equality.
    instr: Optional[int] = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return a compact dict with only non-None fields set."""
        d: dict[str, Any] = {"type": self.type, "ts": self.ts}
        for name in ("x", "y", "button", "pressed", "dx", "dy", "key", "label",
                     "duration", "window", "rect", "var_name", "expr", "target",
                     "target_index", "char_delay", "image_path", "tolerance",
                     "monitor", "capture_var", "capture_var_y", "match_mode", "launch"):
            value = getattr(self, name)
            if value is not None:
                d[name] = value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MacroEvent:
        """Construct a MacroEvent from a JSON-parsed dict.

        Raises ValueError if the event type is not a recognised one.
        """
        event_type = d["type"]
        if event_type not in EVENT_TYPE_VALUES:
            raise ValueError("Unknown event type: %r" % event_type)
        return cls(
            type=event_type,
            ts=d["ts"],
            x=d.get("x"),
            y=d.get("y"),
            button=d.get("button"),
            pressed=d.get("pressed"),
            dx=d.get("dx"),
            dy=d.get("dy"),
            key=d.get("key"),
            label=d.get("label"),
            duration=d.get("duration"),
            window=d.get("window"),
            rect=d.get("rect"),
            var_name=d.get("var_name"),
            expr=d.get("expr"),
            target=d.get("target"),
            target_index=d.get("target_index"),
            char_delay=d.get("char_delay"),
            image_path=d.get("image_path"),
            tolerance=d.get("tolerance"),
            monitor=d.get("monitor"),
            capture_var=d.get("capture_var"),
            capture_var_y=d.get("capture_var_y"),
            match_mode=d.get("match_mode"),
            launch=d.get("launch"),
        )


# Event types whose dx/dy hold a second absolute coordinate (a destination or
# a region corner) rather than a delta, so they shift with the window origin.
_CORNER_TYPES = ("mouse_move_timed", "ocr_read", "match_image", "match_text")


def offset_event(ev: MacroEvent, dx: int, dy: int) -> MacroEvent:
    """Return ``ev`` with its numeric screen coordinates shifted by (dx, dy).

    Shifts x/y, and dx/dy too for the types where those are coordinates rather
    than deltas.  Expression-valued (string) coordinates are left untouched;
    they only get a value at playback.  Returns ``ev`` itself when nothing
    needs shifting.
    """
    changes = {}
    if isinstance(ev.x, (int, float)):
        changes["x"] = ev.x + dx
    if isinstance(ev.y, (int, float)):
        changes["y"] = ev.y + dy
    if ev.type in _CORNER_TYPES:
        if isinstance(ev.dx, (int, float)):
            changes["dx"] = ev.dx + dx
        if isinstance(ev.dy, (int, float)):
            changes["dy"] = ev.dy + dy
    return replace(ev, **changes) if changes else ev


# ---------------------------------------------------------------------------
# Window grouping model
# ---------------------------------------------------------------------------

@dataclass
class WindowRect:
    """A window's screen rectangle, captured at recording time."""
    left: int
    top: int
    width: int
    height: int

    def to_dict(self) -> dict[str, int]:
        return {"left": self.left, "top": self.top,
                "width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, d: dict[str, int]) -> "WindowRect":
        return cls(left=d["left"], top=d["top"],
                   width=d["width"], height=d["height"])


@dataclass
class MacroGroup:
    """A contiguous run of events that occurred under one window context.

    window:        Title of the active window, or None for the leading group
                   recorded before any focus change (absolute coordinates).
    recorded_rect: The window's screen rectangle when recording began for this
                   group.  Mouse x/y in `events` are stored RELATIVE to
                   recorded_rect.left/top when window is set; absolute otherwise.
    events:        The events belonging to this group, in chronological order.
    match_mode:    How `window` is matched against open windows at playback:
                   "substring" (case-insensitive containment) or "regex".
    launch:        Optional command run once if the window is not found within
                   the timeout; the player then waits for the window again.
    """
    window: Optional[str]
    recorded_rect: Optional[WindowRect]
    events: list[MacroEvent]
    match_mode: str = MATCH_SUBSTRING
    launch: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "window": self.window,
            "recorded_rect": self.recorded_rect.to_dict() if self.recorded_rect else None,
            "events": [e.to_dict() for e in self.events],
        }
        if self.match_mode != MATCH_SUBSTRING:
            d["match_mode"] = self.match_mode
        if self.launch:
            d["launch"] = self.launch
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MacroGroup":
        rect_d = d.get("recorded_rect")
        return cls(
            window=d.get("window"),
            recorded_rect=WindowRect.from_dict(rect_d) if rect_d else None,
            events=[MacroEvent.from_dict(e) for e in d.get("events", [])],
            match_mode=d.get("match_mode") or MATCH_SUBSTRING,
            launch=d.get("launch") or None,
        )


# ---------------------------------------------------------------------------
# Key serialization
# ---------------------------------------------------------------------------

def serialize_key(key: Key | KeyCode) -> str:
    """Convert a pynput key object to a canonical string.

    Special keys  → 'Key.shift', 'Key.f6', 'Key.esc', …
    Printable chars → 'a', 'A', '1', …
    Unknown vk     → 'KeyCode(65)', …
    """
    if isinstance(key, Key):
        return "Key." + key.name
    if key.char is not None:
        return key.char
    return "KeyCode(%d)" % key.vk


def deserialize_key(s: str) -> Key | KeyCode:
    """Convert a serialized key string back to a pynput key object.

    Raises ValueError if the Key name is not recognised.
    """
    if s.startswith("Key."):
        attr = s[4:]
        try:
            return Key[attr]
        except KeyError:
            raise ValueError("Unknown pynput Key name: %r" % attr)
    if s.startswith("KeyCode(") and s.endswith(")"):
        vk = int(s[8:-1])
        return KeyCode(vk=vk)
    return KeyCode.from_char(s)


# ---------------------------------------------------------------------------
# Button serialization
# ---------------------------------------------------------------------------

def serialize_button(button: Button) -> str:
    """Return the button name: 'left', 'right', or 'middle'."""
    return button.name


def deserialize_button(s: str) -> Button:
    """Convert 'left', 'right', or 'middle' to a pynput Button member.

    Raises ValueError for unrecognised names.
    """
    try:
        return Button[s]
    except KeyError:
        raise ValueError("Unknown mouse button: %r" % s)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def save_macro(groups: list[MacroGroup], path: str | Path) -> None:
    """Serialize a list of MacroGroups to a human-readable version-2 JSON file.

    The file is written with 2-space indentation so it can be hand-edited.
    """
    path = Path(path)
    payload = {
        "version": 2,
        "groups": [g.to_dict() for g in groups],
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def load_macro(path: str | Path) -> list[MacroGroup]:
    """Load a macro from a JSON file, returning a list of MacroGroups.

    Supports both the current version-2 grouped format and the legacy flat
    list format (a bare JSON array of events), which is wrapped in a single
    null-window group with absolute coordinates.

    Raises FileNotFoundError with a clear message if the file is missing.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError("Macro file not found: %s" % path)
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, list):
        # Legacy flat format: bare array of event dicts.
        events = [MacroEvent.from_dict(d) for d in raw]
        return [MacroGroup(window=None, recorded_rect=None, events=events)]

    # Version-2 grouped format.
    return [MacroGroup.from_dict(g) for g in raw.get("groups", [])]
