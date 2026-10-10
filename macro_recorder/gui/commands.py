"""The command registry: every menu item, button and shortcut runs through it.

Tk-free.  A :class:`Command` declares the modes it is enabled in; the shell
calls :meth:`CommandRegistry.set_mode` on every mode change, and menus and
buttons subscribe so they can refresh their state.  :meth:`run` re-checks
enablement, so a shortcut that fires in the wrong mode does nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from macro_recorder.model.protocols import Mode

log = logging.getLogger(__name__)

ALL_MODES: frozenset[Mode] = frozenset(Mode)
IDLE_ONLY: frozenset[Mode] = frozenset({Mode.IDLE})


@dataclass(frozen=True)
class Command:
    """One user command.

    ``when`` is an optional extra condition (a selection, an undo entry)
    evaluated on every enablement check.
    """
    name: str
    label: str
    run: Callable[..., Any]
    enabled_in: frozenset[Mode]
    accelerator: str | None = None
    when: Callable[[], bool] | None = None


class CommandRegistry:
    """Commands by name, with per-mode enablement and change subscriptions."""

    def __init__(self, mode: Mode = Mode.IDLE) -> None:
        self._commands: dict[str, Command] = {}
        self._unavailable: set[str] = set()
        self._mode = mode
        self._listeners: list[Callable[[], None]] = []

    # -- registration --------------------------------------------------------

    def register(self, command: Command) -> Command:
        if command.name in self._commands:
            raise ValueError("Command %r registered twice" % command.name)
        self._commands[command.name] = command
        return command

    def get(self, name: str) -> Command:
        return self._commands[name]

    def names(self) -> tuple[str, ...]:
        return tuple(self._commands)

    def __contains__(self, name: object) -> bool:
        return name in self._commands

    # -- state ---------------------------------------------------------------

    @property
    def mode(self) -> Mode:
        return self._mode

    def set_mode(self, mode: Mode) -> None:
        """Switch modes and tell every subscriber."""
        if mode == self._mode:
            return
        self._mode = mode
        self.refresh()

    def set_available(self, name: str, available: bool) -> None:
        """Veto a command in every mode (Record and Play when the session check fails)."""
        if name not in self._commands:
            raise KeyError(name)
        before = name not in self._unavailable
        if available:
            self._unavailable.discard(name)
        else:
            self._unavailable.add(name)
        if before != available:
            self.refresh()

    def enabled(self, name: str) -> bool:
        command = self._commands[name]
        if name in self._unavailable or self._mode not in command.enabled_in:
            return False
        if command.when is None:
            return True
        try:
            return bool(command.when())
        except Exception:
            log.exception("Enablement check of %s failed", name)
            return False

    def run(self, name: str, *args: Any) -> bool:
        """Run a command if it is enabled now; True when it ran."""
        if not self.enabled(name):
            return False
        self._commands[name].run(*args)
        return True

    # -- subscriptions -------------------------------------------------------

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` after every mode or availability change; returns an unsubscribe."""
        self._listeners.append(callback)

        def unsubscribe() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)
        return unsubscribe

    def refresh(self) -> None:
        """Tell subscribers to re-read enablement (after a selection or document change)."""
        for callback in list(self._listeners):
            callback()
