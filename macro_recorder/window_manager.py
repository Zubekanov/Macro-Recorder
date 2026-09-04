"""Platform-abstracted window management.

Win32WindowManager uses pywin32 (win32gui / win32con) on Windows.
NullWindowManager is a no-op stub used on non-Windows platforms or when
pywin32 is not installed — recording still works, playback skips window setup.

Usage
-----
    wm = get_window_manager()
    info = wm.find_window("Notepad")
    if info:
        wm.move_resize(info.title, info.left, info.top, info.width, info.height)
        wm.set_foreground(info.title)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class WindowInfo:
    """Snapshot of a window's title and screen rectangle."""
    title: str
    left: int
    top: int
    width: int
    height: int


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class AbstractWindowManager(ABC):
    """Thin abstraction over OS window-management APIs."""

    @abstractmethod
    def get_foreground(self) -> WindowInfo | None:
        """Return info about the currently active window, or None."""

    @abstractmethod
    def find_window(self, title: str) -> WindowInfo | None:
        """Find a visible window whose title starts with *title* (case-insensitive).

        Returns the first match, or None if no window is found.
        """

    @abstractmethod
    def set_foreground(self, title: str) -> bool:
        """Bring the window with the given title to the foreground.

        Returns True on success, False if the window could not be found or
        activated.
        """

    @abstractmethod
    def move_resize(self, title: str, left: int, top: int,
                    width: int, height: int) -> bool:
        """Move and resize the window to the given screen rectangle.

        Returns True on success.
        """


# ---------------------------------------------------------------------------
# Win32 implementation
# ---------------------------------------------------------------------------

class Win32WindowManager(AbstractWindowManager):
    """Window management via pywin32 (win32gui)."""

    def get_foreground(self) -> WindowInfo | None:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        return self._info_from_hwnd(hwnd)

    def find_window(self, title: str) -> WindowInfo | None:
        import win32gui
        title_lower = title.lower()
        result: list[WindowInfo] = []

        def _cb(hwnd: int, _: None) -> bool:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            text = win32gui.GetWindowText(hwnd)
            if text.lower().startswith(title_lower):
                info = self._info_from_hwnd(hwnd)
                if info:
                    result.append(info)
                    return False   # stop after first match
            return True

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception:
            pass   # EnumWindows raises when the callback returns False — expected

        return result[0] if result else None

    def set_foreground(self, title: str) -> bool:
        import win32gui
        info = self.find_window(title)
        if info is None:
            return False
        hwnd = win32gui.FindWindow(None, info.title)
        if not hwnd:
            # FindWindow requires exact match; fall back to EnumWindows result
            return self._set_foreground_by_prefix(title)
        try:
            win32gui.SetForegroundWindow(hwnd)
            return True
        except Exception as e:
            log.debug("SetForegroundWindow failed for %r, trying prefix match: %s", title, e)
            return self._set_foreground_by_prefix(title)

    def move_resize(self, title: str, left: int, top: int,
                    width: int, height: int) -> bool:
        import win32gui
        info = self.find_window(title)
        if info is None:
            return False
        hwnd = self._hwnd_from_title_prefix(title)
        if not hwnd:
            return False
        try:
            win32gui.MoveWindow(hwnd, left, top, width, height, True)
            return True
        except Exception as e:
            log.debug("MoveWindow failed for %r: %s", title, e)
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _info_from_hwnd(hwnd: int) -> WindowInfo | None:
        import win32gui
        try:
            text = win32gui.GetWindowText(hwnd)
            if not text:
                return None
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            return WindowInfo(
                title=text,
                left=left,
                top=top,
                width=right - left,
                height=bottom - top,
            )
        except Exception as e:
            log.debug("Could not read window info for hwnd %r: %s", hwnd, e)
            return None

    def _hwnd_from_title_prefix(self, prefix: str) -> int:
        """Return the hwnd of the first visible window whose title starts with prefix."""
        import win32gui
        prefix_lower = prefix.lower()
        result: list[int] = []

        def _cb(hwnd: int, _: None) -> bool:
            if win32gui.IsWindowVisible(hwnd):
                if win32gui.GetWindowText(hwnd).lower().startswith(prefix_lower):
                    result.append(hwnd)
                    return False
            return True

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception:
            pass
        return result[0] if result else 0

    def _set_foreground_by_prefix(self, prefix: str) -> bool:
        import win32gui
        hwnd = self._hwnd_from_title_prefix(prefix)
        if not hwnd:
            return False
        try:
            win32gui.SetForegroundWindow(hwnd)
            return True
        except Exception as e:
            log.debug("SetForegroundWindow (prefix) failed for %r: %s", prefix, e)
            return False


# ---------------------------------------------------------------------------
# Null stub (non-Windows / pywin32 unavailable)
# ---------------------------------------------------------------------------

class NullWindowManager(AbstractWindowManager):
    """No-op stub used when pywin32 is unavailable or on non-Windows platforms."""

    def get_foreground(self) -> None:
        return None

    def find_window(self, title: str) -> None:
        return None

    def set_foreground(self, title: str) -> bool:
        return False

    def move_resize(self, title: str, left: int, top: int,
                    width: int, height: int) -> bool:
        return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_window_manager() -> AbstractWindowManager:
    """Return the appropriate window manager for the current platform."""
    try:
        import win32gui  # noqa: F401
        return Win32WindowManager()
    except ImportError:
        return NullWindowManager()
