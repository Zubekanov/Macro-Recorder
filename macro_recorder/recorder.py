"""System-wide input recorder using pynput listeners."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from typing import Optional

from pynput import keyboard, mouse

from macro_recorder.event_types import EventType
from macro_recorder.macro import (
    MacroEvent,
    MacroGroup,
    WindowRect,
    deserialize_key,
    serialize_button,
    serialize_key,
)
from macro_recorder.window_manager import get_window_manager

log = logging.getLogger(__name__)


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
    - Window focus monitoring uses SetWinEventHook (Windows only); the
      feature silently no-ops on other platforms.
    - On Windows, recording keystrokes from elevated (UAC) windows
      requires running Python as Administrator.
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
        self._focus_thread_id: int = 0
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
        self._last_window_title = self._get_foreground_title()
        self._focus_thread_id = 0
        self._start_time = time.perf_counter()

        # Start window-focus monitor (Windows-only; silently skipped elsewhere)
        focus_ready = threading.Event()
        focus_thread = threading.Thread(
            target=self._run_window_monitor, args=(focus_ready,), daemon=True,
        )
        focus_thread.start()
        focus_ready.wait(timeout=1.0)   # give the thread time to register its hook

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

        # Tear down window monitor by posting WM_QUIT to its message loop
        if self._focus_thread_id:
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(
                    self._focus_thread_id, 0x0012, 0, 0,  # WM_QUIT
                )
            except Exception as e:
                log.debug("Failed to post WM_QUIT to focus monitor thread: %s", e)
        focus_thread.join(timeout=1.0)

        mouse_listener.stop()
        kb_listener.stop()
        mouse_listener.join()
        kb_listener.join()

        return self._build_groups(list(self._events))

    # ------------------------------------------------------------------
    # Window focus monitoring (Windows only)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_foreground_title() -> str:
        """Return the title of the currently active window, or empty string."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(max(length + 1, 256))
            user32.GetWindowTextW(hwnd, buf, len(buf))
            return buf.value
        except Exception as e:
            log.debug("Could not read foreground window title: %s", e)
            return ""

    def _run_window_monitor(self, ready: threading.Event) -> None:
        """Run a WinEvent hook loop that fires on foreground-window changes.

        Runs until WM_QUIT is posted to this thread's message queue.
        Silently exits on non-Windows platforms or any hook failure.
        """
        try:
            import ctypes
            import ctypes.wintypes

            user32 = ctypes.windll.user32
            self._focus_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            ready.set()   # unblock start() once we have our thread ID

            EVENT_SYSTEM_FOREGROUND = 0x0003
            WINEVENT_OUTOFCONTEXT = 0x0000

            WinEventProc = ctypes.WINFUNCTYPE(
                None,
                ctypes.wintypes.HANDLE,  # hWinEventHook
                ctypes.wintypes.DWORD,   # event
                ctypes.wintypes.HWND,    # hwnd
                ctypes.wintypes.LONG,    # idObject
                ctypes.wintypes.LONG,    # idChild
                ctypes.wintypes.DWORD,   # dwEventThread
                ctypes.wintypes.DWORD,   # dwmsEventTime
            )

            def _on_foreground(hWinEventHook, event, hwnd, idObject, idChild,
                               dwEventThread, dwmsEventTime) -> None:
                if not hwnd:
                    return
                length = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(max(length + 1, 256))
                user32.GetWindowTextW(hwnd, buf, len(buf))
                title = buf.value
                if title and title != self._last_window_title:
                    self._last_window_title = title
                    rect = ctypes.wintypes.RECT()
                    rect_list = None
                    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        rect_list = [
                            rect.left, rect.top,
                            rect.right - rect.left, rect.bottom - rect.top,
                        ]
                    self._append_event(MacroEvent(
                        type=EventType.WINDOW_FOCUS,
                        ts=self._ts(),
                        window=title,
                        rect=rect_list,
                    ))

            # Keep a reference so the callback is not garbage-collected
            proc = WinEventProc(_on_foreground)

            hook = user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND,
                EVENT_SYSTEM_FOREGROUND,
                None,
                proc,
                0, 0,
                WINEVENT_OUTOFCONTEXT,
            )
            if not hook:
                return

            msg = ctypes.wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0 or result == -1:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            user32.UnhookWinEvent(hook)

        except Exception as e:
            log.debug("Window focus monitor exited on error: %s", e)
        finally:
            ready.set()   # ensure start() never blocks indefinitely on failure

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
                if current_window and current_rect and ev.x is not None and ev.y is not None:
                    ev = replace(ev, x=ev.x - current_rect.left,
                                 y=ev.y - current_rect.top)
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
