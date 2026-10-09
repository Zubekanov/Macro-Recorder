"""Rows and the MacroDocument.

The document is the single source of truth for the table, the file and the
player.  Rows are immutable; every operation replaces ``rows`` with a new
tuple, pushes the previous state as one undo entry (unless an edit session
coalesces it), bumps the revision and notifies subscribers.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from macro_recorder.model.catalogue import (
    END_TARGET, FILTER_GROUPS, RESERVED_TARGETS, START_TARGET, spec,
)
from macro_recorder.model.events import EventType, MacroEvent
from macro_recorder.model.protocols import Change, ChangeListener

PATH_MODES = ("recorded", "linear", "spline", "natural")
UNDO_DEPTH = 200

_ids = itertools.count(1)


def new_row_id() -> int:
    return next(_ids)


@dataclass(frozen=True)
class Row:
    id: int
    events: tuple[MacroEvent, ...]
    label: str = ""
    comment: str = ""
    enabled: bool = True
    breakpoint: bool = False
    delay_ms: int = 0

    def __post_init__(self) -> None:
        if not self.events:
            raise ValueError("A row needs at least one event")
        if len(self.events) > 1 and any(e.type != EventType.MOUSE_MOVE for e in self.events):
            raise ValueError("Only mouse_move paths may have several events")

    @property
    def type(self) -> str:
        return self.events[0].type

    @property
    def event(self) -> MacroEvent:
        return self.events[0]

    @property
    def is_path(self) -> bool:
        return len(self.events) > 1

    @property
    def is_window(self) -> bool:
        return self.type == EventType.WINDOW_FOCUS

    @property
    def span_s(self) -> float:
        return sum(e.dt for e in self.events)


def make_row(events: MacroEvent | Sequence[MacroEvent], **fields: Any) -> Row:
    """A row with a fresh id."""
    evs = (events,) if isinstance(events, MacroEvent) else tuple(events)
    return Row(id=new_row_id(), events=evs, **fields)


# Labels: letters, digits, underscore, space and hyphen; no leading digit.
_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ \-]*$")


def label_problem(label: str, document: "MacroDocument | None" = None,
                  row_id: int | None = None) -> str | None:
    """Why ``label`` is not acceptable, or None."""
    if label == "":
        return None
    if not _LABEL_RE.match(label):
        return "Labels start with a letter and use letters, digits, spaces, _ or -"
    if label.lower() in (t.lower() for t in RESERVED_TARGETS):
        return "%r is reserved" % label
    if document is not None:
        for r in document.rows:
            if r.id != row_id and r.label.lower() == label.lower():
                return "Label %r is already used" % label
    return None


@dataclass(frozen=True)
class _State:
    rows: tuple[Row, ...]
    filter: tuple[str, ...]
    path_mode: str


class MacroDocument:
    """Ordered rows plus playback filter and path mode, with undo."""

    def __init__(self, rows: Iterable[Row] = (), filter: Iterable[str] = (),
                 path_mode: str = "recorded", path: str | None = None) -> None:
        self._rows: tuple[Row, ...] = tuple(rows)
        self._filter: tuple[str, ...] = tuple(filter)
        self._path_mode = path_mode
        self.path: str | None = path
        self._revision = 0
        self._saved_revision = 0
        self._undo: list[tuple[_State, int]] = []
        self._redo: list[tuple[_State, int]] = []
        self._session_key: tuple[int, str] | None = None
        self._session_open = False
        self._listeners: list[ChangeListener] = []
        self._validate_state()

    # ------------------------------------------------------------------ state

    @property
    def rows(self) -> tuple[Row, ...]:
        return self._rows

    @property
    def filter(self) -> tuple[str, ...]:
        return self._filter

    @property
    def path_mode(self) -> str:
        return self._path_mode

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def dirty(self) -> bool:
        return self._revision != self._saved_revision

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def mark_saved(self, path: str | None = None) -> None:
        self._saved_revision = self._revision
        if path is not None:
            self.path = path

    def subscribe(self, listener: ChangeListener) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: ChangeListener) -> None:
        self._listeners.remove(listener)

    # ---------------------------------------------------------------- lookup

    def row(self, row_id: int) -> Row:
        for r in self._rows:
            if r.id == row_id:
                return r
        raise KeyError(row_id)

    def index_of(self, row_id: int) -> int:
        for i, r in enumerate(self._rows):
            if r.id == row_id:
                return i
        raise KeyError(row_id)

    def row_index(self) -> dict[int, int]:
        return {r.id: i for i, r in enumerate(self._rows)}

    def label_index(self) -> dict[str, int]:
        """Lower-cased label -> row index (first occurrence)."""
        out: dict[str, int] = {}
        for i, r in enumerate(self._rows):
            if r.label and r.label.lower() not in out:
                out[r.label.lower()] = i
        return out

    def labels(self) -> list[str]:
        return [r.label for r in self._rows if r.label]

    def governing_window(self, index: int) -> Row | None:
        """The nearest enabled, unfiltered window row at or above ``index``."""
        for i in range(index, -1, -1):
            r = self._rows[i]
            if r.is_window:
                return r if r.enabled else None
        return None

    def plays(self, row: Row) -> bool:
        """False for disabled rows and rows whose filter group is filtered."""
        if not row.enabled:
            return False
        group = spec(row.type).filter_group
        return group is None or group not in self._filter

    # ------------------------------------------------------------ mutations

    def insert(self, index: int, rows: Sequence[Row]) -> tuple[int, ...]:
        """Insert rows (fresh ids; duplicate labels cleared).  Returns the new ids."""
        if not rows:
            return ()
        fresh = []
        used = {r.label.lower() for r in self._rows if r.label}
        for r in rows:
            label = r.label
            if label and (label.lower() in used or label_problem(label) is not None):
                label = ""
            if label:
                used.add(label.lower())
            fresh.append(replace(r, id=new_row_id(), label=label))
        index = max(0, min(index, len(self._rows)))
        self._commit(self._rows[:index] + tuple(fresh) + self._rows[index:],
                     Change("inserted", tuple(r.id for r in fresh)))
        return tuple(r.id for r in fresh)

    def append(self, rows: Sequence[Row]) -> tuple[int, ...]:
        return self.insert(len(self._rows), rows)

    def delete(self, ids: Iterable[int]) -> None:
        wanted = set(ids)
        if not wanted:
            return
        self._commit(tuple(r for r in self._rows if r.id not in wanted),
                     Change("removed", tuple(wanted)))

    def move(self, ids: Iterable[int], index: int) -> None:
        """Move the rows with ``ids`` (in document order) so they start at ``index``
        of the remaining rows."""
        wanted = set(ids)
        moving = [r for r in self._rows if r.id in wanted]
        if not moving:
            return
        remaining = [r for r in self._rows if r.id not in wanted]
        index = max(0, min(index, len(remaining)))
        new = tuple(remaining[:index] + moving + remaining[index:])
        if new == self._rows:
            return
        self._commit(new, Change("moved", tuple(r.id for r in moving)))

    def replace_row(self, row: Row) -> None:
        """Replace the row with the same id.  Label changes rewrite targets."""
        old = self.row(row.id)
        rows = self._rows
        if old.label != row.label and old.label:
            rows = tuple(_retarget(r, old.label, row.label) for r in rows)
        rows = tuple(row if r.id == row.id else r for r in rows)
        if rows == self._rows:
            return
        self._commit(rows, Change("updated", (row.id,)))

    def rename_label(self, row_id: int, label: str) -> None:
        problem = label_problem(label, self, row_id)
        if problem:
            raise ValueError(problem)
        self.replace_row(replace(self.row(row_id), label=label))

    def set_enabled(self, ids: Iterable[int], enabled: bool) -> None:
        wanted = set(ids)
        self._commit(tuple(replace(r, enabled=enabled) if r.id in wanted else r for r in self._rows),
                     Change("updated", tuple(wanted)))

    def toggle_enabled(self, ids: Iterable[int]) -> None:
        wanted = set(ids)
        self._commit(tuple(replace(r, enabled=not r.enabled) if r.id in wanted else r for r in self._rows),
                     Change("updated", tuple(wanted)))

    def set_breakpoint(self, ids: Iterable[int], on: bool) -> None:
        wanted = set(ids)
        self._commit(tuple(replace(r, breakpoint=on) if r.id in wanted else r for r in self._rows),
                     Change("updated", tuple(wanted)))

    def toggle_breakpoint(self, ids: Iterable[int]) -> None:
        wanted = set(ids)
        self._commit(tuple(replace(r, breakpoint=not r.breakpoint) if r.id in wanted else r
                           for r in self._rows), Change("updated", tuple(wanted)))

    def split(self, row_id: int, min_wait_ms: int = 100) -> tuple[int, ...]:
        """Turn a path row into one mouse_move row per point.

        The first new row keeps the id, label, comment, breakpoint and delay;
        gaps of ``min_wait_ms`` or more become the next row's delay.
        """
        row = self.row(row_id)
        if not row.is_path:
            return (row_id,)
        index = self.index_of(row_id)
        new_rows: list[Row] = []
        for i, ev in enumerate(row.events):
            delay = int(round(ev.dt * 1000)) if i else row.delay_ms
            if i == 0:
                new_rows.append(replace(row, events=(ev.with_dt(0.0),)))
            else:
                new_rows.append(Row(id=new_row_id(), events=(ev.with_dt(0.0),),
                                    enabled=row.enabled, delay_ms=delay))
        rows = self._rows[:index] + tuple(new_rows) + self._rows[index + 1:]
        self._commit(rows, Change("reset"))
        return tuple(r.id for r in new_rows)

    def set_filter(self, groups: Iterable[str]) -> None:
        groups = tuple(g for g in FILTER_GROUPS if g in set(groups))
        if groups == self._filter:
            return
        self._commit(self._rows, Change("meta"), filter=groups)

    def set_path_mode(self, mode: str) -> None:
        if mode not in PATH_MODES:
            raise ValueError("Unknown path mode %r" % mode)
        if mode == self._path_mode:
            return
        self._commit(self._rows, Change("meta"), path_mode=mode)

    def reset(self, rows: Iterable[Row] = (), filter: Iterable[str] = (),
              path_mode: str = "recorded", path: str | None = None, clear_history: bool = True) -> None:
        """Replace everything (New, Open).  Clears undo when asked."""
        self._rows = tuple(rows)
        self._filter = tuple(filter)
        self._path_mode = path_mode
        self.path = path
        self._validate_state()
        if clear_history:
            self._undo.clear()
            self._redo.clear()
            self._revision = 0
            self._saved_revision = 0
        else:
            self._revision += 1
        self.end_edit()
        self._notify(Change("reset"))

    # ------------------------------------------------------------ edit sessions

    def begin_edit(self, row_id: int, key: str) -> None:
        """Coalesce following replace_row calls on (row_id, key) into one undo step."""
        session = (row_id, key)
        if self._session_key != session:
            self._session_key = session
            self._session_open = False

    def end_edit(self) -> None:
        self._session_key = None
        self._session_open = False

    # ------------------------------------------------------------------ undo

    def undo(self) -> bool:
        self.end_edit()
        if not self._undo:
            return False
        state, revision = self._undo.pop()
        self._redo.append((self._state(), self._revision))
        self._restore(state, revision)
        return True

    def redo(self) -> bool:
        self.end_edit()
        if not self._redo:
            return False
        state, revision = self._redo.pop()
        self._undo.append((self._state(), self._revision))
        self._restore(state, revision)
        return True

    # -------------------------------------------------------------- internals

    def _state(self) -> _State:
        return _State(self._rows, self._filter, self._path_mode)

    def _restore(self, state: _State, revision: int) -> None:
        self._rows, self._filter, self._path_mode = state.rows, state.filter, state.path_mode
        self._revision = revision
        self._notify(Change("reset"))

    def _commit(self, rows: tuple[Row, ...], change: Change, *,
                filter: tuple[str, ...] | None = None, path_mode: str | None = None) -> None:
        coalesce = change.kind == "updated" and self._session_key is not None and self._session_open
        if not coalesce:
            self._undo.append((self._state(), self._revision))
            del self._undo[:-UNDO_DEPTH]
            self._redo.clear()
            if change.kind == "updated" and self._session_key is not None:
                self._session_open = True
        self._revision += 1
        self._rows = rows
        if filter is not None:
            self._filter = filter
        if path_mode is not None:
            self._path_mode = path_mode
        self._validate_state()
        if change.kind != "updated":
            self.end_edit()
        self._notify(change)

    def _validate_state(self) -> None:
        seen: set[int] = set()
        for r in self._rows:
            if r.id in seen:
                raise ValueError("Duplicate row id %d" % r.id)
            seen.add(r.id)
        if self._path_mode not in PATH_MODES:
            raise ValueError("Unknown path mode %r" % self._path_mode)

    def _notify(self, change: Change) -> None:
        for listener in list(self._listeners):
            listener(change)


# ---------------------------------------------------------------------------
# Label retargeting
# ---------------------------------------------------------------------------

def target_keys(row: Row) -> tuple[str, ...]:
    """The LABEL_TARGET keys of the row's spec."""
    from macro_recorder.model.catalogue import FieldKind
    return tuple(k for f in spec(row.type).fields if f.kind == FieldKind.LABEL_TARGET for k in f.keys)


def _retarget(row: Row, old: str, new: str) -> Row:
    changes = {}
    for key in target_keys(row):
        value = row.event.get(key)
        if isinstance(value, str) and value.lower() == old.lower():
            changes[key] = new
    if not changes:
        return row
    return replace(row, events=(row.event.with_params(**changes),))


def rows_referencing(rows: Sequence[Row], label: str) -> list[Row]:
    out = []
    for r in rows:
        for key in target_keys(r):
            value = r.event.get(key)
            if isinstance(value, str) and value.lower() == label.lower():
                out.append(r)
                break
    return out
