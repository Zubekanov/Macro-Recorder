# Roadmap

The refactor described in `DESIGN.md` is delivered as work packages. Each
package owns the files listed; nothing outside a package edits them. WP0
lands first and alone, WP10 last and alone; the rest run in parallel against
the WP0 contracts and the fakes in `tests/conftest.py`.

| WP | Scope | Owns |
|---|---|---|
| 0 | Contracts | `model/events.py`, `model/keys.py`, `model/catalogue.py`, `model/actions/__init__.py` (imports the category modules), `model/document.py` (full), `gui/settings.py` (`Settings` dataclass, load and save only), `platform/session.py`, `tests/conftest.py`, `tests/test_document.py` |
| 1 | Model core | `model/fileformat.py`, `model/validate.py`, `engine/expressions.py`, their tests |
| 2 | Recording | `model/bundling.py`, `engine/recorder.py`, `gui/recording.py`, their tests |
| 3 | Engine | `engine/player.py`, `engine/paths.py`, `engine/processes.py`, their tests |
| 4 | Actions A | `model/actions/mouse.py`, `keyboard.py`, `wait.py`, `engine/screen.py`, their tests |
| 5 | Actions B | `model/actions/window.py`, `variables.py`, `control.py`, `find.py`, `misc.py`, their tests |
| 6 | Platform | `platform/x11.py`, `platform/windows.py`, `platform/input.py`, `gui/hotkeys.py`, `gui/targeting.py`, their tests (`live_x`) |
| 7 | GUI shell | `app.py`, `gui/main_window.py`, `gui/commands.py`, `gui/table.py`, `gui/log_panel.py`, `gui/playback.py`, `gui/dialogs.py`, their tests |
| 8 | Editors | `gui/details.py`, `gui/fields.py`, `gui/overlay.py`, their tests |
| 9 | Settings dialog and CLI | the dialog part of `gui/settings.py`, `cli.py`, their tests |
| 10 | Integration | delete the old modules and tests, `README.md`, `scripts/test-live.sh`, GUI smoke test under Xvfb, packaging check |

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
