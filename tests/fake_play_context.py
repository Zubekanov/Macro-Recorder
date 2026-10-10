"""A PlayContext over the conftest fakes for the action tests.

Numbers resolve as themselves and a bare variable name resolves from
``variables``; ``coord`` adds a configurable frame delta; ``sleep`` records
each request and advances a FakeClock, which ``now`` reads; ``stop_on_sleep``
makes the Nth sleep (1-based) report a stop.  Holds are reference-counted:
only the first hold of a name presses it and only the last unhold releases
it, and an unhold of a name that is not held releases it anyway.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from macro_recorder.gui.settings import Settings
from macro_recorder.model.protocols import Rect
from tests.conftest import FakeClock, FakeHost, FakeInput, FakeScreen, FakeWindows

_TEMPLATE_RE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


class FakePlayContext:
    def __init__(self, delta: tuple[float, float] = (0.0, 0.0), speed: float = 1.0,
                 stop_on_sleep: int | None = None, row_id: int = 1) -> None:
        self.row_id = row_id
        self.speed = speed
        self.variables: dict[str, Any] = {}
        self.input = FakeInput()
        self.screen = FakeScreen()
        self.windows = FakeWindows()
        self.host = FakeHost()
        self.settings = Settings()
        self.document_dir: str | None = None
        self.clock = FakeClock()
        self.delta = delta
        self.stop_on_sleep = stop_on_sleep
        self.sleeps: list[tuple[float, bool]] = []
        self.logs: list[str] = []
        self.held_keys: list[str] = []
        self.held_buttons: list[str] = []
        self.pointer_moves: list[tuple[list[tuple[float, float]], float, list[float] | None]] = []
        self.pointer_stop = False
        self._stopped = False

    # Values ---------------------------------------------------------------

    def resolve(self, value: Any, default: float = 0.0) -> float:
        if value is None or value == "":
            return default
        if isinstance(value, str) and value in self.variables:
            return float(self.variables[value])
        return float(value)

    def resolve_int(self, value: Any, default: int = 0) -> int:
        return int(round(self.resolve(value, default)))

    def coord(self, x: Any, y: Any) -> tuple[float, float]:
        return (self.resolve(x) + self.delta[0], self.resolve(y) + self.delta[1])

    def to_frame(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.delta[0], y - self.delta[1])

    def region(self, values: Mapping[str, Any]) -> Rect:
        x, y = self.coord(values["x"], values["y"])
        x2, y2 = self.coord(values["x2"], values["y2"])
        return Rect(int(min(x, x2)), int(min(y, y2)), int(abs(x2 - x)), int(abs(y2 - y)))

    def render(self, template: str) -> str:
        def sub(m: re.Match) -> str:
            if m.group(0) == "{{":
                return "{"
            if m.group(0) == "}}":
                return "}"
            return str(self.variables[m.group(1)])
        return _TEMPLATE_RE.sub(sub, template)

    # Time and stop --------------------------------------------------------

    def sleep(self, ms: float, scaled: bool = True) -> bool:
        self.sleeps.append((ms, scaled))
        self.clock.sleep((ms / self.speed if scaled else ms) / 1000.0)
        if self.stop_on_sleep is not None and len(self.sleeps) >= self.stop_on_sleep:
            self._stopped = True
        return self._stopped

    def now(self) -> float:
        return self.clock.now()

    def stopped(self) -> bool:
        return self._stopped

    def stop(self) -> None:
        self._stopped = True

    def log(self, text: str) -> None:
        self.logs.append(text)

    # Holds ----------------------------------------------------------------

    def hold_key(self, name: str) -> None:
        if name not in self.held_keys:
            self.input.press_key(name)
        self.held_keys.append(name)

    def unhold_key(self, name: str) -> None:
        if name in self.held_keys:
            self.held_keys.remove(name)
        if name not in self.held_keys:
            self.input.release_key(name)

    def hold_button(self, name: str) -> None:
        if name not in self.held_buttons:
            self.input.press_button(name)
        self.held_buttons.append(name)

    def unhold_button(self, name: str) -> None:
        if name in self.held_buttons:
            self.held_buttons.remove(name)
        if name not in self.held_buttons:
            self.input.release_button(name)

    # Engine services ------------------------------------------------------

    def move_pointer(self, points: Sequence[tuple[float, float]], duration_ms: float,
                     dts: Sequence[float] | None = None) -> bool:
        self.pointer_moves.append((list(points), duration_ms, list(dts) if dts is not None else None))
        if not self.pointer_stop:
            self.input.move(*points[-1])
        return self.pointer_stop

    def reactivate_context(self) -> None:
        pass

    def enter_window(self, row: Any) -> Any:
        raise NotImplementedError

    def row_state(self, row_id: int) -> dict:
        raise NotImplementedError

    def run_command(self, command: str, wait: bool, timeout_ms: float = 0) -> int | None:
        raise NotImplementedError
