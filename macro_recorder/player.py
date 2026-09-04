"""Macro playback engine using pynput controllers."""

from __future__ import annotations

import logging
import random
import subprocess
import threading
import time
from typing import Optional

from pynput import keyboard, mouse

from macro_recorder.event_types import EventType
from macro_recorder.expressions import (
    ExpressionError,
    evaluate,
    evaluate_condition,
    is_valid_variable_name,
    render_template,
    resolve_number,
)
from macro_recorder.macro import MacroEvent, MacroGroup, deserialize_button, deserialize_key
from macro_recorder.matching import MatchError, find_image, match_text, monitor_region
from macro_recorder.ocr import OcrError, read_region
from macro_recorder.window_manager import get_window_manager

log = logging.getLogger(__name__)

# Reserved jump targets selectable in the goto dropdown.
GOTO_START = "Start of program"
GOTO_END = "End of program"

# Safety cap on instructions executed by the control-flow engine, so a macro
# with an unintended infinite loop cannot spin forever if the stop key is
# never pressed.
_MAX_CONTROL_FLOW_STEPS = 1_000_000

# How often the wait-for-match actions re-check the screen while polling.
_MATCH_POLL_INTERVAL = 0.3

# Read-only dynamic values exposed to expressions as pseudo-variables, computed
# live at playback time.  Usable anywhere an expression is (Set Variable,
# conditions, coordinates, Type Text templates).  (name, description) pairs; the
# UI lists these and the player supplies a matching provider for each name.
DYNAMIC_VALUES: tuple[tuple[str, str], ...] = (
    ("rt_ms", "ms since playback started"),
    ("op_num", "current instruction (row) number"),
    ("ex_num", "instructions executed so far"),
    ("iteration", "current repeat pass (1-based)"),
    ("mouse_x", "current mouse X position"),
    ("mouse_y", "current mouse Y position"),
    ("timestamp_ms", "wall-clock Unix epoch ms"),
    ("random", "new random float in [0,1) per read"),
)


def _fmt_log_value(value) -> str:
    """Compact value rendering for log lines: ints without a trailing ``.0``,
    strings quoted, everything else via ``str``."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        return '"%s"' % value
    return str(value)


class _VariableStore(dict):
    """User variables overlaid with read-only dynamic playback values.

    Behaves like a normal dict for user assignments, but reading a dynamic name
    (see DYNAMIC_VALUES) returns a freshly computed value from its provider.
    Dynamic names are reserved: assigning to one raises MacroExecutionError so a
    macro cannot shadow or clobber them.  ``clear()`` only drops user keys
    (providers are kept on the instance), so the store can be reused across
    repeats.
    """

    def __init__(self, providers: dict) -> None:
        super().__init__()
        self._providers = providers

    def __setitem__(self, key, value) -> None:
        if key in self._providers:
            raise MacroExecutionError(
                "Cannot assign to %r: it is a reserved dynamic value." % key)
        super().__setitem__(key, value)

    def __contains__(self, key: object) -> bool:
        return super().__contains__(key) or key in self._providers

    def __getitem__(self, key):
        if super().__contains__(key):
            return super().__getitem__(key)
        provider = self._providers.get(key)
        if provider is not None:
            return provider()
        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class WindowNotFoundError(RuntimeError):
    """Raised when a target window cannot be located within the timeout and
    the player is configured to halt rather than skip."""


class MacroExecutionError(RuntimeError):
    """Raised to abort playback when an expression cannot be evaluated — an
    invalid expression, a bad variable name, or an uninitialised variable."""


class Player:
    """Replays MacroGroups using pynput mouse and keyboard controllers.

    Usage
    -----
    player = Player(speed=1.0, repeat=1, stop_key="Key.esc")
    player.play(groups)   # blocks until playback ends or stop key is pressed

    Window context
    --------------
    Each group may carry a window title and recorded rectangle.  Before a
    windowed group is replayed, the target window is located (by substring or
    regex, per the group's match_mode), moved/resized to its recorded
    rectangle, and brought to the foreground; the group's relative mouse
    coordinates are then converted back to absolute screen coordinates.
    If the window cannot be found within `window_timeout` seconds and the group
    has a launch command, it is run once and the wait repeats.  If the window is
    still missing, the group is skipped (on_missing_window="skip") or playback
    halts with a descriptive error (on_missing_window="halt").

    Timing
    ------
    Within each group, events fire at their recorded timestamps (relative to
    the group's first event) scaled by 1/speed.  stop_event.wait(timeout) is
    used instead of time.sleep so the stop key interrupts any pause instantly.

    Safety
    ------
    A _dispatching flag is set around every controller.press/release call so
    the stop-key listener ignores synthetic keyboard events.  All held keys and
    buttons are released in a try/finally block, so interrupted playback never
    leaves inputs stuck down.
    """

    def __init__(
        self,
        speed: float = 1.0,
        repeat: int = 1,
        stop_key: str = "Key.esc",
        window_timeout: float = 5.0,
        on_missing_window: str = "skip",
        on_active_event=None,
        on_log=None,
    ) -> None:
        """Create a Player.

        Parameters
        ----------
        speed:
            Playback speed multiplier.  0.5 = half speed, 2.0 = double speed.
        repeat:
            Number of times to replay the macro.  0 means repeat indefinitely.
        stop_key:
            Serialized pynput key string that aborts playback mid-run.
        window_timeout:
            Seconds to wait for a target window to appear before applying the
            missing-window policy.
        on_missing_window:
            "skip" to log a warning and skip the group, or "halt" to stop
            playback and raise WindowNotFoundError.
        on_active_event:
            Optional callback invoked (from the playback thread) with the
            MacroEvent the player is about to execute or dwell on.  The UI
            highlights the corresponding row; it is expected to throttle repaints
            so instant instructions that are immediately superseded don't flicker.
        on_log:
            Optional callback invoked (from the playback thread) with a formatted
            log line per executed instruction, e.g.
            ``"[3] Assigned x = 13  [y + z]"`` — the ``[instruction position]``
            then a description.  Called once per row (bundled events collapse to
            one line).
        """
        if speed <= 0:
            raise ValueError("speed must be > 0, got %r" % speed)
        if repeat < 0:
            raise ValueError("repeat must be >= 0, got %r" % repeat)
        if on_missing_window not in ("skip", "halt"):
            raise ValueError("on_missing_window must be 'skip' or 'halt', got %r"
                             % on_missing_window)

        self._speed = speed
        self._repeat = repeat
        self._stop_key_obj = deserialize_key(stop_key)
        self._window_timeout = window_timeout
        self._on_missing_window = on_missing_window
        self._on_active_event = on_active_event
        self._on_log = on_log
        self._uses_markers = False   # set per play(): True if rows carry instr markers
        self._wm = get_window_manager()

        self._mouse_ctrl = mouse.Controller()
        self._kb_ctrl = keyboard.Controller()

        self._stop_event = threading.Event()
        self._held_keys: set = set()
        self._held_buttons: set = set()
        self._dispatching: bool = False
        self._launched: set[int] = set()   # id(group) already launched this run

        # Live playback counters backing the dynamic values (reset in play()).
        self._play_start_perf: float = time.perf_counter()
        self._current_instr: int = 0
        self._instr_executed: int = 0
        self._iteration: int = 0

        # Variable store, evaluated at execution time and shared across the whole
        # play() run (so counters can accumulate over repeats).  Dynamic values
        # are overlaid as read-only pseudo-variables computed on access.
        self._variables: dict[str, object] = _VariableStore({
            "rt_ms": lambda: (time.perf_counter() - self._play_start_perf) * 1000.0,
            "op_num": lambda: self._current_instr,
            "ex_num": lambda: self._instr_executed,
            "iteration": lambda: self._iteration,
            "mouse_x": lambda: float(self._mouse_ctrl.position[0]),
            "mouse_y": lambda: float(self._mouse_ctrl.position[1]),
            "timestamp_ms": lambda: time.time() * 1000.0,
            "random": random.random,
        })

        # Dispatch table: event type -> handler(event, offset_x, offset_y).
        self._handlers = {
            EventType.MOUSE_MOVE: self._handle_mouse_move,
            EventType.MOUSE_MOVE_TIMED: self._handle_mouse_move_timed,
            EventType.MOUSE_CLICK: self._handle_mouse_click,
            EventType.MOUSE_SCROLL: self._handle_mouse_scroll,
            EventType.KEY_PRESS: self._handle_key_press,
            EventType.KEY_RELEASE: self._handle_key_release,
            EventType.TYPE_TEXT: self._handle_type_text,
            EventType.VAR_SET: self._handle_var_set,
            EventType.OCR_READ: self._handle_ocr,
            EventType.MATCH_IMAGE: self._handle_match_image,
            EventType.MATCH_TEXT: self._handle_match_text,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Signal the player to stop if it is currently playing."""
        self._stop_event.set()

    def play(self, groups: list[MacroGroup]) -> None:
        """Replay groups.  Blocks until all repeats complete or stop key pressed.

        Parameters
        ----------
        groups:
            Ordered list of MacroGroup objects as returned by Recorder.start()
            or load_macro().

        Raises
        ------
        WindowNotFoundError:
            If a target window is missing and on_missing_window == "halt".
        """
        self._stop_event.clear()
        self._held_keys.clear()
        self._held_buttons.clear()
        self._launched.clear()
        self._variables.clear()
        self._play_start_perf = time.perf_counter()
        self._current_instr = 0
        self._instr_executed = 0
        self._iteration = 0
        # Rows tagged with instruction numbers (UI playback) let logging and
        # op_num collapse bundled events to one instruction; without them (e.g.
        # CLI), every event counts as its own instruction.
        self._uses_markers = any(
            getattr(ev, "instr", None) is not None
            for g in groups for ev in g.events
        )

        # Linear timed playback is used unless the macro contains jumps, in
        # which case absolute-time scheduling no longer applies and a
        # program-counter engine takes over.  Existing (jump-free) macros are
        # unaffected.
        has_control_flow = any(
            ev.type in (EventType.GOTO, EventType.GOTO_IF)
            for g in groups for ev in g.events
        )
        play_pass = self._play_with_control_flow if has_control_flow else self._play_all_groups

        stop_listener = keyboard.Listener(on_press=self._on_stop_key)
        stop_listener.start()

        try:
            iteration = 0
            while not self._stop_event.is_set():
                self._iteration = iteration + 1   # 1-based pass number (dynamic value)
                play_pass(groups)
                if self._stop_event.is_set():
                    break
                iteration += 1
                if self._repeat > 0 and iteration >= self._repeat:
                    break
        finally:
            self._cleanup()
            stop_listener.stop()
            stop_listener.join()

    # ------------------------------------------------------------------
    # Internal: stop-key listener
    # ------------------------------------------------------------------

    def _on_stop_key(self, key) -> Optional[bool]:
        """Keyboard listener callback used only for stop-key detection.

        Ignores events while _dispatching is True so synthetic keys emitted
        by the playback controller do not falsely trigger a stop.
        """
        if self._dispatching:
            return None  # synthetic event from our own controller — ignore
        if key == self._stop_key_obj:
            self._stop_event.set()
            return False  # stops this listener
        return None

    # ------------------------------------------------------------------
    # Internal: group playback
    # ------------------------------------------------------------------

    def _play_all_groups(self, groups: list[MacroGroup]) -> None:
        """Play every group once, preparing each window before its events."""
        for group in groups:
            if self._stop_event.is_set():
                return
            if group.window:
                if not self._prepare_window(group):
                    if self._stop_event.is_set():
                        return
                    if self._on_missing_window == "halt":
                        raise WindowNotFoundError(
                            "Window %r not found within %.1fs"
                            % (group.window, self._window_timeout)
                        )
                    log.warning(
                        "Window %r not found within %.1fs — skipping group",
                        group.window, self._window_timeout,
                    )
                    continue
            self._play_group(group)

    def _prepare_window(self, group: MacroGroup) -> bool:
        """Locate, reposition, and focus the group's target window.

        Waits up to window_timeout.  If that fails and the group has a launch
        command, runs it once per play() and waits again.  Returns True once
        the window is ready, or False if it never appears or the stop key is
        pressed.
        """
        if self._wait_for_window(group):
            return True
        if self._stop_event.is_set() or not group.launch or id(group) in self._launched:
            return False
        self._launched.add(id(group))
        self._emit("Window %r not found, launching: %s" % (group.window, group.launch))
        try:
            subprocess.Popen(group.launch, shell=True)
        except OSError as e:
            log.warning("Could not launch %r: %s", group.launch, e)
            return False
        return self._wait_for_window(group)

    def _wait_for_window(self, group: MacroGroup) -> bool:
        """Poll for the group's window until found, timed out, or stopped."""
        deadline = time.monotonic() + self._window_timeout
        while True:
            if self._stop_event.is_set():
                return False
            try:
                info = self._wm.find_window(group.window, group.match_mode)
            except ValueError as e:
                raise MacroExecutionError(str(e)) from e
            if info:
                if group.recorded_rect:
                    r = group.recorded_rect
                    self._wm.move_resize(info, r.left, r.top, r.width, r.height)
                self._wm.set_foreground(info)
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._stop_event.wait(timeout=min(0.25, remaining)):
                return False

    def _play_group(self, group: MacroGroup) -> None:
        """Play a single group's events, timed relative to the group's start.

        Relative mouse coordinates are converted to absolute by adding the
        recorded window origin (the window has already been moved there).

        Reports each event to on_active_event (after its preceding gap) so the
        UI can highlight the row whose time is currently being spent.
        """
        events = group.events
        if not events:
            return

        offset_x = offset_y = 0
        if group.window and group.recorded_rect:
            offset_x = group.recorded_rect.left
            offset_y = group.recorded_rect.top

        group_start = events[0].ts
        playback_start = time.perf_counter()

        for event in events:
            elapsed = time.perf_counter() - playback_start
            wait_secs = ((event.ts - group_start) / self._speed) - elapsed

            if wait_secs > 0:
                # wait() wakes immediately if stop_event is set.  The previously
                # reported active row stays highlighted during this gap, which is
                # exactly the dwell for a preceding wait row.
                if self._stop_event.wait(timeout=wait_secs):
                    return
            if self._stop_event.is_set():
                return
            # Report the row becoming active only after the gap, so the row whose
            # time is being spent (not the upcoming one) is what's highlighted.
            self._note_instruction(event)
            if self._on_active_event:
                self._on_active_event(event)

            # Numeric wait durations are already baked into the ts gaps above.
            # An expression duration can't be normalized at edit time, so the
            # row is ts-instant and we sleep the resolved duration here, then
            # shift playback_start so later events stay correctly scheduled.
            if event.type == EventType.WAIT and isinstance(event.duration, str):
                self._log(event, self._describe(event))
                extra = self._resolve_duration_seconds(event.duration) / self._speed
                if extra > 0:
                    if self._stop_event.wait(timeout=extra):
                        return
                    playback_start += extra
            else:
                exec_start = time.perf_counter()
                self._execute_event(event, offset_x, offset_y)
                self._log(event, self._describe(event))
                # Some actions block for real time not reflected in the ts gaps
                # (a per-character-delay type_text, or OCR capture), so shift the
                # reference past that time to keep later waits correctly scheduled.
                blocks_off_plan = (
                    event.type in (EventType.OCR_READ, EventType.MATCH_IMAGE, EventType.MATCH_TEXT)
                    or (event.type == EventType.TYPE_TEXT and event.char_delay is not None)
                )
                if blocks_off_plan:
                    playback_start += time.perf_counter() - exec_start

    # ------------------------------------------------------------------
    # Internal: control-flow playback (program counter + jumps)
    # ------------------------------------------------------------------

    def _play_with_control_flow(self, groups: list[MacroGroup]) -> None:
        """Execute one pass with goto / conditional-goto support.

        All groups are flattened into a single instruction stream.  A program
        counter advances one instruction at a time; goto/goto_if move it to the
        index of the row carrying the target label.  Because jumps make absolute
        time meaningless, timing comes only from wait rows (and the internal
        timing of timed moves).  Window preparation happens lazily the first
        time an instruction from a given windowed group is reached.
        """
        # Flatten to (event, offset_x, offset_y, group); record label positions
        # and the flat index where each instruction (table row) begins.
        flat: list[tuple[MacroEvent, int, int, MacroGroup]] = []
        labels: dict[str, int] = {}
        instr_positions: dict[int, int] = {}
        for g in groups:
            offset_x = g.recorded_rect.left if (g.window and g.recorded_rect) else 0
            offset_y = g.recorded_rect.top if (g.window and g.recorded_rect) else 0
            for ev in g.events:
                if ev.label and ev.label not in labels:
                    labels[ev.label] = len(flat)
                marker = getattr(ev, "instr", None)
                if marker is not None and marker not in instr_positions:
                    instr_positions[marker] = len(flat)
                flat.append((ev, offset_x, offset_y, g))

        if not flat:
            return

        def label_position(target: str | None) -> int:
            if target == GOTO_START:
                return 0
            if target == GOTO_END or target is None:
                return len(flat)
            if target in labels:
                return labels[target]
            log.warning("Goto target %r not found — continuing.", target)
            return -1  # sentinel: do not jump

        def instr_position(number: int) -> int:
            n = int(number)
            if instr_positions:                 # row markers available (UI playback)
                if n in instr_positions:
                    return instr_positions[n]
            elif 1 <= n <= len(flat):            # no markers (e.g. CLI): flat index
                return n - 1
            log.warning("Goto instruction #%s not found — continuing.", number)
            return -1

        def jump_target(event: MacroEvent) -> int:
            if event.target_index is not None:
                return instr_position(event.target_index)
            return label_position(event.target)

        prepared_group: MacroGroup | None = None
        pc = 0
        steps = 0
        while 0 <= pc < len(flat):
            if self._stop_event.is_set():
                return
            steps += 1
            if steps > _MAX_CONTROL_FLOW_STEPS:
                log.warning("Control-flow step limit reached — stopping playback.")
                return

            event, offset_x, offset_y, group = flat[pc]

            self._note_instruction(event)
            if self._on_active_event:
                self._on_active_event(event)

            if event.type == EventType.GOTO:
                dest = jump_target(event)
                self._log(event, "Goto %s%s" % (
                    self._goto_target_desc(event), "" if dest >= 0 else " (not found)"))
                pc = pc + 1 if dest < 0 else dest
                continue

            if event.type == EventType.GOTO_IF:
                try:
                    jump = evaluate_condition(event.expr or "0", self._variables)
                except (ExpressionError, ArithmeticError) as e:
                    raise MacroExecutionError(
                        "Cannot evaluate condition %r: %s" % (event.expr, e)) from e
                self._log(event, "If (%s) -> %s" % (
                    event.expr, "goto %s" % self._goto_target_desc(event) if jump else "no jump"))
                if jump:
                    dest = jump_target(event)
                    pc = pc + 1 if dest < 0 else dest
                else:
                    pc += 1
                continue

            # Prepare a windowed group's target window on first entry.
            if group is not prepared_group and group.window:
                prepared_group = group
                if not self._prepare_window(group):
                    if self._stop_event.is_set():
                        return
                    if self._on_missing_window == "halt":
                        raise WindowNotFoundError(
                            "Window %r not found within %.1fs"
                            % (group.window, self._window_timeout)
                        )
                    log.warning("Window %r not found — skipping its actions.", group.window)
            elif group is not prepared_group:
                prepared_group = group

            if event.type == EventType.WAIT:
                self._log(event, self._describe(event))
                wait_secs = self._resolve_duration_seconds(event.duration) / self._speed
                if wait_secs > 0 and self._stop_event.wait(timeout=wait_secs):
                    return
            else:
                self._execute_event(event, offset_x, offset_y)
                self._log(event, self._describe(event))
            pc += 1

    # ------------------------------------------------------------------
    # Internal: event dispatch
    # ------------------------------------------------------------------

    def _is_row_start(self, event: MacroEvent) -> bool:
        """True for the event that begins a table row (or every event when the
        run carries no row markers, e.g. CLI playback)."""
        return getattr(event, "instr", None) is not None or not self._uses_markers

    def _note_instruction(self, event: MacroEvent) -> None:
        """Update the live playback counters that back the dynamic values.

        Counts one instruction per table row: a row's first event advances the
        counters; bundled siblings (no marker) do not.  ``op_num`` follows the
        event's row marker when present and falls back to the running count."""
        marker = getattr(event, "instr", None)
        if marker is not None:
            self._instr_executed += 1
            self._current_instr = marker
        elif not self._uses_markers:
            self._instr_executed += 1
            self._current_instr = self._instr_executed

    def _log(self, event: MacroEvent, detail: str) -> None:
        """Emit one log line per instruction (row) to the on_log callback."""
        if self._on_log is None or not self._is_row_start(event):
            return
        self._on_log("[%s] %s" % (self._current_instr, detail))

    def _emit(self, message: str) -> None:
        """Emit a log line not tied to an instruction."""
        log.info(message)
        if self._on_log is not None:
            self._on_log(message)

    def _goto_target_desc(self, event: MacroEvent) -> str:
        if event.target_index is not None:
            return "#%s" % event.target_index
        return str(event.target)

    def _describe(self, event: MacroEvent) -> str:
        """A short human description of an executed instruction (read after it
        ran, so assignment/capture results reflect the new value)."""
        t = event.type
        if t == EventType.VAR_SET:
            return "Assigned %s = %s  [%s]" % (
                event.var_name, _fmt_log_value(self._variables.get(event.var_name)), event.expr)
        if t == EventType.TYPE_TEXT:
            return 'Type text "%s"' % (event.expr or "")
        if t == EventType.WAIT:
            secs = self._resolve_duration_seconds(event.duration) if event.duration is not None else 0.0
            return "Wait %.0f ms" % (secs * 1000)
        if t == EventType.OCR_READ:
            return 'OCR -> %s = %s' % (
                event.var_name, _fmt_log_value(self._variables.get(event.var_name)))
        if t in (EventType.MATCH_IMAGE, EventType.MATCH_TEXT):
            return "%s = %s" % (event.var_name, _fmt_log_value(self._variables.get(event.var_name)))
        if t == EventType.MOUSE_MOVE:
            return "Move to (%s, %s)" % (event.x, event.y)
        if t == EventType.MOUSE_MOVE_TIMED:
            return "Move (%s, %s) -> (%s, %s)" % (event.x, event.y, event.dx, event.dy)
        if t == EventType.MOUSE_CLICK:
            return "Click %s %s @ (%s, %s)" % (
                event.button, "down" if event.pressed else "up", event.x, event.y)
        if t == EventType.MOUSE_SCROLL:
            return "Scroll (%s, %s)" % (event.dx, event.dy)
        if t in (EventType.KEY_PRESS, EventType.KEY_RELEASE):
            verb = "Key press" if t == EventType.KEY_PRESS else "Key release"
            return "%s %s" % (verb, event.key)
        if t == EventType.WINDOW_FOCUS:
            return "Focus window %r" % event.window
        return str(t)

    def _execute_event(self, event: MacroEvent, offset_x: int = 0,
                       offset_y: int = 0) -> None:
        """Dispatch a single MacroEvent to its handler.

        offset_x/offset_y are added to mouse coordinates to convert relative
        (window-local) positions back to absolute screen positions.
        """
        handler = self._handlers.get(event.type)
        if handler is None:
            # wait/window_focus are valid but non-executable here (timing and
            # group structure handle them); only warn on truly unknown types.
            if event.type not in (EventType.WAIT, EventType.WINDOW_FOCUS):
                log.warning("No handler for event type %r — skipping.", event.type)
            return
        handler(event, offset_x, offset_y)

    # -- per-type handlers ---------------------------------------------------

    def _resolve(self, value, default: float = 0.0) -> float:
        """Resolve a number-or-expression field against the variable store.

        Numbers pass through; None yields ``default``; an expression string is
        evaluated.  A bad expression (syntax, undefined variable, arithmetic)
        aborts playback via MacroExecutionError.
        """
        if value is None:
            return default
        try:
            return resolve_number(value, self._variables)
        except (ExpressionError, ArithmeticError) as e:
            raise MacroExecutionError("Cannot evaluate value %r: %s" % (value, e)) from e

    def _resolve_duration_seconds(self, value, default: float = 0.0) -> float:
        """Resolve a duration field to seconds.

        A number is already in seconds; an expression string is evaluated in the
        UI's unit (milliseconds) and converted to seconds.  Errors abort playback.
        """
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return value
        try:
            return float(evaluate(str(value), self._variables)) / 1000.0
        except (ExpressionError, ArithmeticError) as e:
            raise MacroExecutionError("Cannot evaluate duration %r: %s" % (value, e)) from e

    def _handle_mouse_move(self, event: MacroEvent, offset_x: int, offset_y: int) -> None:
        x = self._resolve(event.x) + offset_x
        y = self._resolve(event.y) + offset_y
        self._mouse_ctrl.position = (x, y)

    def _handle_mouse_move_timed(self, event: MacroEvent, offset_x: int, offset_y: int) -> None:
        # Smooth movement from From (x, y) to To (dx, dy) over duration seconds.
        # For timed moves dx/dy hold the absolute destination, not a delta.
        start_x = self._resolve(event.x) + offset_x
        start_y = self._resolve(event.y) + offset_y
        end_x = self._resolve(event.dx) + offset_x
        end_y = self._resolve(event.dy) + offset_y
        duration = self._resolve_duration_seconds(event.duration, default=0.5) or 0.5
        start_time = time.time()
        while time.time() - start_time < duration:
            # Exit immediately if the user pressed the stop key.
            if self._stop_event.is_set():
                return
            progress = (time.time() - start_time) / duration
            self._mouse_ctrl.position = (
                start_x + (end_x - start_x) * progress,
                start_y + (end_y - start_y) * progress,
            )
            time.sleep(0.01)  # 10 ms intervals
        if not self._stop_event.is_set():
            self._mouse_ctrl.position = (end_x, end_y)

    def _handle_mouse_click(self, event: MacroEvent, offset_x: int, offset_y: int) -> None:
        # Move to the click's coordinates first so the press/release land there
        # (recorded macros already moved, so this is a harmless no-op for them;
        # manually-added clicks now honour their x/y, including expressions).
        if event.x is not None and event.y is not None:
            self._mouse_ctrl.position = (self._resolve(event.x) + offset_x,
                                         self._resolve(event.y) + offset_y)
        btn = deserialize_button(event.button)
        if event.pressed:
            self._mouse_ctrl.press(btn)
            self._held_buttons.add(btn)
        else:
            self._mouse_ctrl.release(btn)
            self._held_buttons.discard(btn)

    def _handle_mouse_scroll(self, event: MacroEvent, offset_x: int, offset_y: int) -> None:
        self._mouse_ctrl.scroll(int(self._resolve(event.dx)), int(self._resolve(event.dy)))

    def _handle_key_press(self, event: MacroEvent, offset_x: int, offset_y: int) -> None:
        self._safe_kb_action("press", event.key)

    def _handle_key_release(self, event: MacroEvent, offset_x: int, offset_y: int) -> None:
        self._safe_kb_action("release", event.key)

    def _handle_type_text(self, event: MacroEvent, offset_x: int, offset_y: int) -> None:
        """Type an f-string-style template held in ``expr``.

        Literal text is typed as-is; ``{expr}`` segments are evaluated against
        the variable store and interpolated (e.g. ``"x is {x}"``).  Timing
        (mutually exclusive): ``char_delay`` sets a delay between characters;
        otherwise ``duration`` spreads the whole text over that time; with
        neither, the text is typed at once.  A bad/undefined expression aborts
        playback; a platform typing failure is logged and skipped.
        """
        try:
            text = render_template(event.expr or "", self._variables)
        except (ExpressionError, ArithmeticError) as e:
            raise MacroExecutionError(
                "Cannot render text %r: %s" % (event.expr, e)) from e

        # Per-character delay in seconds (raises on a bad timing expression).
        per_char = 0.0
        if event.char_delay is not None:
            per_char = self._resolve_duration_seconds(event.char_delay) / self._speed
        elif event.duration is not None and text:
            per_char = (self._resolve_duration_seconds(event.duration) / self._speed) / len(text)

        self._dispatching = True
        try:
            if per_char > 0:
                for ch in text:
                    if self._stop_event.is_set():
                        return
                    self._kb_ctrl.type(ch)
                    if self._stop_event.wait(timeout=per_char):
                        return
            else:
                self._kb_ctrl.type(text)
        except Exception as e:
            log.warning("Cannot type %r on this platform: %s", text, e)
        finally:
            self._dispatching = False

    def _handle_var_set(self, event: MacroEvent, offset_x: int, offset_y: int) -> None:
        """Assign var_name = eval(expr) against the current variable store.

        Evaluation happens here, at execution time, so no static analysis of the
        action list is needed.  A bad name or expression (including use of an
        uninitialised variable) aborts playback via MacroExecutionError.
        """
        if not is_valid_variable_name(event.var_name or ""):
            raise MacroExecutionError("Invalid variable name %r" % event.var_name)
        try:
            value = evaluate(event.expr or "0", self._variables)
        except (ExpressionError, ArithmeticError) as e:
            raise MacroExecutionError(
                "Cannot set %s = %r: %s" % (event.var_name, event.expr, e)) from e
        self._variables[event.var_name] = value
        log.debug("Variable %s = %r", event.var_name, value)

    def _handle_ocr(self, event: MacroEvent, offset_x: int, offset_y: int) -> None:
        """OCR the screen region between two corners and store the text in var_name.

        x/y and dx/dy are two opposite corners in any orientation; the region is
        the bounding box between them (each may be an expression).  A bad
        name/expression, an empty region, or an OCR failure aborts playback.
        """
        if not is_valid_variable_name(event.var_name or ""):
            raise MacroExecutionError("Invalid variable name %r" % event.var_name)
        x1 = int(self._resolve(event.x)) + offset_x
        y1 = int(self._resolve(event.y)) + offset_y
        x2 = int(self._resolve(event.dx)) + offset_x
        y2 = int(self._resolve(event.dy)) + offset_y
        left, top = min(x1, x2), min(y1, y2)
        width, height = abs(x2 - x1), abs(y2 - y1)
        if width <= 0 or height <= 0:
            raise MacroExecutionError(
                "OCR region is empty (%d, %d) → (%d, %d)" % (x1, y1, x2, y2))
        try:
            text = read_region(left, top, width, height)
        except OcrError as e:
            raise MacroExecutionError(str(e)) from e
        self._variables[event.var_name] = text
        log.debug("OCR %s = %r", event.var_name, text)

    def _match_region(self, event: MacroEvent, offset_x: int, offset_y: int) -> tuple:
        """Resolve a match/region source to (left, top, width, height).

        Uses the whole monitor if ``monitor`` is set, else the bounding box of
        the two corners (x,y)/(dx,dy), each of which may be an expression.
        """
        if event.monitor is not None:
            try:
                return monitor_region(int(event.monitor))
            except MatchError as e:
                raise MacroExecutionError(str(e)) from e
        x1 = int(self._resolve(event.x)) + offset_x
        y1 = int(self._resolve(event.y)) + offset_y
        x2 = int(self._resolve(event.dx)) + offset_x
        y2 = int(self._resolve(event.dy)) + offset_y
        left, top = min(x1, x2), min(y1, y2)
        width, height = abs(x2 - x1), abs(y2 - y1)
        if width <= 0 or height <= 0:
            raise MacroExecutionError("Match region is empty.")
        return (left, top, width, height)

    def _poll_deadline(self, event: MacroEvent, default: float = 5.0) -> float:
        return time.monotonic() + self._resolve_duration_seconds(event.duration, default=default)

    def _poll_sleep(self, deadline: float) -> bool:
        """Sleep up to one poll interval (or until the deadline).

        Returns True if the caller should stop (stop key pressed or timed out).
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        return self._stop_event.wait(timeout=min(_MATCH_POLL_INTERVAL, remaining))

    def _handle_match_image(self, event: MacroEvent, offset_x: int, offset_y: int) -> None:
        """Poll for a reference image in the region; set found flag + centre coords."""
        if not is_valid_variable_name(event.var_name or ""):
            raise MacroExecutionError("Invalid variable name %r" % event.var_name)
        region = self._match_region(event, offset_x, offset_y)
        confidence = float(event.tolerance) if event.tolerance is not None else 0.9
        deadline = self._poll_deadline(event)
        hit = None
        while not self._stop_event.is_set():
            try:
                hit = find_image(region, event.image_path, confidence)
            except MatchError as e:
                raise MacroExecutionError(str(e)) from e
            if hit is not None or self._poll_sleep(deadline):
                break
        if self._stop_event.is_set():
            return
        self._variables[event.var_name] = 1 if hit else 0
        if hit and event.capture_var and is_valid_variable_name(event.capture_var):
            self._variables[event.capture_var] = hit[0]
            if event.capture_var_y and is_valid_variable_name(event.capture_var_y):
                self._variables[event.capture_var_y] = hit[1]
        log.debug("match_image %s = %s", event.var_name, bool(hit))

    def _handle_match_text(self, event: MacroEvent, offset_x: int, offset_y: int) -> None:
        """Poll for expected text via OCR; set found flag + captured OCR text."""
        if not is_valid_variable_name(event.var_name or ""):
            raise MacroExecutionError("Invalid variable name %r" % event.var_name)
        region = self._match_region(event, offset_x, offset_y)
        try:
            expected = render_template(event.expr or "", self._variables)
        except (ExpressionError, ArithmeticError) as e:
            raise MacroExecutionError("Cannot evaluate expected text %r: %s" % (event.expr, e)) from e
        tolerance = float(event.tolerance) if event.tolerance is not None else 0.8
        deadline = self._poll_deadline(event)
        last_text, matched = "", False
        while not self._stop_event.is_set():
            try:
                last_text, _ratio, matched = match_text(region, expected, tolerance)
            except (MatchError, OcrError) as e:
                raise MacroExecutionError(str(e)) from e
            if matched or self._poll_sleep(deadline):
                break
        if self._stop_event.is_set():
            return
        self._variables[event.var_name] = 1 if matched else 0
        if event.capture_var and is_valid_variable_name(event.capture_var):
            self._variables[event.capture_var] = last_text
        log.debug("match_text %s = %s", event.var_name, matched)

    def _safe_kb_action(self, action: str, key_str: str) -> None:
        """Deserialize key_str and call press or release on the controller.

        Sets _dispatching around the controller call so the stop-key listener
        does not react to the synthetic event.  Logs a warning and continues
        if the key cannot be simulated on this platform.
        """
        try:
            key_obj = deserialize_key(key_str)
        except Exception as e:
            log.warning("Unknown key %r in macro — skipping. (%s)", key_str, e)
            return

        self._dispatching = True
        try:
            if action == "press":
                self._kb_ctrl.press(key_obj)
                self._held_keys.add(key_obj)
            else:
                self._kb_ctrl.release(key_obj)
                self._held_keys.discard(key_obj)
        except Exception as e:
            log.warning(
                "Cannot simulate key %s %r on this platform — skipping. (%s)",
                action, key_str, e,
            )
            if action == "press":
                self._held_keys.discard(key_obj)  # don't track a key we failed to press
        finally:
            self._dispatching = False

    # ------------------------------------------------------------------
    # Internal: cleanup
    # ------------------------------------------------------------------

    def _cleanup(self) -> None:
        """Release all currently held keys and mouse buttons.

        Called unconditionally from the try/finally in play() so that
        interrupted playback never leaves inputs stuck in the down state.
        """
        self._dispatching = True
        try:
            for btn in list(self._held_buttons):
                try:
                    self._mouse_ctrl.release(btn)
                except Exception as e:
                    log.debug("Failed to release mouse button %r during cleanup: %s", btn, e)
            for key in list(self._held_keys):
                try:
                    self._kb_ctrl.release(key)
                except Exception as e:
                    log.debug("Failed to release key %r during cleanup: %s", key, e)
        finally:
            self._held_buttons.clear()
            self._held_keys.clear()
            self._dispatching = False
