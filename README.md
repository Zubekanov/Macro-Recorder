# Macro Recorder

Record, edit and replay mouse and keyboard macros on Linux X11 desktops
(developed on Linux Mint 22 / Cinnamon). A recording becomes a table of rows:
clicks, paths, scrolls, key combos, typed text, waits and window rows. Rows
can be edited, reordered, disabled or given breakpoints, and mixed with
variables, conditions, repeats, gotos, pixel and image waits, OCR, message
boxes and external programs. Coordinates inside a window group follow the
window when it moves.

## Install on Linux Mint

Get the repository with `git clone https://github.com/Zubekanov/Macro-Recorder.git`
(or download it as a zip from that page and unpack it), then run from its folder:

```
./scripts/install-mint.sh
```

This installs `tesseract-ocr` (OCR actions) and `xvfb` (tests), installs uv
if it is missing, runs `uv sync` and adds "Macro Recorder" to the
applications menu. It is safe to re-run.

By hand:

```
sudo apt install tesseract-ocr xvfb
curl -LsSf https://astral.sh/uv/install.sh | sh    # if uv is not installed yet
source $HOME/.local/bin/env                         # puts the new uv on PATH in this shell
uv sync
```

uv installs its own CPython (`python-preference = "only-managed"` in
`pyproject.toml`), which bundles tkinter, so the system `python3-tk` is not
needed. OCR uses Tesseract's English model.

## X11 only

Recording uses XRecord, playback XTest, the stop keys XInput2 and the
hotkeys XGrabKey; Wayland offers none of them. In a Wayland session the GUI
disables Record and Play and the CLI exits with status 3 and this message:

```
This is a Wayland session. Recording and playback need X11: log out and
choose 'Cinnamon' (not 'Cinnamon (Wayland)') at the login screen.
```

Window rows use EWMH and work with Muffin (Cinnamon), Mutter, Xfwm, Marco
and other compliant window managers.

## Run

```
uv run macro-recorder                      # GUI
uv run macro-recorder-cli record out.json  # record until F6
uv run macro-recorder-cli play out.json    # Esc stops playback
uv run macro-recorder-cli list out.json    # rows with numbers and labels
uv run macro-recorder-cli validate out.json
```

`play` takes `--speed`, `--repeat` (0 repeats until stopped),
`--window-timeout MS`, `--on-missing skip|halt`, `--no-filter` and
`--verbose`; `record` takes `--stop-key KEY`. `list` and `validate` need no
display. Run `uv run macro-recorder-cli COMMAND --help` for details.

Exit status: 0 done, 1 an error, 2 a bad command line, 3 no X11 session,
130 stopped by Ctrl+C or Esc (Esc under `--repeat 0` exits 0), 141 output
closed early (for example piped into `head`).

Settings live in `$XDG_CONFIG_HOME/macro-recorder/settings.json` and the log
in `$XDG_CACHE_HOME/macro-recorder/app.log`.

## Shortcuts and stop keys

| Where | Keys |
|---|---|
| Anywhere on the desktop | Ctrl+Shift+F9 record or stop, Ctrl+Shift+F5 play or continue (change them in Settings) |
| While recording | F6 stops recording (never recorded) |
| While playing or paused | Esc stops playback; synthetic Esc from the macro does not count |
| Main window | Ctrl+N New, Ctrl+O Open, Ctrl+S Save, Ctrl+Shift+S Save As, Ctrl+Z Undo, Ctrl+Y Redo, F5 Play or Continue, F9 Record or Stop |
| Table | Ctrl+X/C/V Cut, Copy, Paste, Ctrl+A Select all, Ctrl+E Toggle enabled, F8 Toggle breakpoint, Delete, Alt+Up/Down Move, Return Edit label, F2 Edit comment |
| Table, insert a row | M Move, C Click, R Right-click, S Scroll, H Key, T Text, W Wait, P Pixel, G Goto |

The Help tab shows the same list with the current hotkeys.

## File format

Macros are version 3 JSON files: a list of rows, each holding one event (or
a recorded path of `mouse_move` events), with defaults omitted. See
[docs/DESIGN.md](docs/DESIGN.md) for the format, the action catalogue and the
playback rules, and [docs/ROADMAP.md](docs/ROADMAP.md) for known gaps.

## Tests

```
uv run pytest                 # unit tests; live X tests are skipped
./scripts/test-live.sh        # everything, including the live X tests, under a private Xvfb
./scripts/test-live.sh -k overlay
```

The live tests open windows and inject input, so they run only with
`MACRO_RECORDER_LIVE_X=1`, which `scripts/test-live.sh` sets under
`xvfb-run`. They never use your desktop.
