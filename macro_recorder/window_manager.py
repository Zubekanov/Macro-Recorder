"""Platform-abstracted window management.

``get_window_manager`` picks a backend for the running desktop:

- Win32WindowManager uses pywin32 (win32gui) on Windows.
- X11WindowManager uses python-xlib (see ``x11.py``) on Linux desktops such as
  Linux Mint.  It talks EWMH to the window manager (Muffin, Mutter, Xfwm...).
- NullWindowManager is a no-op stub for anything else (no display, Wayland
  without XWayland, missing packages): recording still works, playback skips
  window setup.

A group's window title is matched against the open windows in one of two
modes.  ``substring`` (the default) is a case-insensitive containment test, so
a title recorded as ``report.txt - Notepad`` still matches once the document
name changes as long as the pattern is trimmed to ``Notepad``.  ``regex`` runs
``re.search`` with the pattern as written.  In both modes an exact
(case-insensitive) title wins over a partial one when several windows match.

Usage
-----
    wm = get_window_manager()
    info = wm.find_window("Notepad")
    if info:
        wm.move_resize(info, info.left, info.top, info.width, info.height)
        wm.set_foreground(info)

The recorder additionally calls ``watch_foreground`` on a worker thread to be
told about every foreground-window change while recording.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

MATCH_SUBSTRING = "substring"
MATCH_REGEX = "regex"
MATCH_MODES = (MATCH_SUBSTRING, MATCH_REGEX)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class WindowInfo:
    """Snapshot of a window's title and screen rectangle.

    ``hwnd`` is the native handle: a Win32 HWND or an X11 window id.
    """
    title: str
    left: int
    top: int
    width: int
    height: int
    hwnd: int = 0


# ---------------------------------------------------------------------------
# Title matching (pure)
# ---------------------------------------------------------------------------

def title_matches(title: str, pattern: str, mode: str = MATCH_SUBSTRING) -> bool:
    """True if ``title`` matches ``pattern`` under ``mode``.

    Raises ValueError for an invalid regex so the caller can report it.
    """
    if mode == MATCH_REGEX:
        try:
            return re.search(pattern, title) is not None
        except re.error as e:
            raise ValueError("Invalid window title pattern %r: %s" % (pattern, e)) from e
    return pattern.lower() in title.lower()


def select_window(windows: list[WindowInfo], pattern: str,
                  mode: str = MATCH_SUBSTRING) -> WindowInfo | None:
    """Pick the window for ``pattern``: an exact title first, else the first match."""
    for w in windows:
        if w.title.lower() == pattern.lower():
            return w
    for w in windows:
        if title_matches(w.title, pattern, mode):
            return w
    return None


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class AbstractWindowManager(ABC):
    """Thin abstraction over OS window-management APIs."""

    @abstractmethod
    def get_foreground(self) -> WindowInfo | None:
        """Return info about the currently active window, or None."""

    @abstractmethod
    def find_window(self, pattern: str, mode: str = MATCH_SUBSTRING) -> WindowInfo | None:
        """Find a visible window whose title matches ``pattern`` (see module doc)."""

    @abstractmethod
    def set_foreground(self, window: WindowInfo) -> bool:
        """Bring ``window`` to the foreground.  Returns True on success."""

    @abstractmethod
    def move_resize(self, window: WindowInfo, left: int, top: int,
                    width: int, height: int) -> bool:
        """Move and resize ``window`` to the given screen rectangle."""

    def watch_foreground(self, callback: Callable[[WindowInfo], None],
                         stop_event: threading.Event, poll_interval: float = 0.1) -> None:
        """Block until ``stop_event`` is set, reporting each new active window.

        ``callback`` receives a WindowInfo every time the active window changes,
        including once for the window active when watching starts.  The default
        polls :meth:`get_foreground`; backends with a native notification
        override it.
        """
        last = None
        while not stop_event.is_set():
            info = self.get_foreground()
            key = (info.hwnd, info.title) if info else None
            if key != last:
                last = key
                if info:
                    callback(info)
            stop_event.wait(poll_interval)


# ---------------------------------------------------------------------------
# Win32 implementation
# ---------------------------------------------------------------------------

class Win32WindowManager(AbstractWindowManager):
    """Window management via pywin32 (win32gui)."""

    def get_foreground(self) -> WindowInfo | None:
        import win32gui
        return self._info_from_hwnd(win32gui.GetForegroundWindow())

    def find_window(self, pattern: str, mode: str = MATCH_SUBSTRING) -> WindowInfo | None:
        return select_window(self._visible_windows(), pattern, mode)

    def set_foreground(self, window: WindowInfo) -> bool:
        import win32gui
        try:
            win32gui.SetForegroundWindow(window.hwnd)
            return True
        except Exception as e:
            log.debug("SetForegroundWindow failed for %r: %s", window.title, e)
            return False

    def move_resize(self, window: WindowInfo, left: int, top: int,
                    width: int, height: int) -> bool:
        import win32gui
        try:
            win32gui.MoveWindow(window.hwnd, left, top, width, height, True)
            return True
        except Exception as e:
            log.debug("MoveWindow failed for %r: %s", window.title, e)
            return False

    def watch_foreground(self, callback: Callable[[WindowInfo], None],
                         stop_event: threading.Event, poll_interval: float = 0.1) -> None:
        """Report foreground changes through a WinEvent hook.

        The hook needs a message loop on this thread; it is pumped between
        ``poll_interval`` waits so ``stop_event`` is honoured promptly.  Falls
        back to polling if the hook cannot be installed.
        """
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.windll.user32
        EVENT_SYSTEM_FOREGROUND = 0x0003
        WINEVENT_OUTOFCONTEXT = 0x0000
        QS_ALLINPUT = 0x04FF
        PM_REMOVE = 0x0001

        WinEventProc = ctypes.WINFUNCTYPE(
            None, ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.wintypes.HWND,
            ctypes.wintypes.LONG, ctypes.wintypes.LONG, ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
        )

        def _on_foreground(hook, event, hwnd, id_object, id_child, thread, event_time):
            if hwnd:
                info = self._info_from_hwnd(hwnd)
                if info:
                    callback(info)

        proc = WinEventProc(_on_foreground)   # keep a reference for the hook's lifetime
        hook = user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
                                      None, proc, 0, 0, WINEVENT_OUTOFCONTEXT)
        if not hook:
            log.debug("SetWinEventHook failed; polling the foreground window instead")
            super().watch_foreground(callback, stop_event, poll_interval)
            return
        initial = self.get_foreground()
        if initial:
            callback(initial)
        msg = ctypes.wintypes.MSG()
        try:
            while not stop_event.is_set():
                user32.MsgWaitForMultipleObjectsEx(0, None, int(poll_interval * 1000),
                                                   QS_ALLINPUT, 0)
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnhookWinEvent(hook)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _visible_windows(self) -> list[WindowInfo]:
        """All visible, titled top-level windows in Z order."""
        import win32gui
        found: list[WindowInfo] = []

        def _cb(hwnd: int, _: None) -> bool:
            if win32gui.IsWindowVisible(hwnd):
                info = self._info_from_hwnd(hwnd)
                if info:
                    found.append(info)
            return True

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception as e:
            log.debug("EnumWindows failed: %s", e)
        return found

    @staticmethod
    def _info_from_hwnd(hwnd: int) -> WindowInfo | None:
        import win32gui
        try:
            text = win32gui.GetWindowText(hwnd)
            if not text:
                return None
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            return WindowInfo(title=text, left=left, top=top,
                              width=right - left, height=bottom - top, hwnd=hwnd)
        except Exception as e:
            log.debug("Could not read window info for hwnd %r: %s", hwnd, e)
            return None


# ---------------------------------------------------------------------------
# X11 implementation (Linux)
# ---------------------------------------------------------------------------

class X11WindowManager(AbstractWindowManager):
    """Window management via python-xlib and EWMH.

    Keeps one display connection, guarded by a lock because the player and the
    GUI may call in from different threads.  The connection is dropped and
    reopened after any X error.
    """

    def __init__(self) -> None:
        self._display = None
        self._lock = threading.Lock()

    def _run(self, fn, default):
        """Run ``fn(display)`` under the lock, returning ``default`` on X failure."""
        from Xlib.error import XError

        with self._lock:
            try:
                if self._display is None:
                    from macro_recorder import x11
                    self._display = x11.open_display()
                return fn(self._display)
            except (XError, OSError, ConnectionError) as e:
                log.debug("X11 window operation failed: %s", e)
                self._close_locked()
                return default

    def _close_locked(self) -> None:
        if self._display is not None:
            try:
                self._display.close()
            except Exception:
                pass
            self._display = None

    def get_foreground(self) -> WindowInfo | None:
        from macro_recorder import x11
        return self._run(x11.foreground_info, None)

    def find_window(self, pattern: str, mode: str = MATCH_SUBSTRING) -> WindowInfo | None:
        from macro_recorder import x11
        windows = self._run(x11.list_windows, [])
        return select_window(windows, pattern, mode)

    def set_foreground(self, window: WindowInfo) -> bool:
        from macro_recorder import x11
        return self._run(lambda d: (x11.activate(d, window.hwnd), True)[1], False)

    def move_resize(self, window: WindowInfo, left: int, top: int,
                    width: int, height: int) -> bool:
        from macro_recorder import x11
        return self._run(
            lambda d: (x11.move_resize(d, window.hwnd, left, top, width, height), True)[1],
            False)

    def watch_foreground(self, callback: Callable[[WindowInfo], None],
                         stop_event: threading.Event, poll_interval: float = 0.25) -> None:
        from macro_recorder import x11
        x11.watch_active_window(callback, stop_event, poll_interval)


# ---------------------------------------------------------------------------
# Null stub
# ---------------------------------------------------------------------------

class NullWindowManager(AbstractWindowManager):
    """No-op stub used when no supported windowing system is available."""

    def get_foreground(self) -> None:
        return None

    def find_window(self, pattern: str, mode: str = MATCH_SUBSTRING) -> None:
        return None

    def set_foreground(self, window: WindowInfo) -> bool:
        return False

    def move_resize(self, window: WindowInfo, left: int, top: int,
                    width: int, height: int) -> bool:
        return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_window_manager() -> AbstractWindowManager:
    """Return the appropriate window manager for the current platform."""
    if sys.platform == "win32":
        try:
            import win32gui  # noqa: F401
            return Win32WindowManager()
        except ImportError as e:
            log.debug("pywin32 unavailable, window management disabled: %s", e)
            return NullWindowManager()
    if os.environ.get("DISPLAY"):
        try:
            from macro_recorder import x11
            x11.open_display().close()
            return X11WindowManager()
        except (ImportError, OSError) as e:
            log.debug("X11 unavailable, window management disabled: %s", e)
    return NullWindowManager()
