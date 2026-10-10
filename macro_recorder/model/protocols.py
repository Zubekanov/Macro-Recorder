"""Value types and Protocols shared by the model, engine, platform and GUI.

Everything here is pure Python so `model/` and the action modules can type
against the platform without importing it.  `tests/conftest.py` provides a
fake for every Protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence


class Mode(str, Enum):
    """Application modes; every user command declares the modes it is enabled in."""
    IDLE = "idle"
    TARGETING = "targeting"
    RECORDING = "recording"
    PLAYING = "playing"
    PAUSED = "paused"


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


@dataclass(frozen=True)
class WindowInfo:
    """Snapshot of a top-level window."""
    id: int
    title: str
    rect: Rect              # outer frame
    client_rect: Rect       # client area
    desktop: int = 0        # -1 (0xFFFFFFFF) means sticky
    maximised: bool = False


@dataclass(frozen=True)
class Monitor:
    index: int              # 1-based
    rect: Rect
    work_area: Rect


@dataclass(frozen=True)
class OverlayShape:
    """What the overlay draws for a row: screen coordinates."""
    kind: str                                   # point, click, line, path, rect
    points: tuple[tuple[float, float], ...]


# Raw recorded input, before bundling.
@dataclass(frozen=True)
class RawEvent:
    kind: str       # key_press, key_release, button_press, button_release, motion, scroll, window_focus
    time: float     # seconds, monotonic
    x: int = 0
    y: int = 0
    key: str = ""           # keysym name for key events
    button: str = ""        # left, right, middle, x1, x2
    dx: int = 0             # scroll notches
    dy: int = 0
    window: WindowInfo | None = None   # for window_focus


# ---------------------------------------------------------------------------
# Platform Protocols
# ---------------------------------------------------------------------------

class WindowManager(Protocol):
    def list(self) -> list[WindowInfo]: ...
    def active(self) -> WindowInfo | None: ...
    def find(self, pattern: str, mode: str = "substring") -> WindowInfo | None:
        """First match by title; an exact title wins.  Raises ValueError for a bad regex."""
    def refresh(self, window: WindowInfo) -> WindowInfo | None:
        """Re-read a window; None when it is gone."""
    def activate(self, window: WindowInfo, settle_ms: int = 1000) -> WindowInfo | None:
        """Raise and focus; returns the settled window or None on failure."""
    def move(self, window: WindowInfo, rect: Rect, settle_ms: int = 1000) -> WindowInfo | None:
        """Place the outer frame at rect; returns the settled window or None."""
    def set_maximised(self, window: WindowInfo, maximised: bool, settle_ms: int = 1000) -> WindowInfo | None: ...
    def watch_active(self, callback: Callable[[WindowInfo], None]) -> Callable[[], None]:
        """Report every active-window change (not the window active at the start)
        until the returned function is called."""
    def monitors(self) -> list[Monitor]: ...
    def bell(self) -> None: ...
    def screen_size(self) -> tuple[int, int]: ...
    def window_at(self, x: int, y: int) -> int | None:
        """The top-level window id under a screen point, or None."""


class InputBackend(Protocol):
    """Synthetic input (XTest)."""
    def move(self, x: float, y: float) -> None: ...
    def position(self) -> tuple[int, int]: ...
    def press_button(self, button: str) -> None: ...
    def release_button(self, button: str) -> None: ...
    def scroll(self, dx: int, dy: int) -> None:
        """One call per notch: dx or dy is +1 or -1."""
    def press_key(self, name: str) -> None: ...
    def release_key(self, name: str) -> None: ...
    def type_char(self, char: str) -> None:
        """Press and release whatever keys produce ``char`` in the current keymap."""
    def release_all(self) -> None: ...
    def close(self) -> None: ...


class InputListener(Protocol):
    """Raw global input stream (XRecord)."""
    def start(self, callback: Callable[[RawEvent], None]) -> None:
        """Start delivering events on a background thread; returns once live."""
    def stop(self) -> None: ...
    def is_alive(self) -> bool: ...
    def close(self) -> None: ...


class StopKeyWatcher(Protocol):
    def start(self, key: str, callback: Callable[[], None]) -> None:
        """Call ``callback`` when the user (not the injector) presses ``key``."""
    def stop(self) -> None: ...
    def close(self) -> None: ...


class HotkeyGrabber(Protocol):
    def grab(self, combo: str, callback: Callable[[], None]) -> bool:
        """Grab a combo globally; False if the X server refuses."""
    def ungrab_all(self) -> None: ...
    def suspend(self) -> None:
        """Release every grab, remembering the combos (for the Settings dialog)."""
    def resume(self) -> list[str]:
        """Re-grab the remembered combos; returns the ones the server refused."""
    def close(self) -> None: ...


class Screen(Protocol):
    def capture(self, rect: Rect) -> Any:
        """A PIL RGB image of the rect."""
    def pixel(self, x: int, y: int) -> tuple[int, int, int]: ...
    def find_image(self, rect: Rect, path: str, confidence: float) -> tuple[int, int, float] | None:
        """(centre_x, centre_y, score) in screen pixels, or None."""
    def ocr(self, rect: Rect) -> str: ...
    def monitors(self) -> list[Monitor]: ...


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


# ---------------------------------------------------------------------------
# Engine Protocols
# ---------------------------------------------------------------------------

class PlayerHost(Protocol):
    """Where the player reports.  Every method may be called from the player thread."""
    def on_row(self, row_id: int) -> None: ...
    def on_log(self, line: str) -> None: ...
    def on_paused(self, row_id: int, variables: Mapping[str, Any]) -> None: ...
    def on_finished(self, error: str | None) -> None: ...
    def ask(self, title: str, text: str, buttons: str) -> str | None:
        """Block until the user picks a button ("ok", "cancel", "yes", "no") or playback stops (None)."""


class PlayContext(Protocol):
    """What an action's ``play`` may use."""
    row_id: int
    speed: float
    variables: Any                      # MutableMapping with dynamic values
    input: InputBackend
    screen: Screen
    windows: WindowManager
    host: PlayerHost
    settings: Any                       # Settings
    document_dir: str | None

    def resolve(self, value: Any, default: float = 0.0) -> float:
        """Number or expression -> float.  Raises ExpressionError."""
    def resolve_int(self, value: Any, default: int = 0) -> int: ...
    def coord(self, x: Any, y: Any) -> tuple[float, float]:
        """Apply the coordinate frame to a COORD_PAIR."""
    def to_frame(self, x: float, y: float) -> tuple[float, float]:
        """Screen pixels -> the frame expressions use (for captured values)."""
    def region(self, values: Mapping[str, Any]) -> Rect:
        """A REGION field's five keys -> screen rect."""
    def render(self, template: str) -> str: ...
    def sleep(self, ms: float, scaled: bool = True) -> bool:
        """Wait; True when playback was stopped meanwhile."""
    def now(self) -> float:
        """Monotonic seconds on the engine's clock, unaffected by speed."""
    def stopped(self) -> bool: ...
    def log(self, text: str) -> None: ...
    def hold_key(self, name: str) -> None:
        """Reference-counted press: presses on the first hold of a name and records
        it, so a stop or pause releases it and a resume presses it again."""
    def unhold_key(self, name: str) -> None:
        """Drop one hold; releases on the last one (or at once when not held)."""
    def hold_button(self, name: str) -> None: ...
    def unhold_button(self, name: str) -> None: ...
    def reactivate_context(self) -> None:
        """Re-focus the current window context after a dialog or pause."""
    def enter_window(self, row: Any) -> Any:
        """Run a window row's focus logic and make it the context.

        Returns Flow.NEXT, a Jump or an Abort exactly like an action's play.
        """
    def move_pointer(self, points: Sequence[tuple[float, float]], duration_ms: float,
                     dts: Sequence[float] | None = None) -> bool:
        """Move through screen points.

        With ``dts`` (seconds per point) under path mode ``recorded`` the points
        replay with their timing; otherwise the engine draws the document's
        path mode from the current pointer to the last point over
        ``duration_ms``.  Actions pass unscaled times; the engine applies
        1/speed.  Returns True when playback was stopped meanwhile.
        """
    def row_state(self, row_id: int) -> dict:
        """Mutable per-row state, cleared at each program pass.  A repeat row
        keeps ``counter`` (passes so far) and ``count`` (the resolved total)."""
    def run_command(self, command: str, wait: bool, timeout_ms: float = 0) -> int | None:
        """Run a shell command (see exec_program).  Raises OSError when it cannot
        start.  Exit code when waited for; None when the wait timed out or
        playback stopped."""


# ---------------------------------------------------------------------------
# Change notifications from the document
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Change:
    kind: str                       # reset, inserted, removed, moved, updated, meta
    ids: tuple[int, ...] = ()


ChangeListener = Callable[[Change], None]
