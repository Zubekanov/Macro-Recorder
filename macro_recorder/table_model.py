"""UI-free model backing the action table.

`TableModel` owns the mapping from row id to the events that back that row
(``row_events``) plus the pure operations over them: timestamp normalization
and conversion between the grouped on-disk form and the flat display stream.

The model knows nothing about tkinter.  Callers supply the current visual row
order (a list of row ids) and, where needed, per-row labels; the model never
reads a Treeview.  This keeps the timing/coordinate logic independently
testable.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from macro_recorder.event_types import EventType
from macro_recorder.macro import MacroEvent, MacroGroup, WindowRect, offset_event
from macro_recorder.window_manager import MATCH_SUBSTRING

# Minimum spacing handed to instant (zero-duration) actions so consecutive
# rows never share a timestamp.
DEFAULT_MIN_ACTION_SPACING_S = 0.05


class TableModel:
    """Holds row→events state and the pure operations over it."""

    def __init__(self) -> None:
        self.row_events: dict[str, list[MacroEvent]] = {}

    # ------------------------------------------------------------------
    # Timestamp normalization
    # ------------------------------------------------------------------

    def normalize_timestamps(
        self,
        order: list[str],
        min_action_spacing_s: float = DEFAULT_MIN_ACTION_SPACING_S,
    ) -> None:
        """Re-anchor all timestamps to the given visual row order.

        Each ``window_focus`` row resets the clock to 0.0.  For every other row
        the events are shifted to start at the running clock while preserving
        their internal relative timing; the clock then advances by the row's
        duration, or by ``min_action_spacing_s`` for instant actions.

        Timestamps drive playback timing only; row highlighting is now event-
        driven (the player reports the active row), so no time ranges are kept.
        """
        current_ts = 0.0
        for iid in order:
            group = self.row_events.get(iid)
            if not group:
                continue
            first = group[0]
            if first.type == EventType.WINDOW_FOCUS:
                current_ts = 0.0
                continue

            # Shift all events in the group to start at current_ts.
            row_start = first.ts
            new_group = [replace(ev, ts=current_ts + (ev.ts - row_start)) for ev in group]
            self.row_events[iid] = new_group

            # Advance the clock by the row's numeric duration, or a minimum
            # spacing.  Expression (string) durations aren't known until
            # playback, so the row is treated as instant here and the player
            # sleeps the resolved duration at runtime.
            last_event = new_group[-1]
            duration = last_event.duration
            if isinstance(duration, (int, float)) and duration > 0:
                current_ts = last_event.ts + duration
            else:
                current_ts = last_event.ts + min_action_spacing_s

    # ------------------------------------------------------------------
    # Group <-> flat-stream conversion
    # ------------------------------------------------------------------

    @staticmethod
    def groups_to_flat_events(groups: list[MacroGroup]) -> list[MacroEvent]:
        """Flatten groups into a single absolute-coordinate event stream.

        A window_focus event (carrying window + rect) is emitted at the start of
        each windowed group; relative mouse coordinates are converted to absolute
        by adding the recorded window origin.  Timestamps are made cumulative
        across groups (group 1: 0-5s, group 2: 5-10s, …).
        """
        flat: list[MacroEvent] = []
        time_offset = 0.0

        for g in groups:
            group_end_time = max((ev.ts for ev in g.events), default=0.0)

            if g.window:
                ts = (g.events[0].ts if g.events else 0.0) + time_offset
                rect_list = None
                if g.recorded_rect:
                    r = g.recorded_rect
                    rect_list = [r.left, r.top, r.width, r.height]
                flat.append(MacroEvent(type=EventType.WINDOW_FOCUS, ts=ts,
                                       window=g.window, rect=rect_list,
                                       match_mode=g.match_mode, launch=g.launch))
            for ev in g.events:
                ev_adjusted = replace(ev, ts=ev.ts + time_offset)
                if g.window and g.recorded_rect:
                    ev_adjusted = offset_event(ev_adjusted, g.recorded_rect.left, g.recorded_rect.top)
                flat.append(ev_adjusted)

            time_offset += group_end_time

        return flat

    def rows_to_groups(
        self,
        order: list[str],
        labels: dict[str, Optional[str]],
        event_to_iid: Optional[dict[int, str]] = None,
    ) -> list[MacroGroup]:
        """Reconstruct MacroGroups from ``row_events`` in the given row order.

        window_focus rows act as group delimiters (their window + rect become
        the group metadata and the row itself is not retained as an event).
        Mouse coordinates, stored absolute, are converted back to window-relative
        for windowed groups.  ``labels`` maps row id → edited label (or None) and
        is synced onto the first event of each row's bundle.

        If ``event_to_iid`` is given, it is populated with ``id(event) -> row id``
        for every event placed into a group — including the relative-coordinate
        copies made for windowed groups — so the caller can map the player's
        active-event reports back to table rows by identity.

        The first event of each row's bundle is tagged with its 1-based
        instruction number (the value shown in the table's number column),
        so the control-flow engine can resolve goto-by-instruction-number jumps.
        The numbering mirrors the table: every row with events counts, including
        window_focus rows.
        """
        groups: list[MacroGroup] = []
        current_window: Optional[str] = None
        current_rect: Optional[WindowRect] = None
        current_events: list[MacroEvent] = []
        current_mode: str = MATCH_SUBSTRING
        current_launch: Optional[str] = None
        instr_no = 0

        def _flush() -> None:
            if current_events or current_window is not None:
                groups.append(MacroGroup(
                    window=current_window,
                    recorded_rect=current_rect,
                    events=current_events,
                    match_mode=current_mode,
                    launch=current_launch,
                ))

        for iid in order:
            group = self.row_events.get(iid)
            if not group:
                continue
            first = group[0]
            instr_no += 1   # mirrors the table number column (counts every row)

            if first.type == EventType.WINDOW_FOCUS:
                _flush()
                current_window = first.window
                current_rect = WindowRect(*first.rect) if first.rect else None
                current_events = []
                current_mode = first.match_mode or MATCH_SUBSTRING
                current_launch = first.launch or None
                continue

            # Sync the edited label and instruction number onto the first event
            # of the row's bundle (the rest stay unmarked so each row resolves to
            # one jump position).
            group[0].label = labels.get(iid)
            for i, ev in enumerate(group):
                ev.instr = instr_no if i == 0 else None
                if current_window and current_rect:
                    ev = offset_event(ev, -current_rect.left, -current_rect.top)
                current_events.append(ev)
                if event_to_iid is not None:
                    event_to_iid[id(ev)] = iid

        _flush()
        return groups
