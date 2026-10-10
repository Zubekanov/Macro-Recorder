"""The program-counter playback engine.

``Player.play()`` walks a snapshot of the document's rows on the caller's
thread.  It owns the program counter, the window context, repeat counters,
breakpoints, timing, stop and pause; each row's ``ActionSpec.play`` does the
row's own work through the :class:`Player`, which is also the
``PlayContext`` handed to it.  ``stop()`` and ``resume()`` may be called from
any thread.
"""

from __future__ import annotations

import logging
import math
import os
import random
import re
import subprocess
import threading
import time
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from macro_recorder.engine import expressions, paths, processes
from macro_recorder.model.actions.variables import DYNAMIC_VALUES
from macro_recorder.model.catalogue import (
    END_TARGET, START_TARGET, Abort, Flow, Jump, spec_for,
)
from macro_recorder.model.document import MacroDocument, Row
from macro_recorder.model.events import EventType
from macro_recorder.model.protocols import (
    Clock, InputBackend, PlayerHost, Rect, Screen, StopKeyWatcher, WindowInfo, WindowManager,
)

log = logging.getLogger(__name__)

STOP_KEY = "Escape"
WINDOW_POLL_MS = 250
LATE_S = 0.05                   # schedule slack before path points are skipped
SLEEP_SLICE_S = 0.05            # longest uninterrupted sleep, so stop stays responsive
BURST_ROWS = 10_000             # rows per second without a wait before throttling
BURST_SLEEP_S = 0.001
REPEAT_COUNTER = "counter"      # row_state key a repeat row keeps its pass counter in
# Rows whose length the schedule cannot know; the clock resets after them.
# Window rows, message boxes and waited commands reset where they happen.
UNSCHEDULED = frozenset({EventType.WAIT_PIXEL, EventType.OCR_READ,
                         EventType.MATCH_IMAGE, EventType.MATCH_TEXT})

Outcome = Flow | Jump | Abort
_ROW_PREFIX = re.compile(r"^Row \d+: ")


class PlaybackError(RuntimeError):
    """A row cannot run; playback aborts with "Row N: message"."""


@dataclass(frozen=True)
class IndexJump(Jump):
    """A jump to a row index, for targets that have no label (the next window row)."""
    index: int = 0


@dataclass(frozen=True)
class WindowContext:
    """The window the current group plays in."""
    row_id: int
    window: WindowInfo
    recorded: Rect | None           # recorded client rect

    @property
    def delta(self) -> tuple[int, int]:
        """Current client origin minus recorded client origin."""
        if self.recorded is None:
            return (0, 0)
        return (self.window.client_rect.left - self.recorded.left,
                self.window.client_rect.top - self.recorded.top)


class _SystemClock:
    """Monotonic time whose sleep wakes early when playback stops."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        self._stop.wait(max(0.0, seconds))


class Variables(MutableMapping):
    """User variables plus the read-only dynamic values.

    Iteration and ``len`` cover user variables only; :meth:`snapshot` adds
    the dynamic values.  Assigning or deleting a dynamic name raises
    PlaybackError.
    """

    def __init__(self, providers: Mapping[str, Callable[[], Any]]) -> None:
        self._values: dict[str, Any] = {}
        self._providers = dict(providers)

    def __getitem__(self, key: str) -> Any:
        if key in self._values:
            return self._values[key]
        if key in self._providers:
            return self._providers[key]()
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self._providers:
            raise PlaybackError("%s is a read-only dynamic value" % key)
        self._values[key] = value

    def __delitem__(self, key: str) -> None:
        if key in self._providers:
            raise PlaybackError("%s is a read-only dynamic value" % key)
        del self._values[key]

    def __contains__(self, key: object) -> bool:
        return key in self._values or key in self._providers

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def snapshot(self) -> dict[str, Any]:
        """User variables and the current dynamic values."""
        out = {name: provider() for name, provider in self._providers.items()}
        out.update(self._values)
        return out


class _HostProxy:
    """The host as actions see it: ``ask`` releases held input and restores it after."""

    def __init__(self, player: "Player", host: PlayerHost) -> None:
        self._player = player
        self._host = host

    def on_row(self, row_id: int) -> None:
        self._host.on_row(row_id)

    def on_log(self, line: str) -> None:
        self._host.on_log(line)

    def on_paused(self, row_id: int, variables: Mapping[str, Any]) -> None:
        self._host.on_paused(row_id, variables)

    def on_finished(self, error: str | None) -> None:
        self._host.on_finished(error)

    def ask(self, title: str, text: str, buttons: str) -> str | None:
        return self._player._ask(title, text, buttons)


class Player:
    """Plays a document once (with its repeat passes).  Implements PlayContext.

    A Player is single-use: create a new one for every run.
    """

    def __init__(self, document: MacroDocument, settings: Any, host: PlayerHost,
                 input: InputBackend, windows: WindowManager, screen: Screen,
                 clock: Clock | None = None, stop_key_watcher: StopKeyWatcher | None = None) -> None:
        speed = float(settings.playback.speed)
        if not speed > 0:
            raise ValueError("Speed must be above 0, got %r" % settings.playback.speed)
        if int(settings.playback.repeat) < 0:
            raise ValueError("Repeat must be 0 or more, got %r" % settings.playback.repeat)
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._clock: Clock = clock if clock is not None else _SystemClock(self._stop)
        self._host = host
        self._watcher = stop_key_watcher
        self._played = False
        self.rng = random.Random()

        # Snapshot of the program.
        self._rows: tuple[Row, ...] = tuple(document.rows)
        self._plays = tuple(document.plays(r) for r in self._rows)
        self._path_mode = document.path_mode
        self._index = {r.id: i for i, r in enumerate(self._rows)}
        self._labels: dict[str, int] = {}
        for i, r in enumerate(self._rows):
            if r.label and r.label.lower() not in self._labels:
                self._labels[r.label.lower()] = i
        self._governing = self._governing_rows()
        self._repeats = self._repeat_spans()

        # PlayContext attributes.
        self.row_id = 0
        self.speed = speed
        self.input = input
        self.screen = screen
        self.windows = windows
        self.host = _HostProxy(self, host)
        self.settings = settings
        self.document_dir = os.path.dirname(os.path.abspath(document.path)) if document.path else None
        providers = self._dynamic_providers()
        missing = [name for name, _ in DYNAMIC_VALUES if name not in providers]
        if missing:
            raise RuntimeError("No provider for dynamic values: %s" % ", ".join(missing))
        self.variables = Variables(providers)

        # Run state.
        self._pc = 0
        self._iteration = 0
        self._executed = 0
        self._context: WindowContext | None = None
        self._row_states: dict[int, dict] = {}
        self._launched: set[int] = set()
        self._missing: set[int] = set()
        self._held_keys: list[str] = []         # one entry per hold, so holds nest
        self._held_buttons: list[str] = []
        self._children: list[subprocess.Popen] = []     # unwaited exec_program commands
        self._start = 0.0
        self._paused_s = 0.0
        self._due = 0.0
        self._burst_start = 0.0
        self._burst_rows = 0
        self._slept = False

    # ------------------------------------------------------------ public API

    def play(self) -> None:
        """Run every pass, then release held input and report ``on_finished``."""
        if self._played:
            raise RuntimeError("A Player plays once; create a new one")
        self._played = True
        error: str | None = None
        try:
            if self._watcher is not None:
                self._watcher.start(STOP_KEY, self.stop)
            self._start = self._clock.now()
            self._reset_schedule()
            self._burst_start = self._start
            self._run()
        except PlaybackError as e:
            error = str(e)
        except Exception as e:      # noqa: BLE001 - reported to the host
            log.exception("Playback failed")
            error = str(e) or type(e).__name__
        finally:
            if self._watcher is not None:
                try:
                    self._watcher.stop()
                except Exception:   # noqa: BLE001
                    log.exception("Stopping the stop-key watcher failed")
            try:
                self.input.release_all()
            except Exception:       # noqa: BLE001
                log.exception("Releasing held input failed")
            self._held_keys.clear()
            self._held_buttons.clear()
            if self._children:
                try:
                    processes.terminate(self._children)
                except Exception:   # noqa: BLE001
                    log.exception("Ending exec_program commands failed")
            self._host.on_finished(error)

    def stop(self) -> None:
        """Stop playback from any thread; wakes every wait, including a pause."""
        self._stop.set()
        self._resume.set()

    def resume(self) -> None:
        """Continue after a breakpoint."""
        self._resume.set()

    # ------------------------------------------------------------- the loop

    def _run(self) -> None:
        repeat = int(self.settings.playback.repeat)
        while not self._stop.is_set():
            self._iteration += 1
            self._row_states.clear()
            self._launched.clear()
            self._missing.clear()
            self._context = None
            if self._run_pass() is Flow.STOP:
                return
            if repeat > 0 and self._iteration >= repeat:
                return

    def _run_pass(self) -> Flow:
        """One pass over the program.  Returns STOP to end the whole run."""
        self._pc = 0
        while self._pc < len(self._rows):
            if self._stop.is_set():
                return Flow.STOP
            index = self._pc
            row = self._rows[index]
            if not self._plays[index]:
                self._pc += 1
                continue
            outcome = self._step(index, row)
            if outcome is Flow.STOP or self._stop.is_set():
                return Flow.STOP
            if isinstance(outcome, Abort):
                raise PlaybackError(self._row_message(index, outcome.message))
            if isinstance(outcome, Jump):
                self._pc = self._jump_index(outcome, index)
            else:
                self._pc = index + 1
            self._throttle()
        return Flow.NEXT

    def _step(self, index: int, row: Row) -> Outcome:
        """Window context, breakpoint, delay, then the row's own play.

        The context comes first so a breakpoint on the first row of a group
        pauses in, and re-activates, that group's window.
        """
        self.row_id = row.id
        try:
            self._host.on_row(row.id)
            outcome = self._ensure_context(index, row)
            if outcome is not Flow.NEXT:
                return outcome
            if row.breakpoint and self._pause(row):
                return Flow.STOP
            delay_ms = max(row.delay_ms, self.settings.playback.min_spacing_ms)
            if self._scheduled_wait(delay_ms / 1000.0 / self.speed):
                return Flow.STOP
            self._executed += 1
            spec = spec_for(row)
            self._host.on_log("[%d] %s" % (index + 1, self._describe(spec, row)))
            outcome = spec.play(self, row)
            if row.type in UNSCHEDULED:
                self._reset_schedule()
            return outcome
        except (expressions.ExpressionError, ArithmeticError, PlaybackError, processes.ProcessError) as e:
            return Abort(str(e))
        except Exception as e:      # noqa: BLE001 - a row bug aborts the run with its number
            log.exception("Row %d failed", index + 1)
            return Abort("%s: %s" % (type(e).__name__, e))

    @staticmethod
    def _describe(spec: Any, row: Row) -> str:
        try:
            return spec.describe(row)
        except Exception:           # noqa: BLE001 - a bad value must not stop playback
            return spec.name

    @staticmethod
    def _row_message(index: int, message: str) -> str:
        """``message`` prefixed with "Row N: " unless it already names a row."""
        if _ROW_PREFIX.match(message):
            return message
        return "Row %d: %s" % (index + 1, message)

    def _jump_index(self, jump: Jump, index: int) -> int:
        if isinstance(jump, IndexJump):
            return jump.index
        target = jump.target or ""
        if target.lower() == START_TARGET.lower():
            return 0
        if target.lower() == END_TARGET.lower():
            return len(self._rows)
        try:
            return self._labels[target.lower()]
        except KeyError:
            raise PlaybackError(self._row_message(index, "no row is labelled %r" % target)) from None

    def _throttle(self) -> None:
        """Sleep 1 ms per row once more than 10 000 rows run in a second without a wait."""
        now = self._clock.now()
        if self._slept or now - self._burst_start >= 1.0:
            self._slept = False
            self._burst_start = now
            self._burst_rows = 0
        self._burst_rows += 1
        if self._burst_rows > BURST_ROWS:
            self._clock.sleep(BURST_SLEEP_S)

    # ------------------------------------------------------- program shape

    def _governing_rows(self) -> tuple[Row | None, ...]:
        """For each index, the window row whose group it is in, or None."""
        out: list[Row | None] = []
        current: Row | None = None
        for r in self._rows:
            if r.is_window:
                current = r if r.enabled else None
            out.append(current)
        return tuple(out)

    def _repeat_spans(self) -> tuple[tuple[int, int, int], ...]:
        """(body start, repeat row index, repeat row id) for each repeat with a label target."""
        out = []
        for i, r in enumerate(self._rows):
            if r.type != EventType.REPEAT:
                continue
            target = r.event.get("target")
            start = self._labels.get(target.lower()) if isinstance(target, str) else None
            if start is not None and start <= i:
                out.append((start, i, r.id))
        return tuple(out)

    def _next_window_index(self, index: int) -> int:
        """The next window row (enabled or not) after ``index``, or the end."""
        for i in range(index + 1, len(self._rows)):
            if self._rows[i].is_window:
                return i
        return len(self._rows)

    # ------------------------------------------------------- window handling

    def _ensure_context(self, index: int, row: Row) -> Outcome:
        """Make the window context match the row's group before it plays."""
        gov = self._governing[index]
        if gov is None:
            self._context = None
            return Flow.NEXT
        if gov.id == row.id or (self._context is not None and self._context.row_id == gov.id):
            return Flow.NEXT
        gov_index = self._index[gov.id]
        if gov.id in self._missing:
            self._log_row(gov_index, "skipped: window %r was not found earlier" % gov.event["window"])
            return self._skip_group(gov_index)
        return self.enter_window(gov)

    def _skip_group(self, window_index: int) -> Jump:
        nxt = self._next_window_index(window_index)
        if nxt >= len(self._rows):
            return Jump(END_TARGET)
        return IndexJump("row %d" % (nxt + 1), nxt)

    def enter_window(self, row: Row) -> Outcome:
        """Find, launch if needed, restore and activate a window row's window.

        On success the window becomes the context and NEXT is returned; a
        missing window follows the row's (or the setting's) policy.
        """
        ev = row.event
        index = self._index[row.id]
        title = ev["window"] or ""
        settle = int(self.settings.playback.window_settle_ms)
        self._context = None
        window = self._find_window(row, index)
        if self._stop.is_set():
            return Flow.STOP
        if window is not None:
            window = self._place_window(row, index, window, settle)
            if self._stop.is_set():
                return Flow.STOP
        if window is None:
            return self._window_missing(row, index)
        self._missing.discard(row.id)
        client = ev["client_rect"]
        recorded = Rect(*client) if client else None
        self._context = WindowContext(row.id, window, recorded)
        self._reset_schedule()
        if not title:
            self._log_row(index, "using the active window %r" % window.title)
        return Flow.NEXT

    def _find_window(self, row: Row, index: int) -> WindowInfo | None:
        """Poll for the window every 250 ms until the timeout; launch once per pass."""
        ev = row.event
        title = ev["window"] or ""
        if not title:
            return self.windows.active()
        timeout_ms = self.resolve(ev["timeout_ms"], self.settings.playback.window_timeout_ms)
        deadline = self._clock.now() + max(0.0, timeout_ms) / 1000.0
        first = True
        while True:
            try:
                window = self.windows.find(title, ev["match_mode"])
            except (ValueError, re.error) as e:     # bad regex
                raise PlaybackError(self._row_message(index, "bad window pattern %r: %s" % (title, e))) from e
            if window is not None:
                if not first and self._sleep(self.settings.playback.window_settle_ms / 1000.0):
                    return None
                return window
            if first and ev["launch"] and row.id not in self._launched:
                self._launched.add(row.id)
                self._log_row(index, "window %r not found, launching: %s" % (title, ev["launch"]))
                try:
                    processes.run(ev["launch"], self.document_dir, False, 0, self._stop)
                except processes.ProcessError as e:
                    raise PlaybackError(self._row_message(index, str(e))) from e
            first = False
            remaining = deadline - self._clock.now()
            if remaining <= 0:
                return None
            if self._sleep(min(WINDOW_POLL_MS / 1000.0, remaining)):
                return None

    def _place_window(self, row: Row, index: int, window: WindowInfo, settle: int) -> WindowInfo | None:
        """Restore the recorded state (maximised or frame), then activate."""
        ev = row.event
        if ev["maximised"]:
            if not window.maximised:
                window = self.windows.set_maximised(window, True, settle) or window
        elif self._restore_rect(ev["restore_rect"]) and ev["rect"]:
            rect = Rect(*ev["rect"])
            if self._on_work_area(rect):
                if window.maximised:
                    window = self.windows.set_maximised(window, False, settle) or window
                moved = self.windows.move(window, rect, settle)
                if moved is not None:
                    window = moved
                if (window.rect.width, window.rect.height) != (rect.width, rect.height):
                    self._log_row(index, "window %r is %dx%d, recorded %dx%d" % (
                        window.title, window.rect.width, window.rect.height, rect.width, rect.height))
            else:
                self._log_row(index, "recorded position of %r is off-screen; not moving it" % window.title)
        return self.windows.activate(window, settle)

    def _restore_rect(self, value: str) -> bool:
        if value == "yes":
            return True
        if value == "no":
            return False
        return bool(self.settings.playback.restore_rect)

    def _on_work_area(self, rect: Rect) -> bool:
        for m in self.windows.monitors():
            wa = m.work_area
            if wa.left <= rect.left and wa.top <= rect.top and rect.right <= wa.right and rect.bottom <= wa.bottom:
                return True
        return False

    def _window_missing(self, row: Row, index: int) -> Outcome:
        ev = row.event
        self._missing.add(row.id)
        policy = ev["on_missing"]
        if policy == "default":
            policy = self.settings.playback.on_missing
        title = ev["window"] or "(active window)"
        if policy == "halt":
            return Abort(self._row_message(index, "window %r was not found" % title))
        if policy == "goto" and ev["target"]:
            self._log_row(index, "window %r not found, going to %s" % (title, ev["target"]))
            return Jump(ev["target"])
        self._log_row(index, "window %r not found, skipping its rows" % title)
        return self._skip_group(index)

    def reactivate_context(self) -> None:
        """Re-focus the context window (no launch, no move) and refresh the context."""
        ctx = self._context
        if ctx is None:
            return
        window = self.windows.refresh(ctx.window)
        if window is not None:
            window = self.windows.activate(window, int(self.settings.playback.window_settle_ms))
        if window is None:
            self._log_row(self._index.get(ctx.row_id, 0), "could not re-activate %r" % ctx.window.title)
            return
        self._context = WindowContext(ctx.row_id, window, ctx.recorded)

    # ----------------------------------------------------- pause and dialogs

    def _pause(self, row: Row) -> bool:
        """Pause at a breakpoint until resumed.  True when stopped meanwhile."""
        self._release_held()
        self._resume.clear()
        started = self._clock.now()
        self._host.on_paused(row.id, self.variables.snapshot())
        self._resume.wait()
        self._paused_s += self._clock.now() - started
        if self._stop.is_set():
            return True
        self._restore_after_interruption()
        return False

    def _ask(self, title: str, text: str, buttons: str) -> str | None:
        self._release_held()
        answer = self._host.ask(title, text, buttons)
        if self._stop.is_set():
            return None
        self._restore_after_interruption()
        return answer

    def _release_held(self) -> None:
        """Release held buttons and keys but remember them."""
        for b in dict.fromkeys(self._held_buttons):
            self.input.release_button(b)
        for k in dict.fromkeys(self._held_keys):
            self.input.release_key(k)

    def _restore_after_interruption(self) -> None:
        self.reactivate_context()
        for k in dict.fromkeys(self._held_keys):
            self.input.press_key(k)
        for b in dict.fromkeys(self._held_buttons):
            self.input.press_button(b)
        self._reset_schedule()

    def hold_key(self, name: str) -> None:
        """Press key ``name`` unless a hold already has it down, and count the hold."""
        if name not in self._held_keys:
            self.input.press_key(name)
        self._held_keys.append(name)

    def unhold_key(self, name: str) -> None:
        """Drop one hold of ``name``; release the key when no hold remains."""
        if name in self._held_keys:
            self._held_keys.remove(name)
        if name not in self._held_keys:
            self.input.release_key(name)

    def hold_button(self, name: str) -> None:
        """Press button ``name`` unless a hold already has it down, and count the hold."""
        if name not in self._held_buttons:
            self.input.press_button(name)
        self._held_buttons.append(name)

    def unhold_button(self, name: str) -> None:
        """Drop one hold of ``name``; release the button when no hold remains."""
        if name in self._held_buttons:
            self._held_buttons.remove(name)
        if name not in self._held_buttons:
            self.input.release_button(name)

    # ---------------------------------------------------------------- timing

    def _sleep(self, seconds: float) -> bool:
        """Sleep in slices, waking for stop.  True when stopped."""
        end = self._clock.now() + seconds
        while not self._stop.is_set():
            remaining = end - self._clock.now()
            if remaining <= 0:
                return False
            self._clock.sleep(min(remaining, SLEEP_SLICE_S))
            self._slept = True
        return True

    def _reset_schedule(self) -> None:
        self._due = self._clock.now()

    def _align_schedule(self) -> None:
        """Forget lateness beyond the slack, so an unscheduled step does not eat later waits."""
        now = self._clock.now()
        if now - self._due > LATE_S:
            self._due = now

    def _sleep_until_due(self) -> bool:
        return self._sleep(self._due - self._clock.now())

    def _scheduled_wait(self, seconds: float) -> bool:
        self._align_schedule()
        self._due += max(0.0, seconds)
        return self._sleep_until_due()

    def sleep(self, ms: float, scaled: bool = True) -> bool:
        """Wait ``ms`` (divided by speed when ``scaled``).  True when stopped."""
        if self._stop.is_set():
            return True
        if ms <= 0:
            return False
        if scaled:
            return self._scheduled_wait(ms / 1000.0 / self.speed)
        stopped = self._sleep(ms / 1000.0)
        self._reset_schedule()
        return stopped

    def now(self) -> float:
        """The engine clock, in seconds."""
        return self._clock.now()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def move_pointer(self, points: Sequence[tuple[float, float]], duration_ms: float,
                     dts: Sequence[float] | None = None) -> bool:
        """Move through screen points on the schedule clock, skipping points when late."""
        if not points:
            return self._stop.is_set()
        if dts is not None and self._path_mode == "recorded":
            offsets = []
            total = 0.0
            for dt in dts:
                total += max(0.0, float(dt)) / self.speed
                offsets.append(total)
            samples = [(t, float(p[0]), float(p[1])) for t, p in zip(offsets, points)]
        else:
            mode = "linear" if self._path_mode == "recorded" else self._path_mode
            start = self.input.position()
            via = points if mode == "spline" else None
            generated = paths.generate(mode, start, points[-1], max(0.0, duration_ms) / self.speed,
                                       self.rng, via)
            samples = [(t / 1000.0, x, y) for t, x, y in generated]
        self._align_schedule()
        base = self._due
        last = len(samples) - 1
        for i, (t, x, y) in enumerate(samples):
            self._due = base + t
            late = self._clock.now() - self._due
            if late > LATE_S and i < last:
                continue
            if self._sleep_until_due():
                return True
            self.input.move(x, y)
        return self._stop.is_set()

    # --------------------------------------------------------- coordinates

    def resolve(self, value: Any, default: float = 0.0) -> float:
        """A number or an expression as a float; None or blank gives ``default``."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return float(default)
        if isinstance(value, (int, float)):
            return float(value)
        return float(expressions.resolve_number(value, self.variables))

    def resolve_int(self, value: Any, default: int = 0) -> int:
        return int(round(self.resolve(value, default)))

    def _origin(self) -> tuple[int, int]:
        if self._context is None:
            return (0, 0)
        client = self._context.window.client_rect
        return (client.left, client.top)

    def coord(self, x: Any, y: Any) -> tuple[float, float]:
        """Numbers get the window delta; expressions are relative to the client origin."""
        delta = self._context.delta if self._context is not None else (0, 0)
        origin = self._origin()
        out = []
        for value, d, o in ((x, delta[0], origin[0]), (y, delta[1], origin[1])):
            if value is None or (isinstance(value, str) and not value.strip()):
                raise PlaybackError("a coordinate is blank")
            if isinstance(value, (int, float)):
                out.append(float(value) + d)
            else:
                out.append(self.resolve(value) + o)
        return (out[0], out[1])

    def to_frame(self, x: float, y: float) -> tuple[float, float]:
        """Screen pixels to the frame expressions use."""
        ox, oy = self._origin()
        return (float(x) - ox, float(y) - oy)

    def region(self, values: Mapping[str, Any]) -> Rect:
        """Corners (frame applied) or a whole monitor (1-based) as a screen rect."""
        monitor = values.get("monitor")
        if monitor is not None and not (isinstance(monitor, str) and not monitor.strip()):
            number = self.resolve_int(monitor)
            for m in self.screen.monitors():
                if m.index == number:
                    return m.rect
            raise PlaybackError("there is no monitor %d" % number)
        x1, y1 = self.coord(values.get("x"), values.get("y"))
        x2, y2 = self.coord(values.get("x2"), values.get("y2"))
        left, top = int(round(min(x1, x2))), int(round(min(y1, y2)))
        right, bottom = int(round(max(x1, x2))), int(round(max(y1, y2)))
        return Rect(left, top, right - left, bottom - top)

    def render(self, template: str) -> str:
        return expressions.render_template(template, self.variables)

    # ------------------------------------------------------- misc context

    def log(self, text: str) -> None:
        self._host.on_log("[%d] %s" % (self._index.get(self.row_id, -1) + 1, text))

    def _log_row(self, index: int, text: str) -> None:
        self._host.on_log("[%d] %s" % (index + 1, text))

    def row_state(self, row_id: int) -> dict:
        return self._row_states.setdefault(row_id, {})

    def run_command(self, command: str, wait: bool, timeout_ms: float = 0) -> int | None:
        """Run a shell command in the macro's folder (or $HOME).  Not scaled by speed."""
        try:
            return processes.run(command, self.document_dir, wait, timeout_ms, self._stop,
                                 self._children)
        finally:
            if wait:
                self._reset_schedule()

    # ------------------------------------------------------- dynamic values

    def _dynamic_providers(self) -> dict[str, Callable[[], Any]]:
        return {
            "rt_ms": lambda: (self._clock.now() - self._start - self._paused_s) * 1000.0,
            "op_num": lambda: self._pc + 1,
            "ex_num": lambda: self._executed,
            "iteration": lambda: self._iteration,
            "loop": self._loop_value,
            "mouse_x": lambda: self.to_frame(*self.input.position())[0],
            "mouse_y": lambda: self.to_frame(*self.input.position())[1],
            "win_x": lambda: self._client_rect().left,
            "win_y": lambda: self._client_rect().top,
            "win_w": lambda: self._client_rect().width,
            "win_h": lambda: self._client_rect().height,
            "timestamp_ms": lambda: math.floor(time.time() * 1000),
            "random": lambda: self.rng.random(),
        }

    def _client_rect(self) -> Rect:
        if self._context is not None:
            return self._context.window.client_rect
        width, height = self.windows.screen_size()
        return Rect(0, 0, width, height)

    def _loop_value(self) -> int:
        """Pass of the innermost repeat whose body holds the current row; 0 outside any."""
        best: tuple[int, int] | None = None
        for start, end, row_id in self._repeats:
            if start <= self._pc <= end:
                size = end - start
                if best is None or size < best[0]:
                    best = (size, row_id)
        if best is None:
            return 0
        return int(self._row_states.get(best[1], {}).get(REPEAT_COUNTER, 0)) + 1
