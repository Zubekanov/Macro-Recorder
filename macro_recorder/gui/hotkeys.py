"""Routes the global hotkeys and stop keys by application mode.

Tk-free: the grabber and stop-key watcher call in from their own threads and
the router calls the shell's actions there.  The actions are expected to
post to the main-thread queue; the shell re-checks the mode when it runs
them.

| Mode | Record combo | Play combo | Stop key |
|---|---|---|---|
| idle | start recording | play | none |
| recording | stop recording | ignored | F6 stops recording |
| playing | ignored | ignored | Escape stops playback |
| paused | ignored | resume | Escape stops playback |
| targeting | ignored | ignored | none |

Everything is ignored while ``is_blocked()`` is true (a modal grab).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from macro_recorder.model.keys import format_combo, parse_combo
from macro_recorder.model.protocols import HotkeyGrabber, StopKeyWatcher
from macro_recorder.platform.input import combo_problem
from macro_recorder.platform.x11 import MODIFIER_FAMILIES

log = logging.getLogger(__name__)

RECORD_STOP_KEY = "F6"
PLAY_STOP_KEY = "Escape"


@dataclass(frozen=True)
class HotkeyActions:
    """What the router may ask the shell to do."""
    start_record: Callable[[], None]
    stop_record: Callable[[str], None]      # trigger: "hotkey" or "stop_key"
    play: Callable[[], None]
    resume: Callable[[], None]
    stop_play: Callable[[], None]


def mode_name(mode: Any) -> str:
    """Mode as a lower-case string, accepting an Enum or a string."""
    return str(getattr(mode, "value", mode)).lower()


class HotkeyRouter:
    """Grabs the record and play combos from settings and routes presses by mode."""

    def __init__(self, grabber: HotkeyGrabber, stop_watcher: StopKeyWatcher, settings: Any,
                 mode_provider: Callable[[], Any], actions: HotkeyActions,
                 is_blocked: Callable[[], bool] | None = None) -> None:
        self._grabber = grabber
        self._stop_watcher = stop_watcher
        self._settings = settings
        self._mode = mode_provider
        self._actions = actions
        self._is_blocked = is_blocked or (lambda: False)
        self._failures: dict[str, str] = {}
        self._armed: str | None = None

    @staticmethod
    def check(combo: str) -> str | None:
        """Why ``combo`` cannot be a hotkey, or None.  For the Settings dialog."""
        return combo_problem(combo)

    @property
    def failures(self) -> dict[str, str]:
        """``{"record" | "play": message}`` for every combo not grabbed."""
        return dict(self._failures)

    def start(self) -> dict[str, str]:
        """Grab both combos from settings, replacing earlier grabs; returns the failures.

        Call again after the Settings dialog changes the combos.  While the
        grabber is suspended the server is only asked on :meth:`resume`,
        which reports its refusals.
        """
        self._grabber.ungrab_all()
        self._failures = {}
        hotkeys = self._settings.hotkeys
        same = _same_combo(hotkeys.record, hotkeys.play)
        for name, combo, callback in (("record", hotkeys.record, self._on_record),
                                      ("play", hotkeys.play, self._on_play)):
            problem = combo_problem(combo)
            if problem is None and same and name == "play":
                problem = "Record and Play use the same keys"
            if problem is None and not self._grabber.grab(combo, callback):
                problem = "%s is in use by another application" % _display(combo)
            if problem is not None:
                self._failures[name] = problem
                log.warning("%s hotkey not available: %s", name.capitalize(), problem)
        return self.failures

    def suspend(self) -> None:
        """Release the grabs while the Settings dialog captures combos."""
        self._grabber.suspend()

    def resume(self) -> dict[str, str]:
        """Grab the combos again after the dialog; returns the failures, as :meth:`start`."""
        refused = set(self._grabber.resume() or ())
        hotkeys = self._settings.hotkeys
        for name, combo in (("record", hotkeys.record), ("play", hotkeys.play)):
            if combo in refused and name not in self._failures:
                self._failures[name] = "%s is in use by another application" % _display(combo)
                log.warning("%s hotkey not available: %s", name.capitalize(),
                            self._failures[name])
        return self.failures

    def mode_changed(self, mode: Any) -> None:
        """Arm the stop key for the new mode: F6 while recording, Escape while playing."""
        name = mode_name(mode)
        wanted = {"recording": RECORD_STOP_KEY, "playing": PLAY_STOP_KEY,
                  "paused": PLAY_STOP_KEY}.get(name)
        if wanted == self._armed:
            return
        if self._armed is not None:
            self._stop_watcher.stop()
            self._armed = None
        if wanted is not None:
            try:
                self._stop_watcher.start(wanted, self._on_stop_key)
            except (OSError, ValueError) as e:
                log.warning("Stop key %s unavailable: %s", wanted, e)
                return
            self._armed = wanted

    def stop(self) -> None:
        """Release everything (application exit)."""
        self._grabber.ungrab_all()
        if self._armed is not None:
            self._stop_watcher.stop()
            self._armed = None

    # -- routing ------------------------------------------------------------

    def _current(self) -> str | None:
        if self._is_blocked():
            return None
        return mode_name(self._mode())

    def _on_record(self) -> None:
        mode = self._current()
        if mode == "idle":
            self._actions.start_record()
        elif mode == "recording":
            self._actions.stop_record("hotkey")

    def _on_play(self) -> None:
        mode = self._current()
        if mode == "idle":
            self._actions.play()
        elif mode == "paused":
            self._actions.resume()

    def _on_stop_key(self) -> None:
        mode = self._current()
        if mode == "recording":
            self._actions.stop_record("stop_key")
        elif mode in ("playing", "paused"):
            self._actions.stop_play()


def _same_combo(first: str, second: str) -> bool:
    """True when two combos press the same key with the same modifiers, in any order."""
    def key(combo: str) -> tuple[frozenset[str], str]:
        names = parse_combo(combo)
        return frozenset(MODIFIER_FAMILIES.get(n, n) for n in names[:-1]), names[-1]
    try:
        return key(first) == key(second)
    except ValueError:
        return False


def _display(combo: str) -> str:
    try:
        return format_combo(parse_combo(combo))
    except ValueError:
        return combo
