"""Pure event-bundling logic for the action table.

Groups a flat, chronologically-ordered event stream into display "rows":

- a mouse click down/up pair within ``click_gap_s`` becomes one click row,
- consecutive same-direction scrolls collapse into one scroll row,
- consecutive mouse moves within ``move_gap_s`` collapse into one move row, and
- a gap larger than ``move_gap_s`` between two move segments yields a synthetic
  ``wait`` row carrying the gap duration.

This module is UI-free: it only consumes and produces MacroEvent objects, so it
can be unit-tested without tkinter.
"""

from __future__ import annotations

from macro_recorder.event_types import EventType
from macro_recorder.macro import MacroEvent


def bundle_events(
    events: list[MacroEvent],
    move_gap_s: float,
    click_gap_s: float,
) -> list[list[MacroEvent]]:
    """Bundle a flat event stream into a list of row-groups.

    Each returned inner list is the set of events backing one table row, in
    display order.  Synthetic ``wait`` rows (single-event groups) are inserted
    between move segments separated by more than ``move_gap_s``.

    Coordinates are taken as-is; this function never mutates input events.
    """
    groups: list[list[MacroEvent]] = []
    i = 0
    while i < len(events):
        ev = events[i]

        # Bundle a click down with its corresponding up.
        if (ev.type == EventType.MOUSE_CLICK and ev.pressed and
                i + 1 < len(events) and
                events[i + 1].type == EventType.MOUSE_CLICK and
                not events[i + 1].pressed and
                events[i + 1].button == ev.button and
                events[i + 1].ts - ev.ts <= click_gap_s):
            groups.append([ev, events[i + 1]])
            i += 2
            continue

        # Bundle consecutive scroll events in the same direction.
        if ev.type == EventType.MOUSE_SCROLL and groups and groups[-1][0].type == EventType.MOUSE_SCROLL:
            last_scroll = groups[-1][0]
            same_vert = (ev.dy and last_scroll.dy and
                         (ev.dy > 0) == (last_scroll.dy > 0))
            same_horiz = (ev.dx and last_scroll.dx and
                          (ev.dx > 0) == (last_scroll.dx > 0))
            if same_vert or same_horiz:
                groups[-1].append(ev)
                i += 1
                continue
            else:
                groups.append([ev])   # different direction → new group
        elif ev.type == EventType.MOUSE_SCROLL:
            groups.append([ev])
        # Bundle consecutive mouse_move events within the gap threshold.
        elif ev.type == EventType.MOUSE_MOVE and groups and groups[-1][0].type == EventType.MOUSE_MOVE:
            gap = ev.ts - groups[-1][-1].ts
            if gap <= move_gap_s:
                groups[-1].append(ev)
                i += 1
                continue
            else:
                groups.append([ev])   # gap too large → new segment
        else:
            groups.append([ev])

        i += 1

    # Insert a synthetic wait row between two consecutive move segments.
    rows: list[list[MacroEvent]] = []
    for idx, group in enumerate(groups):
        first = group[0]
        if idx > 0 and first.type == EventType.MOUSE_MOVE and groups[idx - 1][0].type == EventType.MOUSE_MOVE:
            prev_last = groups[idx - 1][-1]
            gap_s = first.ts - prev_last.ts
            rows.append([MacroEvent(type=EventType.WAIT, ts=prev_last.ts, duration=gap_s)])
        rows.append(group)

    return rows
