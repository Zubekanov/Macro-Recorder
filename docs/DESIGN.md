# Design notes

## Layout

| Module | Role |
|---|---|
| `run.py` | tkinter GUI (`MacroRecorderApp`). The only module besides `overlay_renderer.py` that imports tkinter. |
| `main.py` | CLI: `record`, `play`, `list`. |
| `recorder.py` | pynput listeners plus a Win32 foreground-window hook. Produces `MacroGroup`s. |
| `player.py` | Replays groups. Two engines, see below. |
| `macro.py` | `MacroEvent`, `MacroGroup`, `WindowRect`, JSON load/save. |
| `event_types.py` | `EventType` str-enum. |
| `bundling.py` | Collapses a raw event stream into table rows. |
| `table_model.py` | Row to events map, timestamp normalisation, group/flat conversion. Pure, no tkinter. |
| `expressions.py` | Safe AST evaluator for expressions and `{templates}`. |
| `matching.py`, `ocr.py` | Image match (OpenCV + mss) and text read (Windows OCR via winsdk). Imported lazily. |
| `window_manager.py` | pywin32 window find/move/focus with a no-op fallback. |
| `overlay_renderer.py` | Transparent topmost canvas that previews the selected row on screen. |

## File format

```json
{"version": 2, "groups": [
  {"window": "Notepad", "recorded_rect": {"left": 0, "top": 0, "width": 800, "height": 600},
   "events": [{"type": "mouse_click", "ts": 0.0, "x": 10, "y": 20, "button": "left", "pressed": true}]}
]}
```

- A group is a run of events under one foreground window. `window: null` means the leading group before any focus change.
- `match_mode` (`substring`, the default and omitted, or `regex`) says how `window` is matched at playback. `launch` is an optional command run once if the window is missing.
- Mouse `x`/`y` are relative to `recorded_rect` inside a windowed group, absolute otherwise. The GUI table always shows absolute coordinates and converts at load, save and play.
- `MacroEvent.to_dict` writes only non-None fields. `instr` is transient and never written.
- A bare JSON array is accepted as the legacy flat format.

## Event types

| Type | Playback |
|---|---|
| `mouse_move` | Set cursor to `x,y`. |
| `mouse_move_timed` | Interpolate from `x,y` to `dx,dy` over `duration` seconds in 10 ms steps. `dx,dy` are absolute, not a delta. |
| `mouse_click` | Move to `x,y` if set, then press or release `button` per `pressed`. |
| `mouse_scroll` | Scroll by `dx,dy`. |
| `key_press`, `key_release` | Via pynput. Unknown keys log and skip. |
| `type_text` | Render `expr` as a template, type it. `char_delay` or `duration` spreads it over time. |
| `wait` | Sleep `duration`. Numeric durations are baked into `ts` gaps at edit time; expression durations are slept at runtime. |
| `window_focus` | Never executed. Only appears in the GUI table as a group delimiter. |
| `var_set` | `var_name = evaluate(expr)`. |
| `goto`, `goto_if` | Jump to `target` label or `target_index` row. Control-flow engine only. |
| `ocr_read` | OCR the box between `x,y` and `dx,dy` into `var_name`. |
| `match_image` | Poll for `image_path` in the region until found or `duration` elapses. Sets `var_name` to 0/1, optional centre into `capture_var`/`capture_var_y`. |
| `match_text` | Same, comparing OCR text to `expr` with Levenshtein similarity >= `tolerance`. |

Any numeric field may instead hold an expression string, resolved at playback. Durations are seconds when numeric and milliseconds when an expression.

## Playback engines

`Player.play` picks an engine per run:

- **Linear.** No `goto` present. Each group is scheduled by absolute `ts` relative to the group start, divided by speed.
- **Control flow.** Any `goto` present. Groups are flattened, labels and row numbers indexed, and a program counter walks the list. Timing comes only from `wait` rows and timed moves. A step cap of one million guards against runaway loops. A missing target logs and falls through.

Windowed groups are located, moved to `recorded_rect`, and focused before their first event. Matching is case-insensitive substring by default, or `re.search` in regex mode, and an exact title wins when several windows match. If the window is missing after `window_timeout` seconds and the group has a `launch` command, the command runs once per play and the wait repeats. If it is still missing the group is skipped or playback halts, per `on_missing_window`.

Variables live in a dict overlaid with read-only dynamic values (`rt_ms`, `op_num`, `ex_num`, `iteration`, `mouse_x`, `mouse_y`, `timestamp_ms`, `random`). Assigning to one raises.

## Timestamp normalisation

Every table mutation calls `TableModel.normalize_timestamps` with the visual row order. Each `window_focus` row resets the clock to zero. Every other row is shifted to start at the running clock, keeping its internal spacing, and the clock advances by the row's numeric duration or 50 ms for instant actions. This is what keeps edited macros playing in table order.

## Recording

The recorder keeps a flat stream from pynput plus `window_focus` markers from the Win32 hook. When recording stops, `collapse_drags` turns every button-held movement into press, one `mouse_move_timed`, release. Then `_build_groups` splits the stream at focus markers and makes coordinates window-relative. Free movement stays as raw moves and is bundled into rows by the GUI.

## GUI notes

- Rows are bundles: a click down/up pair, a run of moves, or a run of same-direction scrolls is one row. `bundling.py` decides.
- The Details panel is rebuilt per event type and syncs to the event on every keystroke. Window rows expose the title, match mode and launch command; those travel on the `window_focus` row event and become group fields on save.
- Active-row highlight during playback uses Treeview selection. The player thread enqueues rows, a 50 ms main-thread pump paints one per tick.
- The targeting button installs a global mouse hook and swallows the click via a Win32 event filter.

## Tests

`uv run pytest`. Player tests patch the pynput controllers and listener with `tests/playback_test_utils.py` so nothing touches the real cursor or keyboard.
