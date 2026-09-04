"""Platform-abstracted window management.

Win32WindowManager uses pywin32 (win32gui) on Windows.  NullWindowManager is a
no-op stub used on non-Windows platforms or when pywin32 is not installed:
recording still works, playback skips window setup.

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
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

log = logging.getLogger(__name__)

MATCH_SUBSTRING = "substring"
MATCH_REGEX = "regex"
MATCH_MODES = (MATCH_SUBSTRING, MATCH_REGEX)


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
# Null stub (non-Windows / pywin32 unavailable)
# ---------------------------------------------------------------------------

class NullWindowManager(AbstractWindowManager):
    """No-op stub used when pywin32 is unavailable or on non-Windows platforms."""

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
    try:
        import win32gui  # noqa: F401
        return Win32WindowManager()
    except ImportError:
        return NullWindowManager()
