# Design

Macro Recorder records, edits and replays mouse and keyboard macros on Linux
X11 desktops (developed on Linux Mint 22 / Cinnamon). Bartels Media's Macro
Recorder is the guide for behaviour and vocabulary, not an exact copy target.
This document is the intended state of the code. `docs/ROADMAP.md` lists the
work packages, their file ownership and what is out of scope.

## Principles

- One document model, one file format, one playback engine. What the table
  shows is what is saved and what is played.
- Rows and events are immutable. Every edit builds new objects with
  `dataclasses.replace`, so an undo entry is just the previous row tuple and
  the player never shares mutable state with the editor.
- Every action type is described once, by an `ActionSpec` in the catalogue:
  its fields, defaults, Details layout, Value text, overlay, validation and
  playback. Adding an action touches one category module.
- `model/` and `engine/expressions.py` import nothing from tkinter or Xlib.
  `platform/` is the only package that talks to the X server. python-xlib
  covers recording (XRecord), injection (XTest), the stop key and hotkeys
  (XInput2, XGrabKey) and window management (EWMH).
- Worker threads never call Tk. They post to a queue the main thread drains.
- Tests never touch the user's display. Anything that opens a window or
  reads input runs only under Xvfb, behind a marker.

## Package layout

| Module | Role |
|---|---|
| `macro_recorder/app.py` | GUI entry point: logging, session check, settings, Tk root with class `macro-recorder`. |
| `macro_recorder/cli.py` | `record`, `play`, `list`, `validate`. |
| `macro_recorder/model/events.py` | `EventType`, `MacroEvent`. |
| `macro_recorder/model/keys.py` | Key vocabulary: X keysym names, modifier aliases, `parse_combo`, `format_combo`, `is_modifier`. Pure Python. |
| `macro_recorder/model/document.py` | `Row`, `MacroDocument`, `Change` notifications, undo. |
| `macro_recorder/model/catalogue.py` | `FieldKind`, `FieldSpec`, `Preset`, `ActionSpec`, `Flow`, the registry, `PlayContext` and `PlayerHost` Protocols. |
| `macro_recorder/model/actions/` | One module per category registering specs: `mouse.py`, `keyboard.py`, `wait.py`, `window.py`, `variables.py`, `control.py`, `find.py`, `misc.py`. `__init__.py` imports them all. |
| `macro_recorder/model/fileformat.py` | Version 3 JSON read and write, structural validation, atomic save. |
| `macro_recorder/model/validate.py` | Semantic checks: labels, targets, expressions, files, variable names, field ranges. |
| `macro_recorder/model/bundling.py` | Raw recorded stream to rows. |
| `macro_recorder/engine/expressions.py` | Safe expression evaluator and `{template}` rendering. |
| `macro_recorder/engine/player.py` | The program-counter engine. |
| `macro_recorder/engine/recorder.py` | Drives the platform listener into a raw event stream with own-window filtering. |
| `macro_recorder/engine/paths.py` | Pure path generators: linear, spline, natural. |
| `macro_recorder/engine/screen.py` | Screen capture (mss), image find (OpenCV), pixel read, OCR (Tesseract). |
| `macro_recorder/engine/processes.py` | Runs launch and exec_program commands, reaps children. `run` returns the exit code, None when stopped, and raises `ProcessTimeout` when `timeout_ms` passes (the group is killed first) or `ProcessError` when the command cannot start. An unwaited child is reaped by a daemon thread and dropped from the caller's list once its process group is gone, so `terminate` never signals a reused number. |
| `macro_recorder/platform/session.py` | X11 vs Wayland detection, server time, own-window registry. |
| `macro_recorder/platform/x11.py` | python-xlib helpers: EWMH, geometry, activation, workspaces, SHAPE, XTest, XRecord, XInput2 raw events, XGrabKey. |
| `macro_recorder/platform/windows.py` | `WindowManager` on one locked display connection; a `NullWindowManager`. |
| `macro_recorder/platform/input.py` | `InputBackend` (XTest injector), `InputListener` (XRecord stream), `StopKeyWatcher` (XInput2 raw), `HotkeyGrabber` (XGrabKey). |
| `macro_recorder/gui/main_window.py` | Shell: menus, ribbon tabs, footer, modes, the event queue pump. |
| `macro_recorder/gui/commands.py` | `Command` registry with per-mode enablement; every menu item, button and shortcut goes through it. |
| `macro_recorder/gui/table.py` | `ActionTable`: Treeview over the document, incremental updates, drag reorder, inline Label and Comment editing, context menu. |
| `macro_recorder/gui/details.py` | Details panel generated from the row's `ActionSpec` layout; per-field commit. |
| `macro_recorder/gui/fields.py` | Field widgets, one class per `FieldKind`. |
| `macro_recorder/gui/recording.py` | Recording controller: provisional live rows, insert at stop. |
| `macro_recorder/gui/playback.py` | Playback controller: thread, host implementation, highlight, pause, variables view. |
| `macro_recorder/gui/overlay.py` | Click-through SHAPE overlay for the selected row. |
| `macro_recorder/gui/log_panel.py` | Output Log and Variables tabs in the bottom pane. |
| `macro_recorder/gui/targeting.py` | Capture a point or a pixel colour with grabbed pointer and keyboard. |
| `macro_recorder/gui/hotkeys.py` | Routes grabbed hotkeys and stop keys by mode. |
| `macro_recorder/gui/settings.py` | `Settings` dataclass, load and save, the Settings dialog. |
| `macro_recorder/gui/dialogs.py` | Unsaved-changes prompt, error dialog, message_box dialog. |

## Document model

```python
@dataclass(frozen=True)
class MacroEvent:
    type: str                 # an EventType value
    params: dict              # exactly the keys of the type's FieldSpecs; never mutated
    dt: float = 0.0           # seconds after the previous event in the same row; 0 for the first

@dataclass(frozen=True)
class Row:
    id: int                   # runtime handle from a per-process counter; not saved
    events: tuple[MacroEvent, ...]
    label: str = ""
    comment: str = ""
    enabled: bool = True
    breakpoint: bool = False
    delay_ms: int = 0         # waited before the row plays
```

- `MacroEvent.params` is built only through `catalogue.make(type, **params)`,
  which fills defaults and rejects unknown keys. `ev[key]` reads a param.
  `ev.with_params(**changes)` returns a new event. Container values are
  tuples.
- A row is the unit of editing, numbering, labels, goto targets, enabling,
  breakpoints and delay. Row ids are allocated by the document; load, paste
  and split always allocate fresh ids. The clipboard carries row dicts in
  the file format, so paste works across documents and instances.
- The only multi-event row is a **path**: a recorded mouse movement, many
  `mouse_move` events with `dt`. Every other row holds one event. A click
  (press and release) is one `mouse_click` event; a scroll run is one
  `mouse_scroll` event with the notch total; a key tap or chord is one
  `key_combo` event.
- A **window row** holds one `window_focus` event. Rows after it, up to the
  next enabled window row, form its group. A disabled window row ends the
  previous group; its group plays with no window context.
- Labels live only on rows and are unique, case-insensitive, never "Start"
  or "End". Renaming a label rewrites every target that used it in the same
  undo step. Clearing a label that rows jump to is refused (`replace_row`
  and `rename_label` raise ValueError naming those rows; `rename_problem`
  gives the same text for live validation). Paste renames a label that
  already exists to the first free `<base> 2`, `<base> 3`..., where the
  base is the label without a trailing ` N` ("Loop 2" pasted again becomes
  "Loop 3") and a name the pasted rows use as a label or a target is not
  free, and retargets the pasted rows that jump to it, clears an invalid
  label, and logs either.
- `MacroDocument` holds `rows: tuple[Row, ...]`, `filter`, `path_mode`, a
  revision counter and `saved_revision` (dirty means they differ), and the
  undo stack. Operations: `insert(index, rows)`, `delete(ids)`,
  `move(ids, index)`, `replace_row(row)`, `split(ids)`, `set_enabled(ids,
  bool)`, `set_breakpoint(ids, bool)`, `rename_label(id, label)`,
  `set_filter`, `set_path_mode`, `begin_edit(id, key)`, `end_edit()`,
  `undo()`, `redo()`, `row_index()`, `label_index()`, `subscribe(callback)`.
  Each operation pushes one undo entry (unless an edit session with the same
  `(row id, field key)` is open), bumps the revision and notifies
  subscribers with `Change(kind, ids)` where kind is `reset`, `inserted`,
  `removed`, `moved` or `updated`.
- Edit sessions coalesce keystrokes on one field of one row. A session ends
  on selection change, a different field, FocusOut of the Details panel,
  any other document command (undo and redo close it before acting),
  targeting completion and any mode change.
- Breakpoints and `enabled` are saved; toggling either dirties the document
  and is one undo step.

### Coordinates and frames

- Numeric coordinates are screen pixels, as the table shows them.
- Inside a group the player adds a **delta**: the window's current client
  origin minus the recorded client origin. Outside a group the delta is 0.
  A window row without a recorded rect has delta 0.
- An expression in a coordinate field evaluates relative to the current
  window's client origin, or the screen origin outside a group.
- Every coordinate the engine writes into a variable (`capture_var`,
  `capture_var_y`, `mouse_x`, `mouse_y`) is in that same frame. `win_x`,
  `win_y`, `win_w`, `win_h` give the current client rectangle (0, 0 and the
  screen size outside a group).
- Only `COORD_PAIR` fields are frame-adjusted. Both halves of a pair must be
  numbers or both expressions; validation rejects a mix. Scroll deltas and
  `REGION` fields in monitor mode are never adjusted.
- Moving or pasting a row never rewrites its coordinates; they are read in
  the group they land in.

### Timing

Rows have no absolute time.

- Before a row plays the engine waits `max(row.delay_ms,
  settings.playback.min_spacing_ms)` (default 10) divided by speed.
- A `wait` row sleeps its duration divided by speed.
- A path replays its `dt` offsets divided by speed. The engine keeps a
  schedule clock (`due += dt / speed`, sleep until `due`); when it is more
  than 50 ms late it skips path points, never clicks or keys, to catch up.
  After any step whose length is not scheduled (window row, pixel or match
  wait, OCR, message box, breakpoint, exec with wait) the clock resets.
- `mouse_click` holds the button for `hold_ms` and waits `interval_ms`
  between repeated clicks. `mouse_scroll` waits `notch_ms` between notches.
  `key_combo` holds for `hold_ms`.
- Scaled by 1/speed: row delays, waits (fixed, expression and random), path
  and timed-move durations, click and scroll intervals, type_text delays and
  humanise jitter. Not scaled: window timeout and settle, pixel and match
  timeouts and poll intervals, exec timeouts, message boxes.
- Durations are milliseconds everywhere: in the file, the Details panel and
  expressions. Keys end in `_ms`. A numeric value is an int; an expression
  must evaluate to a number of milliseconds.

### Recording into rows

`bundling.rows_from_stream(stream, settings.recording)` walks the raw stream
once:

1. A `window_focus` marker that arrives within 500 ms after a button press,
   with no other input between, moves to just before that press. A marker
   never splits a click, a drag or a path; it moves after the unit.
2. A press and the release of the same button within `click_gap_ms` (250)
   and `min_move_px` of each other become one `mouse_click` with
   `hold_ms` set from the recording. A second such click within
   `double_click_ms` (400) at the same point raises `count` to 2 (3).
3. A press, movement and a release elsewhere become `mouse_click` down, a
   path (or a `mouse_move_timed` when `record_paths` is off), and
   `mouse_click` up.
4. Consecutive `mouse_move` samples at least `min_move_px` (3) apart join a
   path. A gap over `min_wait_ms` (100) closes it.
5. Same-direction scroll notches at one point within `min_wait_ms` sum into
   one `mouse_scroll`.
6. A key press and its release with no other key between become one
   `key_combo`. Keys pressed while modifiers are held become a `key_combo`
   with the modifiers. Repeated presses of a held key collapse. A press
   with no release, or a release with no press, is dropped and logged.
7. The gap between two rows runs from the last event of the earlier row to
   the first of the later one. A `key_combo` ends at its main key's
   release, so modifiers released after it count towards the gap. Over
   `min_wait_ms` the gap becomes a `wait` row with that duration; otherwise
   it becomes the later row's `delay_ms`.
8. Recording starts with a window row for the active window unless that is
   an application window.

## File format (version 3)

```json
{
  "version": 3,
  "filter": ["mouse_move"],
  "path_mode": "recorded",
  "rows": [
    {"label": "top",
     "events": [{"type": "window_focus", "window": "xed", "match_mode": "substring",
                 "rect": [100, 80, 900, 600], "client_rect": [100, 110, 900, 570]}]},
    {"delay_ms": 60,
     "events": [{"type": "mouse_click", "x": 240, "y": 300, "button": "left"}]},
    {"enabled": false, "breakpoint": true, "comment": "skip for now",
     "events": [{"type": "wait_pixel", "x": 10, "y": 10, "colour": "#ff0000",
                 "timeout_ms": 5000, "on_timeout": "goto", "target": "top"}]},
    {"events": [{"type": "mouse_move", "x": 1, "y": 2},
                {"type": "mouse_move", "x": 5, "y": 9, "dt": 0.012}]}
  ]
}
```

- An event dict holds `type`, the params whose value differs from the
  FieldSpec default, and `dt` when non-zero. Row fields with default values
  are omitted. There are no ids.
- `filter` holds names from `{mouse_move, mouse_click, mouse_scroll,
  keyboard, wait}`. `path_mode` is `recorded`, `linear`, `spline` or
  `natural`.
- Reading: bad JSON, a version other than 3, an unknown type or key, a
  value of the wrong kind, a multi-event row that is not all `mouse_move`,
  or a negative `dt` rejects the file; every error is listed with its row
  number. Semantic problems (expression syntax, duplicate or missing
  labels, missing image files) load and are reported by `validate`.
- Writing resolves symlinks, writes `.<name>.tmp` beside the target with the
  target's mode, flushes, fsyncs, `os.replace`s and fsyncs the directory.
  The document's `saved_revision` and path update only after that succeeds.

## Action catalogue

```python
class FieldKind(Enum):
    NUMBER, COORD_PAIR, REGION, DURATION, TEXT, TEMPLATE, CHOICE, FILE,
    COMMAND, BOOL, COLOUR, LABEL_TARGET, VAR_NAME, KEY, KEY_COMBO, INFO

@dataclass(frozen=True)
class FieldSpec:
    keys: tuple[str, ...]        # one key; two for COORD_PAIR; five for REGION
    label: str
    kind: FieldKind
    default: object = None
    choices: tuple[str, ...] = ()
    allow_expr: bool = True      # NUMBER, COORD_PAIR, DURATION, REGION only
    minimum: float | None = None
    maximum: float | None = None
    integer: bool = False
    file_types: tuple[tuple[str, str], ...] = ()
    none_label: str | None = None        # LABEL_TARGET: text for "no target"
    visible_when: Callable[[dict], bool] | None = None
    enabled_when: Callable[[dict], bool] | None = None
    targeting_fills: tuple[str, ...] = ()   # keys a screen capture fills
    help: str = ""

@dataclass(frozen=True)
class Preset:
    name: str                    # Add-menu text
    insert_key: str | None       # single key while the table has focus
    make: Callable[[], MacroEvent]

class Flow(Enum): NEXT, STOP
@dataclass(frozen=True) class Jump: target: str        # label, "Start" or "End"
@dataclass(frozen=True) class Abort: message: str

@dataclass(frozen=True)
class ActionSpec:
    type: str
    name: str                    # "Mouse Click"
    category: str                # Mouse, Keyboard, Wait, Window, Variable, Control, Find, Misc
    filter_group: str | None     # mouse_move, mouse_click, mouse_scroll, keyboard, wait or None
    fields: tuple[FieldSpec, ...]
    presets: tuple[Preset, ...]
    layout: Callable[[Row], tuple[FieldSpec, ...]]   # default: fields
    read: Callable[[Row], dict]                       # field values for the panel
    write: Callable[[Row, dict], Row]                 # panel values back to a row
    describe: Callable[[Row], str]                    # Value column, CLI
    overlay: Callable[[Row], OverlayShape | None]
    play: Callable[[PlayContext, Row], Flow | Jump | Abort]
```

- The registry maps type to spec. `catalogue.make(type, **params)` builds an
  event. `catalogue.spec_for(row)` returns the row's spec; a path row's
  layout is Start (COORD_PAIR, numbers only; editing translates every
  point; targeting translates), End (INFO), Points (INFO), Duration
  (DURATION, numbers only; editing scales every `dt`).
- Validation uses `fields`: kind, range, `allow_expr`, both halves of a pair
  alike, label targets present, variable names legal, files present.
- `PlayContext` exposes `resolve(value) -> float`, `coord(x, y) ->
  tuple[float, float]` (frame applied), `region(values) -> Rect`,
  `render(template) -> str`, `sleep(ms) -> bool` (True when stopped),
  `variables`, `input`, `screen`, `windows`, `host`, `settings`, `speed`,
  `log(text)` and `row_id`. Action modules import only the Protocols from
  `catalogue.py`.

### Action types

| Type | Category | Fields (defaults) | Playback |
|---|---|---|---|
| `mouse_move` | Mouse | x, y (COORD_PAIR, targetable) | Set the pointer. A path row replays its points under `recorded`, else one move along the path mode over the recorded span. |
| `mouse_move_timed` | Mouse | x, y; x2, y2 (COORD_PAIR); duration_ms (500) | Move from the first point to the second along the path mode. |
| `mouse_click` | Mouse | x, y; button (left; right, middle); action (click; down, up); count (1, 1..3); hold_ms (50); interval_ms (80) | Move to (x, y) if the pointer is over 2 px away, then press and release `count` times. `down` and `up` do one half. Presets: Click (C), Right-click (R), Click down, Click up. |
| `mouse_scroll` | Mouse | x, y (optional); dx, dy (NUMBER, 0 and 3); notch_ms (30) | Move to (x, y) when set, then one notch at a time. Preset: Scroll (S). |
| `key_combo` | Keyboard | keys (KEY_COMBO, "a"); hold_ms (50) | Press modifiers then the key, hold, release in reverse. Preset: Key (H). |
| `key_press`, `key_release` | Keyboard | key (KEY) | One half of a hold. |
| `type_text` | Keyboard | text (TEMPLATE); char_delay_ms (0); duration_ms (0, exclusive with char_delay_ms); humanise (false, +-50% jitter) | Type each character through XTest, pressing Shift or Level3 as the keymap needs. Preset: Text (T). |
| `wait` | Wait | duration_ms (500); random_max_ms (0) | Sleep; when `random_max_ms` > duration the sleep is uniform between the two. Preset: Wait (W). |
| `wait_pixel` | Wait | x, y (targetable, fills colour too); colour (COLOUR "#ffffff"); mode (equals; differs); colour_tolerance (0, 0..255 per channel); timeout_ms (10000); on_timeout (abort; continue, goto); target | Poll every 50 ms. On timeout: abort with "Row N: pixel (x, y) did not become #rrggbb within T ms", continue, or jump. Preset: Pixel (P). |
| `window_focus` | Window | window (TEXT); match_mode (substring; regex); rect, client_rect (INFO, from recording); maximised (BOOL); launch (COMMAND); timeout_ms (none = setting); on_missing (default; skip, halt, goto); target; restore_rect (default; yes, no) | See Window handling. Preset: Window focus. |
| `var_set` | Variable | var_name; expr (TEMPLATE-free expression) | Assign. Preset: Set variable. |
| `ocr_read` | Variable | var_name; region (REGION) | OCR the region into the variable. |
| `goto` | Control | target (LABEL_TARGET, "End") | Jump. A missing label aborts. Preset: Goto (G). |
| `if_else` | Control | expr ("x > 0"); target; else_target (none_label "next row") | Jump to target when true, else to else_target or the next row. Preset: If. |
| `repeat` | Control | target; count (3) | The body is the rows from `target` to this row. On reaching the row the counter increments; if counter < count it jumps to target, else it resets to 0 and falls through. `count` is read when the counter is 0; count <= 1 falls through. Counters reset at each program pass. `target` must be at or above the row. The dynamic value `loop` is the innermost active repeat's pass. Preset: Repeat. |
| `match_image` | Find | image_path (FILE); region; confidence (0.9, 0..1); timeout_ms (5000); var_name ("found"); capture_var, capture_var_y; click_found (false); on_timeout (continue; abort, goto); target | Poll every 300 ms until the template is found. Sets var_name 0/1, the centre into the capture vars (frame applied), clicks the centre when asked. |
| `match_text` | Find | expr (TEMPLATE); region; similarity (0.8); timeout_ms; var_name; capture_var; on_timeout; target | As above with OCR text compared by normalised Levenshtein similarity. |
| `exec_program` | Misc | command (COMMAND, template; each `{expr}` is shell-quoted); wait_exit (false); timeout_ms (0 = none); var_name (exit code) | Run through `/bin/sh` with cwd = the macro's folder or `$HOME`, `VIRTUAL_ENV` removed, the venv stripped from `PATH`, `start_new_session`. Launch failure aborts; a non-zero exit is logged; a timeout is logged and stores -1 in the exit-code variable. A stop or an abort kills the process group of every program started without Wait for exit; when playback reaches its end they keep running. Preset: Run program. |
| `message_box` | Misc | title (""); text (TEMPLATE); buttons (ok; ok_cancel, yes_no); target_ok, target_cancel, target_yes, target_no (LABEL_TARGET, none = next row, Cancel defaults to End); var_name | `host.ask` blocks until answered. Esc anywhere stops playback. Closing the dialog counts as Cancel or No. |
| `notify` | Misc | text (TEMPLATE) | `notify-send`; logged if unavailable. |
| `beep` | Misc | none | XBell through the window manager's connection. |

### Expressions

Grammar: numbers, `'strings'`, variable names, `+ - * / // % **` (exponent
capped at 1000), comparisons (chained), `and`, `or`, `not`, parentheses and
the functions `abs round floor ceil min max int str len rnd(a, b)`
(round's digit count capped at 1000 either way). Truth is
a non-zero number or a non-empty string. Any evaluation error aborts
playback with "Row N: message". `{expr}` segments in templates render with
`str()`; `{{` and `}}` are literal braces.

Dynamic values, read-only: `rt_ms` (playback time excluding pauses),
`op_num`, `ex_num`, `iteration`, `loop`, `mouse_x`, `mouse_y`, `win_x`,
`win_y`, `win_w`, `win_h`, `timestamp_ms`, `random`.

## Playback engine

`Player(document_snapshot, settings, host, input, windows, screen)`.
`play()` runs on the caller's thread; `stop()`, `resume()` from any thread.

- The program is the row tuple. The program counter walks it; disabled rows
  and rows whose spec `filter_group` is in the document filter are skipped.
  A jump to a skipped row continues at the next row that plays.
- Each row's spec `play` returns `NEXT`, `Jump(target)`, `Abort(message)` or
  `STOP`. The engine owns the counter, window context, repeat counters,
  breakpoints, timing, stop and pause.
- **Window context** is `(window row id, matched X id, client rect,
  recorded client rect)` or None. On landing on a row the engine finds its
  governing window row (the nearest enabled, unfiltered window row above).
  If that differs from the context's row id the window row runs first, or
  the context clears when there is none. A window found missing in this
  pass is remembered, so later jumps into its group skip straight to the
  next window row without waiting again. Launch commands run at most once
  per pass per window row.
- A missing window: `skip` jumps to the next window row or End; `halt`
  aborts; `goto` jumps to the row's target.
- Breakpoints pause before the row. On pause the engine releases every held
  key and button and remembers them, posts `on_paused(row id, variables)`,
  and blocks until `resume()` or `stop()`. On resume it re-activates the
  context window (no launch, no move) and waits for confirmation, re-presses
  the remembered keys and buttons (moving the pointer back to where a held
  button was released, following the context window if it moved), and
  resets the schedule clock. The same
  happens when `ctx.host.ask` returns: the engine wraps the host, so an
  action that asks does not call `reactivate_context` itself. Paused time is
  excluded from `rt_ms`.
- Repeat count applies to the whole program; 0 repeats until stopped. A
  program in which no row plays ends at once with a log line.
- There is no step limit. A run of more than 10 000 rows without a wait in
  one second sleeps 1 ms per row so Esc stays responsive.
- `stop()` sets the stop flag and every wait event, including the pause
  event and any pending `ask`. A stop that lands just before a breakpoint
  ends the run instead of pausing. The player always releases held keys and
  buttons on exit and always calls `on_finished(error)` last.

`PlayerHost` Protocol: `on_row(row_id)`, `on_log(line)`,
`on_paused(row_id, variables)`, `on_finished(error: str | None)` and the
blocking `ask(title, text, buttons) -> str | None`. The GUI host posts to the
main-thread queue and waits on an Event that `stop()` also sets. The CLI host
uses `zenity` when present and otherwise answers the first button and logs it.

### Window handling

- Matching: substring (case-insensitive) or regex over `_NET_WM_NAME`, an
  exact title winning. Windows on other workspaces count; minimised ones
  are activated like any other.
- Activation: `_NET_CURRENT_DESKTOP` to the window's desktop unless it is
  sticky, then `_NET_ACTIVE_WINDOW` with source indication 2 and the server
  time (from a PropertyNotify on the manager's own window). Then poll
  `_NET_ACTIVE_WINDOW` and the geometry every 50 ms until two reads agree
  or `window_settle_ms` (1000) passes. A dialog whose `_NET_WM_STATE` holds
  `_NET_WM_STATE_MODAL` and whose `WM_TRANSIENT_FOR` chain leads to the
  window counts as the window being active (Muffin focuses a window's modal
  dialog instead). Failure counts as missing.
- Restore: when the recorded state was maximised the engine sets that state
  and does not move. Otherwise, when `restore_rect` resolves to yes and the
  recorded outer rect lies inside a current monitor's work area, it sends
  `_NET_MOVERESIZE_WINDOW` for the outer rect and polls until stable. A
  size that still differs from the recording is logged with the row number.
- Launch: when the first lookup fails and the row has a launch command it
  runs at once, then the engine polls for up to `timeout_ms`. After the
  first match it waits `window_settle_ms` before input.

## Recording

`Recorder(listener, windows, session)` keeps a raw stream of
`(kind, time, data)` from the XRecord listener plus `window_focus` markers
from the active-window watcher.

- Mouse events are filtered by hit test: on every press and scroll the
  recorder queries the pointer on its own connection, climbs to the
  top-level and drops the event when that id is registered as an
  application window (the overlay excluded). A dropped press drops its
  release and the moves between. Key events are dropped while
  `_NET_ACTIVE_WINDOW` is an application window. The watcher never emits a
  marker for an application window.
- Application window ids come from `session.own_windows`: every Tk
  toplevel registers `tk_toplevel_xid(winfo_id())` on `<Map>` and
  unregisters on `<Destroy>`. The window manager built for the GUI is given
  the same registry, so `list()` and `find()` never return an application
  window and a window row cannot match Macro Recorder's own title.
- F6 and the record hotkey are never recorded. When the record combo stops
  a recording, the recorder drops the combo's final press and walks back
  removing the trailing modifier presses without releases. Releases arriving
  after stop are discarded.
- The listener is started with `start()` then `wait()` before the recorder
  reports ready, so the first events are not lost. If the listener thread
  dies the recorder stops with an error.

## Platform layer

| Concern | Implementation |
|---|---|
| Session | `session.check()` refuses recording and playback when `XDG_SESSION_TYPE` is `wayland`, `WAYLAND_DISPLAY` is set, or `DISPLAY` is unset. The GUI shows a dialog and disables Record and Play; the CLI exits 3 (2 is argparse's usage error). |
| Keys | The model stores X keysym names (`a`, `KP_1`, `Return`, `Control_L`, `F6`). Combos are `ctrl+shift+s` with aliases `ctrl`, `shift`, `alt`, `super` and are shown as `Ctrl+Shift+S` (a Details key field shows the stored form when the shown form would parse back to a different value, such as `ctrl+S`; letters in a typed combination are stored in lower case). A lone `+` and a trailing `++` (`ctrl++`) are the plus key; a single trailing `+` (`ctrl+`) leaves the key out and is an error. `input.py` looks a name up in the active XKB group under the live lock state (`KeyboardMap.find_for_state`), borrowing a spare keycode for keysyms the group lacks and restoring it 150 ms after the last key event when playback ends (a borrow that another client's keymap reload overwrote is mapped afresh). `press_key` adds Shift only for a keypad keysym or an upper-case letter the live Num Lock or Caps Lock state would otherwise turn into the other level, and holds it until that key is released (or `release_all`), so autorepeat keeps the level; Caps Lock adds Shift in `type_char` only on a key whose two levels are a lower and upper case pair. A `live_x` test resolves every name in the table. |
| Injection | XTest only, through `InputBackend`: `move(x, y)`, `press_button`, `release_button`, `scroll(dx, dy)`, `press_key(name)`, `release_key(name)`, `type_text(text)`. The backend counts every synthetic key it sends, by keycode, for the stop-key fallback. |
| Recording stream | `InputListener` on XRecord with its own two connections (one blocks in the record context, one controls it): KeyPress and KeyRelease with keycode and state mapped to a keysym name, ButtonPress, ButtonRelease and MotionNotify with root coordinates. |
| Stop key | `StopKeyWatcher` selects `XI_RawKeyPress` on the root through XInput2 (python-xlib provides the masks; `x11.py` registers a raw event decoder with `ge_add_event_data`) and ignores events whose `sourceid` is the `Virtual core XTEST keyboard` slave. Synthetic Esc, including autorepeat of a held synthetic Esc, never counts as the user. Where XInput2 2.1 is unavailable the fallback uses XRecord and the injector's counter, ignoring Esc while the injector holds it. |
| Global hotkeys | `HotkeyGrabber` grabs each combo on the root with `XGrabKey` in the Lock and Mod2 mask variants, so other applications do not see it. A failed grab is reported in the Settings dialog: refusals from startup when it opens, and new ones on OK, where a second OK keeps them. `Ctrl+Alt+F1..F12`, `Ctrl+Alt+Backspace` and `Ctrl+Alt+Delete` are rejected. Defaults: record toggle `Ctrl+Shift+F9`, play `Ctrl+Shift+F5`. Esc and F6 are observed, never grabbed. The Play combo waits (up to 3 s) until no key is down before it plays or continues, because the grab activated by the press sends every key event, XTest ones included, to the grabbing connection until the key is released. |
| Windows | EWMH through one locked connection, with core-protocol fallbacks for Xvfb. `WindowManager`: `list()`, `active()`, `find(pattern, mode)`, `activate(win, settle_ms) -> WindowInfo | None`, `move(win, rect, settle_ms) -> WindowInfo | None`, `set_maximised(win, bool)`, `watch_active(callback) -> stop`, `bell()`, `monitors()`. `WindowInfo` carries id, title, outer rect, client rect, desktop, maximised. The outer rect is the visible one: the client grown by `_NET_FRAME_EXTENTS` and shrunk by a header-bar window's `_GTK_FRAME_EXTENTS` shadow. Each monitor's work area comes from `_GTK_WORKAREAS_D<n>`, else from cutting panel struts out of the monitor, else from `_NET_WORKAREA`. `ConnectionClosedError` drops the cached display. |
| Screen | mss for capture; monitors from `sct.monitors`. Pixel reads capture a 1x1 region. |
| Overlay | Tk override-redirect toplevel shaped to the drawn marks with an empty input region, shaped before it is mapped. Hidden while recording, playing or when the application loses focus; a shape set while unfocused is drawn when the focus returns. |
| Targeting | Its own connection grabs pointer and keyboard on the root (`owner_events` false) and checks both replies, retrying for 500 ms then cancelling with a message. While grabbed it reads ButtonPress, ButtonRelease and KeyPress: the first button press (wheel notches are ignored) captures the point and the colour under it, the matching release ends targeting, Space captures without a click, Esc cancels. Both grabs are released in a `finally`. |
| OCR | Tesseract via pytesseract. |

## GUI

- **Layout**: a File menubutton and an Edit menubutton, then ribbon tabs
  Record and Edit (Add menubuttons per category, Record, Stop), Playback
  (Play or Continue, Stop, Speed, Repeat with 0 meaning until stopped and
  blank invalid, Filter menubutton, Mouse path, Window timeout, If window
  missing, Restore windows; two rows), View (Output log, Overlay), Help
  (Keyboard shortcuts, which opens the shortcut text in a message box,
  and About). Below: the table with the Details panel on the right; a
  bottom pane with Output Log and Variables tabs; a footer with the row
  count, elapsed time and log toggle.
- **File menu**: New, Open, Recent (10), Save, Save As, Settings, Exit.
  **Edit menu**: Undo, Redo, Cut, Copy, Paste, Delete, Split path, Toggle
  enabled (Ctrl+E), Toggle breakpoint (F8), Select All. Menubuttons use
  persistent menus with `postcommand`. The table has a right-click context
  menu with the Edit items and an Add submenu from the presets.
- **Table**: columns number, Action, Value, Label, Comment. Treeview iid is
  the row id. Disabled rows show `||` in the number column and grey text;
  filtered rows show their number in brackets and italics; a breakpoint
  shows `*` in the number column; each window group has its own tint. The table
  applies `Change` notifications incrementally. Any document change,
  scroll, column resize or mode change cancels an open inline editor;
  Return or FocusOut commits it only if the row still exists. The Value
  column and the Output Log show typed newlines and tabs as `\n` and `\t`. Inline label
  edits validate live. Double-click on the number column toggles the
  breakpoint. Drag drops before the hovered row's upper half or after its
  lower half, with a line marking the position; with no visible row under
  the pointer (the heading, or below the rows) it drops before the first
  visible row or after the last visible one, so a scrolled table never
  sends rows to its far end. The context menu keeps the grab `tk_popup`
  takes, so a click elsewhere closes it.
- **Highlight**: while playing, the running row gets exactly the tag
  `playing` and `>` in the number column; the previous row gets its
  computed tags back. Selection is saved on entering playing and restored
  by id on return to idle. The Details panel and overlay stay on the saved
  selection. While paused the overlay shows the row about to run.
- **Details panel**: one widget per FieldSpec of `spec.layout(row)`. Each
  widget owns a StringVar traced on write, suppressed while populating. A
  change validates only that field and, if valid, commits only its keys
  through `spec.write` inside the row's edit session. Invalid text turns
  red and reverts on FocusOut, selection change, undo, redo or a mode
  change. An expression naming a variable that nothing in the document
  assigns is amber and still committed. Numbers show in ms with up to three
  decimals. An Enabled checkbox and the Delay field are always present.
- **Commands and modes**: every user command is a `Command(name, run,
  enabled_in: set[Mode])`; menus and buttons take their state from it on
  each mode change and handlers re-check it. Modes: idle, targeting,
  recording, playing, paused. Entering recording, playing or paused commits
  or cancels the inline editor, reverts red fields, sets the table to
  `selectmode none` with its mouse and key bindings returning `break`, and
  disables the Details widgets. Paused allows Continue, Stop and Toggle
  breakpoint only. Global hotkeys are ignored while a modal grab is active
  or the mode is targeting.
- **Shortcuts**: Ctrl+N/O/S, Ctrl+Shift+S, F5 (play or continue), F9
  (record toggle) and F8 (so it works while paused) on the toplevel. Ctrl+X/C/V, Ctrl+A, Ctrl+E, F8, Delete,
  Alt+Up/Down, Return (edit label), F2 (edit comment) and the preset insert
  keys on the table widget only, so Entry widgets keep text semantics.
  Ctrl+Z/Y on the toplevel, and inside an Entry they still undo the
  document. An insert fires only when `state & (Control | Mod1 | Mod4)`
  is 0; both letter cases are bound; each table binding returns `break`.
  New rows go below the selected row and become the selection.
- **Marshalling**: worker threads post `(kind, payload)` to one
  `queue.SimpleQueue`. A main-thread pump runs every 50 ms while not idle
  and every 200 ms when idle. Each tick drains all log lines, applies only
  the most recent active-row report, then handles paused, ask and finished
  in order. The player thread body is wrapped so `finished` always arrives
  with any exception text.
- **Recording**: Record inserts the bundled rows below the selected row
  (or at the end) as one undo step when recording stops. During recording
  the table shows provisional rows under a `recording` tag, built
  incrementally from `recorder.events_since(cursor)`; they are not in the
  document. No unsaved-changes prompt on Record.
- **Unsaved changes**: Save / Don't save / Cancel on New, Open, a recent
  file and Exit. Text typed in the inline Label or Comment editor is
  committed first (and before Save), so it counts. Return presses the
  focused button, else Save; the same holds in the message_box dialog.
  File and colour choosers opened from the Details panel count as modal. Title `name* - Macro Recorder` or `Untitled - Macro
  Recorder`; the name is the file stem.
- **Output Log**: follows new lines until the user scrolls away from the
  end, also while hidden; its saved height is placed on the first layout
  tall enough for it and is saved on exit only once it was placed.
- **Settings**: `$XDG_CONFIG_HOME/macro-recorder/settings.json`. Playback:
  speed, repeat, window_timeout_ms, window_settle_ms, on_missing,
  restore_rect, path_mode default, min_spacing_ms. Recording: record_paths,
  min_move_px, min_wait_ms, click_gap_ms, double_click_ms. Hotkeys: record,
  play. UI: geometry, log_visible, log_height, overlay_enabled, last_folder,
  recent (default geometry 1200x720). Written atomically on dialog OK and
  on exit. An unreadable file is
  renamed `settings.json.bad` and replaced with defaults. The dialog edits
  Recording, Window and Hotkeys; the hotkey grabber is suspended while it
  is open and fields capture by key press. Return presses the focused
  button, or OK. The Playback tab holds the
  playback values.
- **Errors**: `app.main` configures logging to
  `$XDG_CACHE_HOME/macro-recorder/app.log` and the Output Log (WARNING and
  up), sets `report_callback_exception` and `threading.excepthook`. An error
  that ends a run is shown in a dialog naming the row.
- Interface text is ASCII. Fonts are Tk's defaults. Removed from the
  previous GUI: goto by instruction number, Speed and Repeat as
  expressions, the Debug highlight checkbox, per-group alternating stripes,
  everything Win32.

## CLI

`record [output] [--stop-key F6]`, `play FILE [--speed] [--repeat]
[--window-timeout] [--on-missing] [--no-filter] [--verbose]`, `list FILE`
(the Value text per row, with row numbers and labels, typed newlines and
tabs escaped so each row is one line), `validate FILE` (exit 1 with one line
per problem). `list` and `validate` need no display. Execution errors print
"Error: Row N: message" and exit 1. Ctrl+C prints "Stopped" and exits 130.
Esc prints "Stopped" and exits 0 under `--repeat 0`, where it is the normal
way to end, and 130 otherwise. No X11 session exits 3; output closed early
exits 141 without a traceback.

## Tests

- `tests/conftest.py` provides fakes for `WindowManager`, `InputBackend`,
  `InputListener`, `StopKeyWatcher`, `HotkeyGrabber`, `Screen` and
  `PlayerHost`, plus a fake clock. An autouse fixture fails any test not
  marked `live_x` that creates a Tk root or opens an X connection.
- `live_x` tests run only with `MACRO_RECORDER_LIVE_X=1`, which
  `scripts/test-live.sh` sets under `xvfb-run -a`. They cover the GUI smoke
  path, overlay shaping, the window manager, the XTest injector against the
  XRecord listener, and the stop-key watcher.
- The file format has a round-trip test of every field of every type; the
  player is tested with the fake clock; bundling with synthetic streams.
