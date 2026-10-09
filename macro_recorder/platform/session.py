"""Session checks and the registry of the application's own X windows.

Pure Python apart from reading the environment, so the model and the CLI
can use it without a display.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionProblem:
    code: str       # wayland | no_display
    message: str


def check(environ: dict[str, str] | None = None) -> SessionProblem | None:
    """Why global input cannot work in this session, or None when it can."""
    env = os.environ if environ is None else environ
    if env.get("XDG_SESSION_TYPE", "").lower() == "wayland" or env.get("WAYLAND_DISPLAY"):
        return SessionProblem(
            "wayland",
            "This is a Wayland session. Recording and playback need X11: "
            "log out and choose 'Cinnamon' (not 'Cinnamon (Wayland)') at the login screen.")
    if not env.get("DISPLAY"):
        return SessionProblem("no_display", "DISPLAY is not set; no X server to talk to.")
    return None


class OwnWindows:
    """Thread-safe set of the application's top-level X window ids.

    Tk toplevels register on <Map> and unregister on <Destroy>.  The recorder
    and the active-window watcher consult it from their own threads.
    """

    def __init__(self) -> None:
        self._ids: set[int] = set()
        self._overlay: set[int] = set()
        self._lock = threading.Lock()

    def add(self, xid: int, overlay: bool = False) -> None:
        with self._lock:
            self._ids.add(xid)
            if overlay:
                self._overlay.add(xid)

    def remove(self, xid: int) -> None:
        with self._lock:
            self._ids.discard(xid)
            self._overlay.discard(xid)

    def contains(self, xid: int, include_overlay: bool = False) -> bool:
        with self._lock:
            if xid in self._ids:
                return include_overlay or xid not in self._overlay
            return False

    def ids(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._ids)


own_windows = OwnWindows()
