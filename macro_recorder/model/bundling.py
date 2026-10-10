"""Raw recorded stream to rows.

:func:`rows_from_stream` walks the recorder's filtered stream once and
applies the rules of DESIGN.md "Recording into rows".  The walk is done by
:class:`StreamBundler`, which can also be fed incrementally so the recording
controller can show provisional rows without re-bundling the whole stream on
every poll.

The bundler keeps a list of drafts, one per future row, in the order the rows
will appear.  Some drafts are slots whose content is decided later: a key
press reserves its place and becomes a ``key_combo`` at its release (or a
``key_press`` half when something else happens first); a button press
reserves its place and becomes a click, or the ``down`` half of a drag, at
its release.

Decisions where the design is silent:

- Events are processed in time order; an event that arrives late but stamped
  earlier than one already processed is processed in arrival order and its
  gap clamps to 0.
- Rule 1: between the press and the marker only motion and the release of the
  pressed button may occur.  The moved marker takes the press's time, so the
  click gets no delay after the window row.
- A key tap is a press and release with no other row between; motion thinned
  away by ``min_move_px`` does not count, a path does.  A key held across a
  click, scroll, path or another key's press or release is recorded as
  ``key_press`` and ``key_release`` rows around it.
- Modifiers absorbed into a combo whose key is still down when a button or
  scroll arrives are re-pressed with a ``key_press`` row before it, so a
  Ctrl+S followed by a Ctrl+click replays both correctly.  A combo's
  modifiers are those absorbed or still undecided; a modifier already
  emitted as ``key_press`` is not repeated in the combo.
- A press and release that are neither a click (rule 2) nor a drag (rule 3),
  such as a long press in place, become ``down``, any path, and ``up``.  Any
  other row between a press and its release (a key, a scroll, another
  button) does the same.  A drag's movement is one path even across pauses
  longer than ``min_wait_ms``.
- Double-click timing is measured press to press; the merged click keeps the
  first click's hold and takes ``interval_ms`` from the first gap.
- A scroll between two different points (``min_move_px`` apart), in another
  direction, or after more than ``min_wait_ms`` starts a new scroll row.  A
  summed scroll takes ``notch_ms`` from the mean notch interval.
- With ``record_paths`` off, free movement is not recorded at all; only a
  drag's movement becomes a ``mouse_move_timed``.
- A window row immediately followed by another window row is dropped (the
  later one wins), and a marker equal to the last window row is ignored.
- A button press without a release takes its ``down`` row and its unflushed
  movement with it.  Orphans are reported in :attr:`StreamBundler.dropped`
  and logged by :func:`rows_from_stream`.
"""

from __future__ import annotations

import bisect
import logging
import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from macro_recorder.model.catalogue import make
from macro_recorder.model.document import Row, make_row
from macro_recorder.model.events import EventType, MacroEvent
from macro_recorder.model.keys import MODIFIER_ALIASES, display_key, is_modifier
from macro_recorder.model.protocols import RawEvent, WindowInfo

if TYPE_CHECKING:
    from macro_recorder.gui.settings import RecordingSettings

log = logging.getLogger(__name__)

# Rule 1: how long after a button press a window marker may arrive and still
# be treated as caused by that press.
MARKER_WINDOW_S = 0.5

# Draft kinds that are not event types.
_PATH = "path"
_PRESS = "press"                # undecided button press slot
_PENDING_KEY = "pending_key"    # undecided key press slot


# ---------------------------------------------------------------------------
# Rule helpers
# ---------------------------------------------------------------------------

def distance(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(bx - ax, by - ay)


def activating_marker_index(buffer: Sequence[RawEvent], press_index: int) -> int | None:
    """Rule 1: the index of a window marker the press at ``press_index`` caused.

    The marker must come within :data:`MARKER_WINDOW_S` of the press with
    nothing between but motion and the press's own release.
    """
    press = buffer[press_index]
    released = False
    for j in range(press_index + 1, len(buffer)):
        e = buffer[j]
        if e.time - press.time > MARKER_WINDOW_S:
            return None
        if e.kind == "window_focus":
            return j
        if e.kind == "motion":
            continue
        if e.kind == "button_release" and e.button == press.button and not released:
            released = True
            continue
        return None
    return None


def is_click(press: RawEvent, release: RawEvent, settings: RecordingSettings) -> bool:
    """Rule 2: a press and release close in time and place."""
    return (release.button == press.button
            and (release.time - press.time) * 1000 <= settings.click_gap_ms
            and distance(press.x, press.y, release.x, release.y) < settings.min_move_px)


def continues_click(previous: "_Draft", press: RawEvent, settings: RecordingSettings) -> bool:
    """Rule 2: whether ``press`` starts the second or third click of ``previous``."""
    if previous.type != EventType.MOUSE_CLICK or previous.params.get("action") != "click":
        return False
    p = previous.params
    return (p["button"] == press.button and p["count"] < 3
            and (press.time - previous.meta["last_press"]) * 1000 <= settings.double_click_ms
            and distance(p["x"], p["y"], press.x, press.y) < settings.min_move_px)


def point_kept(reference: tuple[int, int] | None, x: int, y: int, settings: RecordingSettings) -> bool:
    """Rule 4: a motion sample joins a path when far enough from the last point."""
    return reference is None or distance(reference[0], reference[1], x, y) >= settings.min_move_px


def path_continues(last_motion_time: float, time: float, settings: RecordingSettings) -> bool:
    """Rule 4: a gap over ``min_wait_ms`` closes a path."""
    return (time - last_motion_time) * 1000 <= settings.min_wait_ms


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def scroll_merges(previous: "_Draft", event: RawEvent, settings: RecordingSettings) -> bool:
    """Rule 5: same direction, same point, within ``min_wait_ms`` of the last notch."""
    if previous.type != EventType.MOUSE_SCROLL:
        return False
    p = previous.params
    return (_sign(p["dx"]) == _sign(event.dx) and _sign(p["dy"]) == _sign(event.dy)
            and distance(p["x"], p["y"], event.x, event.y) < settings.min_move_px
            and (event.time - previous.end) * 1000 <= settings.min_wait_ms)


def modifier_alias(name: str) -> str:
    """``Control_R`` -> ``ctrl``; a modifier without an alias keeps its keysym name."""
    family = display_key(name).lower()
    return family if family in MODIFIER_ALIASES else name


def combo_text(names: Iterable[str]) -> str:
    """Rule 6: stored combo text such as ``ctrl+shift+s``, each modifier once."""
    *mods, key = names
    parts: list[str] = []
    for name in mods:
        alias = modifier_alias(name)
        if alias not in parts:
            parts.append(alias)
    return "+".join(parts + [key])


def split_gap(gap_s: float, settings: RecordingSettings) -> tuple[int | None, int]:
    """Rule 7: ``(wait row ms or None, delay_ms of the later row)``."""
    gap_ms = max(0.0, gap_s) * 1000
    if gap_ms > settings.min_wait_ms:
        return int(round(gap_ms)), 0
    return None, int(round(gap_ms))


def window_event(info: WindowInfo) -> MacroEvent:
    """Rules 1 and 8: the window row event for a focus marker."""
    return make("window_focus", window=info.title, match_mode="substring",
                rect=info.rect.as_tuple(), client_rect=info.client_rect.as_tuple(),
                maximised=info.maximised)


# ---------------------------------------------------------------------------
# Drafts and open state
# ---------------------------------------------------------------------------

@dataclass(eq=False)
class _Draft:
    """One future row.  ``type`` is an event type or a draft kind."""
    type: str
    start: float
    end: float
    params: dict[str, Any] = field(default_factory=dict)
    points: list[tuple[int, int, float]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    open: bool = False
    removed: bool = False


@dataclass(eq=False)
class _PressUnit:
    """A button held down, with the movement since its press."""
    press: RawEvent
    slot: _Draft
    merge_into: _Draft | None = None        # the click this press may raise to a double-click
    clean: bool = True                      # False once its down row is emitted
    points: list[tuple[int, int, float]] = field(default_factory=list)
    first_motion: float | None = None
    last_motion: float | None = None


@dataclass(eq=False)
class _HeldKey:
    key: str
    time: float
    mods: tuple[str, ...]
    state: str                  # pending | held | absorbed
    slot: _Draft | None


# ---------------------------------------------------------------------------
# The bundler
# ---------------------------------------------------------------------------

class StreamBundler:
    """Turns a raw stream into rows, incrementally.

    Call :meth:`feed` with new events and :meth:`advance` with the current
    recording time, then :meth:`provisional` for the rows so far.  Call
    :meth:`finish` once the stream is complete and :meth:`rows` for the
    result.  Each event is processed once; rows for drafts that can no
    longer change are built once and cached.
    """

    def __init__(self, settings: RecordingSettings) -> None:
        self.settings = settings
        self.dropped: list[str] = []
        self.version = 0                        # bumps whenever an event is processed
        self._buffer: list[RawEvent] = []
        self._head = 0
        self._horizon = -math.inf
        self._drafts: list[_Draft] = []
        self._buttons: dict[str, _PressUnit] = {}
        self._clean_unit: _PressUnit | None = None
        self._keys: dict[str, _HeldKey] = {}
        self._path: _Draft | None = None
        self._last_motion = -math.inf
        self._pointer: tuple[int, int] | None = None
        self._deferred: list[RawEvent] = []
        self._last_window: WindowInfo | None = None
        self._finished = False
        self._cache_rows: list[Row] = []
        self._cache_upto = 0
        self._cache_end: float | None = None

    # ----------------------------------------------------------------- input

    def feed(self, events: Iterable[RawEvent]) -> None:
        """Queue events (in time order) and process what can be processed."""
        for e in events:
            bisect.insort(self._buffer, e, lo=self._head, key=_event_time)
        self._drain()

    def advance(self, now: float) -> None:
        """Tell the bundler the recording has reached ``now`` seconds.

        A button press waits in the queue until :data:`MARKER_WINDOW_S` has
        passed, so a window marker it causes can still move before it.
        """
        self._horizon = max(self._horizon, now)
        self._drain()

    def finish(self) -> None:
        """Process everything left, close open units and drop orphans."""
        if self._finished:
            return
        self._horizon = math.inf
        self._drain()
        self._finished = True
        for unit in list(self._buttons.values()):
            self.dropped.append("Dropped a %s button press with no release" % unit.press.button)
            unit.slot.removed = True
            unit.slot.open = False
        self._buttons.clear()
        self._clean_unit = None
        for hk in self._keys.values():
            if hk.state in ("pending", "held") and hk.slot is not None:
                self.dropped.append("Dropped a %s key press with no release" % hk.key)
                hk.slot.removed = True
                hk.slot.open = False
        self._keys.clear()
        self._close_path()
        self._flush_markers()

    # ---------------------------------------------------------------- output

    def rows(self) -> list[Row]:
        """Every row, built from scratch.  Call after :meth:`finish`."""
        rows, _ = self._render(self._drafts, None)
        return rows

    def provisional(self) -> list[Row]:
        """The rows so far, open units shown as they would be if closed now.

        Rows that can no longer change keep their identity across calls.
        """
        stable = self._stable_count()
        if stable > self._cache_upto:
            rows, self._cache_end = self._render(self._drafts[self._cache_upto:stable], self._cache_end)
            self._cache_rows.extend(rows)
            self._cache_upto = stable
        tail, _ = self._render(self._drafts[self._cache_upto:], self._cache_end)
        return self._cache_rows + tail

    def _stable_count(self) -> int:
        """How many leading drafts can no longer change.

        Open drafts can change, and so can the last two live drafts: a scroll
        or window row may still merge or be replaced, and a click may become
        a double-click while the next press is undecided.
        """
        live = 0
        limit = len(self._drafts)
        while limit > 0 and live < 2:
            limit -= 1
            if not self._drafts[limit].removed:
                live += 1
        if live < 2:
            return 0
        for i in range(self._cache_upto, limit):
            if self._drafts[i].open:
                return i
        return limit

    def _render(self, drafts: Sequence[_Draft], prev_end: float | None) -> tuple[list[Row], float | None]:
        """Rule 7: build rows, turning the gap before each into a wait or a delay."""
        rows: list[Row] = []
        for d in drafts:
            if d.removed:
                continue
            delay = 0
            if prev_end is not None:
                wait_ms, delay = split_gap(d.start - prev_end, self.settings)
                if wait_ms is not None:
                    rows.append(make_row(make("wait", duration_ms=wait_ms)))
            rows.append(make_row(_draft_events(d), delay_ms=delay))
            prev_end = d.end
        return rows, prev_end

    # ------------------------------------------------------------- draining

    def _drain(self) -> None:
        buf = self._buffer
        while self._head < len(buf):
            e = buf[self._head]
            if e.kind == "button_press":
                if e.time + MARKER_WINDOW_S > self._horizon:
                    break
                j = activating_marker_index(buf, self._head)
                if j is not None:
                    marker = buf.pop(j)
                    self._process(replace(marker, time=e.time))
            self._head += 1
            self._process(e)
        if self._head > 256 and self._head * 2 > len(buf):
            del buf[:self._head]
            self._head = 0

    def _process(self, e: RawEvent) -> None:
        self.version += 1
        handler = _HANDLERS.get(e.kind)
        if handler is None:
            self.dropped.append("Ignored an unknown raw event %r" % e.kind)
        else:
            handler(self, e)
        if not self._buttons and self._path is None:
            self._flush_markers()

    # ---------------------------------------------------------- draft list

    def _last(self) -> _Draft | None:
        for d in reversed(self._drafts):
            if not d.removed:
                return d
        return None

    def _push(self, draft: _Draft) -> None:
        self._drafts.append(draft)

    def _append(self, draft: _Draft, *, key_slot: bool = False, needs_modifiers: bool = False) -> None:
        """Add a row after everything so far, settling what it interrupts."""
        self._close_path()
        if not self._buttons:
            self._flush_markers()
        self._interrupt_press()
        self._decide_pending_keys(key_slot)
        if needs_modifiers:
            self._hold_absorbed_modifiers(draft.start)
        self._push(draft)

    def _close_path(self) -> None:
        if self._path is not None:
            self._path.open = False
            self._path = None

    def _flush_markers(self) -> None:
        """Place markers held back by a click, drag or path after it."""
        if not self._deferred:
            return
        markers, self._deferred = self._deferred, []
        for m in markers:
            last = self._last()
            t = max(m.time, last.end) if last is not None else m.time
            self._push_window(m.window, t)

    def _push_window(self, info: WindowInfo | None, time: float) -> None:
        if info is None or info == self._last_window:
            return
        last = self._last()
        if last is not None and last.type == EventType.WINDOW_FOCUS:
            last.removed = True
        self._last_window = info
        self._push(_Draft(EventType.WINDOW_FOCUS.value, time, time, {"info": info}))

    # ------------------------------------------------------------- markers

    def _on_window_focus(self, e: RawEvent) -> None:
        """Rule 1: a marker never splits a click, a drag or a path."""
        if self._buttons or self._path is not None:
            self._deferred.append(e)
        else:
            self._push_window(e.window, e.time)

    # --------------------------------------------------------------- mouse

    def _on_button_press(self, e: RawEvent) -> None:
        if e.button in self._buttons:
            self.dropped.append("Ignored a second %s button press" % e.button)
            return
        self._pointer = (e.x, e.y)
        previous = self._last()
        slot = _Draft(_PRESS, e.time, e.time,
                      {"x": e.x, "y": e.y, "button": e.button}, open=True)
        self._append(slot, needs_modifiers=True)
        unit = _PressUnit(e, slot)
        if previous is not None and previous is self._last_before(slot) \
                and continues_click(previous, e, self.settings):
            unit.merge_into = previous
        self._buttons[e.button] = unit
        self._clean_unit = unit

    def _last_before(self, draft: _Draft) -> _Draft | None:
        found = False
        for d in reversed(self._drafts):
            if d is draft:
                found = True
            elif found and not d.removed:
                return d
        return None

    def _on_button_release(self, e: RawEvent) -> None:
        unit = self._buttons.get(e.button)
        if unit is None:
            self.dropped.append("Dropped a %s button release with no press" % e.button)
            return
        if unit.clean and is_click(unit.press, e, self.settings):
            del self._buttons[e.button]
            self._clean_unit = None
            self._pointer = (e.x, e.y)
            self._finish_click(unit, e)
            return
        if unit.clean:
            self._emit_down(unit)
        self._pointer = (e.x, e.y)
        self._append(_Draft(EventType.MOUSE_CLICK.value, e.time, e.time,
                            {"x": e.x, "y": e.y, "button": e.button, "action": "up"}))
        del self._buttons[e.button]

    def _finish_click(self, unit: _PressUnit, release: RawEvent) -> None:
        """Rule 2: a click, or the next click of a double or triple click."""
        press, slot = unit.press, unit.slot
        prev = unit.merge_into
        if prev is not None and self._last_before(slot) is prev:
            slot.removed = True
            slot.open = False
            p = prev.params
            if p["count"] == 1:
                p["interval_ms"] = int(round((press.time - prev.end) * 1000))
            p["count"] += 1
            prev.end = release.time
            prev.meta["last_press"] = press.time
            return
        slot.type = EventType.MOUSE_CLICK.value
        slot.params = {"x": press.x, "y": press.y, "button": press.button, "action": "click",
                       "count": 1, "hold_ms": int(round((release.time - press.time) * 1000))}
        slot.meta["last_press"] = press.time
        slot.end = release.time
        slot.open = False

    def _interrupt_press(self) -> None:
        """Another row arrived while a button is down: it becomes down, path, ..., up."""
        if self._clean_unit is not None:
            self._emit_down(self._clean_unit)

    def _emit_down(self, unit: _PressUnit) -> None:
        """Rule 3: the press becomes ``down`` followed by the movement so far."""
        press, slot = unit.press, unit.slot
        slot.type = EventType.MOUSE_CLICK.value
        slot.params = {"x": press.x, "y": press.y, "button": press.button, "action": "down"}
        slot.open = False
        unit.clean = False
        unit.merge_into = None
        if self._clean_unit is unit:
            self._clean_unit = None
        if not unit.points:
            return
        if self.settings.record_paths:
            first = unit.points[0]
            self._push(_Draft(_PATH, first[2], unit.points[-1][2], points=list(unit.points)))
        else:
            last = unit.points[-1]
            self._push(_Draft(EventType.MOUSE_MOVE_TIMED.value, unit.first_motion, unit.last_motion, {
                "x": press.x, "y": press.y, "x2": last[0], "y2": last[1],
                "duration_ms": int(round((unit.last_motion - unit.first_motion) * 1000))}))
        unit.points = []

    def _on_motion(self, e: RawEvent) -> None:
        unit = self._clean_unit
        if unit is not None:
            ref = (unit.points[-1][0], unit.points[-1][1]) if unit.points else (unit.press.x, unit.press.y)
            if unit.first_motion is None:
                unit.first_motion = e.time
            unit.last_motion = e.time
            if point_kept(ref, e.x, e.y, self.settings):
                unit.points.append((e.x, e.y, e.time))
                self._pointer = (e.x, e.y)
            return
        if not self.settings.record_paths:
            self._pointer = (e.x, e.y)
            return
        if self._path is not None and not path_continues(self._last_motion, e.time, self.settings):
            self._close_path()
        self._last_motion = e.time
        if not point_kept(self._pointer, e.x, e.y, self.settings):
            return
        self._pointer = (e.x, e.y)
        if self._path is not None:
            self._path.points.append((e.x, e.y, e.time))
            self._path.end = e.time
            return
        path = _Draft(_PATH, e.time, e.time, points=[(e.x, e.y, e.time)], open=True)
        self._append(path)
        self._path = path

    def _on_scroll(self, e: RawEvent) -> None:
        last = self._last()
        self._pointer = (e.x, e.y)
        if last is not None and self._path is None and self._clean_unit is None \
                and last is self._drafts[-1] and scroll_merges(last, e, self.settings):
            p = last.params
            p["dx"] += e.dx
            p["dy"] += e.dy
            last.meta["notches"] += 1
            last.end = e.time
            p["notch_ms"] = int(round((last.end - last.start) * 1000 / (last.meta["notches"] - 1)))
            return
        draft = _Draft(EventType.MOUSE_SCROLL.value, e.time, e.time,
                       {"x": e.x, "y": e.y, "dx": e.dx, "dy": e.dy}, meta={"notches": 1})
        self._append(draft, needs_modifiers=True)

    # ---------------------------------------------------------------- keys

    def _decide_pending_keys(self, key_slot: bool) -> None:
        """Rule 6: a row between a key's press and release ends its chance of a tap.

        A modifier followed by another key press is absorbed into that key's
        combo; anything else makes the key a held ``key_press``.
        """
        for hk in self._keys.values():
            if hk.state != "pending":
                continue
            assert hk.slot is not None
            if key_slot and is_modifier(hk.key):
                hk.state = "absorbed"
                hk.slot.removed = True
                hk.slot.open = False
                hk.slot = None
            else:
                hk.state = "held"
                hk.slot.type = EventType.KEY_PRESS.value
                hk.slot.params = {"key": hk.key}
                hk.slot.open = False

    def _hold_absorbed_modifiers(self, time: float) -> None:
        """Modifiers still down from a combo are pressed again before a click or scroll."""
        for hk in self._keys.values():
            if hk.state == "absorbed":
                hk.state = "held"
                hk.slot = _Draft(EventType.KEY_PRESS.value, time, time, {"key": hk.key})
                self._push(hk.slot)

    def _on_key_press(self, e: RawEvent) -> None:
        if e.key in self._keys:
            return                                  # autorepeat of a held key
        mods = tuple(k for k, hk in self._keys.items()
                     if is_modifier(k) and hk.state in ("pending", "absorbed"))
        slot = _Draft(_PENDING_KEY, e.time, e.time, {"names": mods + (e.key,)}, open=True)
        self._append(slot, key_slot=True)
        self._keys[e.key] = _HeldKey(e.key, e.time, mods, "pending", slot)

    def _on_key_release(self, e: RawEvent) -> None:
        hk = self._keys.pop(e.key, None)
        if hk is None:
            self.dropped.append("Dropped a %s key release with no press" % e.key)
            return
        if hk.state == "absorbed":
            return
        if hk.state == "pending":
            assert hk.slot is not None
            hk.slot.type = EventType.KEY_COMBO.value
            hk.slot.params = {"keys": combo_text(hk.mods + (hk.key,)),
                              "hold_ms": int(round((e.time - hk.time) * 1000))}
            hk.slot.end = e.time
            hk.slot.open = False
            return
        self._append(_Draft(EventType.KEY_RELEASE.value, e.time, e.time, {"key": e.key}))


_HANDLERS = {
    "window_focus": StreamBundler._on_window_focus,
    "button_press": StreamBundler._on_button_press,
    "button_release": StreamBundler._on_button_release,
    "motion": StreamBundler._on_motion,
    "scroll": StreamBundler._on_scroll,
    "key_press": StreamBundler._on_key_press,
    "key_release": StreamBundler._on_key_release,
}


def _event_time(e: RawEvent) -> float:
    return e.time


def _draft_events(d: _Draft) -> tuple[MacroEvent, ...]:
    """The events of a draft's row.  Open slots show as if closed now."""
    if d.type == _PATH:
        out = []
        prev = d.points[0][2]
        for x, y, t in d.points:
            out.append(make("mouse_move", x=x, y=y, dt=max(0.0, t - prev)))
            prev = t
        return tuple(out)
    if d.type == _PRESS:
        return (make("mouse_click", **d.params),)
    if d.type == _PENDING_KEY:
        return (make("key_combo", keys=combo_text(d.params["names"])),)
    if d.type == EventType.WINDOW_FOCUS:
        return (window_event(d.params["info"]),)
    return (make(d.type, **d.params),)


def rows_from_stream(events: Sequence[RawEvent], settings: RecordingSettings) -> list[Row]:
    """Bundle a complete raw stream into rows.  Orphans are logged."""
    bundler = StreamBundler(settings)
    bundler.feed(events)
    bundler.finish()
    for message in bundler.dropped:
        log.warning("Recording: %s", message)
    return bundler.rows()
