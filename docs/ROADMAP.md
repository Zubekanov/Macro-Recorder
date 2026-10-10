# Roadmap

The refactor described in `DESIGN.md` was delivered as work packages. Each
package owned the files listed; nothing outside a package edited them. WP0
landed first and alone, WP10 last and alone; the rest ran in parallel against
the WP0 contracts and the fakes in `tests/conftest.py`. All packages are
done; version 0.3.0 is the first release built from them.

| WP | Status | Scope | Owns |
|---|---|---|---|
| 0 | Done | Contracts | `model/events.py`, `model/keys.py`, `model/catalogue.py`, `model/actions/__init__.py` (imports the category modules), `model/document.py` (full), `gui/settings.py` (`Settings` dataclass, load and save only), `platform/session.py`, `tests/conftest.py`, `tests/test_document.py` |
| 1 | Done | Model core | `model/fileformat.py`, `model/validate.py`, `engine/expressions.py`, their tests |
| 2 | Done | Recording | `model/bundling.py`, `engine/recorder.py`, `gui/recording.py`, their tests |
| 3 | Done | Engine | `engine/player.py`, `engine/paths.py`, `engine/processes.py`, their tests |
| 4 | Done | Actions A | `model/actions/mouse.py`, `keyboard.py`, `wait.py`, `engine/screen.py`, their tests |
| 5 | Done | Actions B | `model/actions/window.py`, `variables.py`, `control.py`, `find.py`, `misc.py`, their tests |
| 6 | Done | Platform | `platform/x11.py`, `platform/windows.py`, `platform/input.py`, `gui/hotkeys.py`, `gui/targeting.py`, their tests (`live_x`) |
| 7 | Done | GUI shell | `app.py`, `gui/main_window.py`, `gui/commands.py`, `gui/table.py`, `gui/log_panel.py`, `gui/playback.py`, `gui/dialogs.py`, their tests |
| 8 | Done | Editors | `gui/details.py`, `gui/fields.py`, `gui/overlay.py`, their tests |
| 9 | Done | Settings dialog and CLI | the dialog part of `gui/settings.py`, `cli.py`, their tests |
| 10 | Done | Integration | delete the old modules and tests, `README.md`, `pyproject.toml` (pynput dropped), `uv.lock`, `scripts/install-mint.sh`, `scripts/test-live.sh`, GUI smoke test under Xvfb, packaging check |

## Known gaps

- `NullWindowManager` (used when the X display cannot be opened) still
  waits the window timeout for every window row before skipping its group.
- The Details panel does not scroll, so a layout taller than the pane is
  clipped.
- The overlay is not recreated after its X connection drops.
- A breakpoint toggled while paused applies from the next run: the player
  plays a snapshot and has no `Player.set_breakpoint` for the GUI to call.

## Out of scope this round

Smart Record and SmartClick; AI actions; embedding another macro; data
lists; clipboard actions; wait for hotkey, text input, file event or desktop
change; wait until a clock time; Repeat by duration, until time or until the
last data item; Search and Replace; user-made row groups (Ctrl+G); partial
playback (modifier-click on Play, `--start-at`); a separate Variable Explorer
window; single-step debugging; stop on user input; a custom stop hotkey;
post-playback shutdown or lock; scheduling; a tray icon; CSV export; `.mrf`
import; per-macro desktop shortcuts; OCR language choice and regex text
find; barcode and QR capture; bitmap variables; dark mode; translation;
display-layout change detection.
