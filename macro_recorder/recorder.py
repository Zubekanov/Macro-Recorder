"""System-wide input recorder using pynput listeners."""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from pynput import keyboard, mouse

from macro_recorder.event_types import EventType
from macro_recorder.macro import (
    MacroEvent,
    MacroGroup,
    WindowRect,
    deserialize_key,
    offset_event,
    serialize_button,
    serialize_key,
)
from macro_recorder.window_manager import WindowInfo, get_window_manager

log = logging.getLogger(__name__)

# A drag whose moves span less than this is still given a short travel time so
# playback does not fall back to the timed-move default.
_MIN_DRAG_DURATION_S = 0.05


def collapse_drags(events: list[MacroEvent]) -> list[MacroEvent]:
    """Replace each recorded drag with press, one timed move, release.

    A drag is a button press, one or more mouse moves with nothing else in
    between, then the release of the same button somewhere else.  The moves
    become a single ``mouse_move_timed`` from the press point to the release
    point whose duration is the span of the recorded movement.  Everything
    else, including a press and release at the same spot, passes through
    unchanged.  Input is not mutated.
    """
    out: list[MacroEvent] = []
    i, n = 0, len(events)
    while i < n:
        press = events[i]
        if press.type == EventType.MOUSE_CLICK and press.pressed:
            j = i + 1
            while j < n and events[j].type == EventType.MOUSE_MOVE:
                j += 1
            release = events[j] if j < n else None
            is_drag = (
                j > i + 1
                and release is not None
                and release.type == EventType.MOUSE_CLICK
                and not release.pressed
                and release.button == press.button
                and (release.x, release.y) != (press.x, press.y)
            )
            if is_drag:
                first_move, last_move = events[i + 1], events[j - 1]
                out.append(press)
                out.append(MacroEvent(
                    type=EventType.MOUSE_MOVE_TIMED,
                    ts=first_move.ts,
                    x=press.x, y=press.y,
                    dx=release.x, dy=release.y,
                    duration=max(last_move.ts - first_move.ts, _MIN_DRAG_DURATION_S),
                ))
                out.append(release)
                i = j + 1
                continue
        out.append(press)
        i += 1
    return out


class Recorder:
    """Records mouse, keyboard, and window-focus events system-wide.

    Usage
    -----
    recorder = Recorder(stop_key="Key.f6")
    events = recorder.start()   # blocks until stop key is pressed
    save_macro(events, "out.json")

    Notes
    -----
    - Consecutive identical mouse positions are deduplicated.
    - The stop key itself is never included in the recorded events.
    - Thread-safe: callbacks run in separate daemon threads; shared state
      is protected by a threading.Lock.
    - Window focus changes come from the platform window manager's
      ``watch_foreground`` (a WinEvent hook on Windows, _NET_ACTIVE_WINDOW on
      X11).  Where no window manager backend exists the feature no-ops.
    - On Windows, recording keystrokes from elevated (UAC) windows
      requires running Python as Administrator.
    - On Linux the listeners need an X11 session (XRecord); Wayland sessions
      do not deliver global input events.
    """

    def __init__(self, stop_key: str = "Key.f6") -> None:
        self._stop_key_obj = deserialize_key(stop_key)
        self._stop_key_str = stop_key
        self._events: list[MacroEvent] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._start_time: float = 0.0
        self._last_mouse_pos: Optional[tuple[int, int]] = None
        self._last_window_title: str = ""
        self._wm = get_window_manager()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Signal the recorder to stop if it is currently running."""
        self._stop_event.set()

    def get_events(self) -> list[MacroEvent]:
        """Return a thread-safe snapshot of events recorded so far."""
        with self._lock:
            return list(self._events)

    def start(self) -> list[MacroGroup]:
        """Start recording.  Blocks until the stop key is pressed.

        Returns the recorded events grouped by window context.  All timestamps
        are seconds elapsed since this call was made.
        """
        self._events = []
        self._stop_event.clear()
        self._last_mouse_pos = None
        foreground = self._wm.get_foreground()
        self._last_window_title = foreground.title if foreground else ""
        self._start_time = time.perf_counter()

        # Window-focus monitor: the window manager backend reports changes
        # until the stop event is set.  Runs on its own thread because the
        # Win32 hook and the X11 watcher both block in their own event loops.
        focus_thread = threading.Thread(target=self._run_window_monitor, daemon=True)
        focus_thread.start()

        mouse_listener = mouse.Listener(
            on_move=self._on_mouse_move,
            on_click=self._on_mouse_click,
            on_scroll=self._on_mouse_scroll,
        )
        kb_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )

        mouse_listener.start()
        kb_listener.start()

        self._stop_event.wait()  # blocks until stop key or Recorder.stop()

        focus_thread.join(timeout=2.0)   # the watcher polls the stop event

        mouse_listener.stop()
        kb_listener.stop()
        mouse_listener.join()
        kb_listener.join()

        return self._build_groups(collapse_drags(list(self._events)))

    # ------------------------------------------------------------------
    # Window focus monitoring
    # ------------------------------------------------------------------

    def _run_window_monitor(self) -> None:
        """Block on the window manager's foreground watcher until stopped."""
        try:
            self._wm.watch_foreground(self._on_foreground_change, self._stop_event)
        except Exception as e:
            log.debug("Window focus monitor exited on error: %s", e)

    def _on_foreground_change(self, info: WindowInfo) -> None:
        """Record a window_focus marker when a different titled window activates."""
        if not info.title or info.title == self._last_window_title:
            return
        self._last_window_title = info.title
        self._append_event(MacroEvent(
            type=EventType.WINDOW_FOCUS,
            ts=self._ts(),
            window=info.title,
            rect=[info.left, info.top, info.width, info.height],
        ))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ts(self) -> float:
        """Seconds elapsed since recording started."""
        return time.perf_counter() - self._start_time

    def _append_event(self, event: MacroEvent) -> None:
        """Thread-safe append to the event list."""
        with self._lock:
            self._events.append(event)

    @staticmethod
    def _build_groups(events: list[MacroEvent]) -> list[MacroGroup]:
        """Split a flat event list into per-window groups.

        Each window_focus event starts a new group; the event itself is not
        retained (its window/rect become the group's metadata).  Within a
        windowed group, mouse coordinates are converted to be relative to the
        window's top-left corner.  Events before the first focus change form a
        leading null-window group with absolute coordinates.
        """
        groups: list[MacroGroup] = []
        current_window: Optional[str] = None
        current_rect: Optional[WindowRect] = None
        current_events: list[MacroEvent] = []

        def _flush() -> None:
            # Keep a group only if it has events, or if it carries window context.
            if current_events or current_window is not None:
                groups.append(MacroGroup(
                    window=current_window,
                    recorded_rect=current_rect,
                    events=current_events,
                ))

        for ev in events:
            if ev.type == EventType.WINDOW_FOCUS:
                _flush()
                current_window = ev.window
                current_rect = WindowRect(*ev.rect) if ev.rect else None
                current_events = []
            else:
                if current_window and current_rect:
                    ev = offset_event(ev, -current_rect.left, -current_rect.top)
                current_events.append(ev)

        _flush()
        return groups

    # ------------------------------------------------------------------
    # Mouse callbacks
    # ------------------------------------------------------------------

    def _on_mouse_move(self, x: int, y: int) -> None:
        if (x, y) == self._last_mouse_pos:
            return
        self._last_mouse_pos = (x, y)
        self._append_event(MacroEvent(type=EventType.MOUSE_MOVE, ts=self._ts(), x=x, y=y))

    def _on_mouse_click(self, x: int, y: int, button, pressed: bool) -> None:
        self._append_event(MacroEvent(
            type=EventType.MOUSE_CLICK,
            ts=self._ts(),
            x=x,
            y=y,
            button=serialize_button(button),
            pressed=pressed,
        ))

    def _on_mouse_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self._append_event(MacroEvent(
            type=EventType.MOUSE_SCROLL,
            ts=self._ts(),
            x=x,
            y=y,
            dx=dx,
            dy=dy,
        ))

    # ------------------------------------------------------------------
    # Keyboard callbacks
    # ------------------------------------------------------------------

    def _on_key_press(self, key) -> Optional[bool]:
        """Handle a key-press event; returns False to stop listener on stop key."""
        if key == self._stop_key_obj:
            self._stop_event.set()
            return False
        self._append_event(MacroEvent(
            type=EventType.KEY_PRESS,
            ts=self._ts(),
            key=serialize_key(key),
        ))
        return None

    def _on_key_release(self, key) -> None:
        """Handle a key-release event; silently discards the stop key release."""
        if key == self._stop_key_obj:
            return
        self._append_event(MacroEvent(
            type=EventType.KEY_RELEASE,
            ts=self._ts(),
            key=serialize_key(key),
        ))
