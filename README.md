# Macro Recorder

Record, edit and replay mouse and keyboard macros. Runs on Linux desktops with
X11 (developed against Linux Mint / Cinnamon) and on Windows.

```
uv sync
uv run macro-recorder              # GUI
uv run macro-recorder-cli --help   # CLI: record / play / list
uv run pytest
```

## Linux Mint

One-shot setup, which installs the system packages, syncs the Python
environment and adds a "Macro Recorder" entry to the applications menu:

```
./scripts/install-mint.sh
```

Or by hand:

```
sudo apt install python3-tk tesseract-ocr          # Tk for the GUI, Tesseract for OCR actions
curl -LsSf https://astral.sh/uv/install.sh | sh    # if uv is not installed yet
uv sync
uv run macro-recorder
```

What to know on Linux:

- **X11 session required.** Global input capture and playback go through
  XRecord/XTest, which Wayland does not expose. Mint's default
  `Cinnamon` session is X11; if you picked `Cinnamon (Wayland)` at the login
  screen, switch back.
- **Windows.** Window matching, moving and focusing use the EWMH protocol and
  work with Muffin (Cinnamon), Mutter, Xfwm, Marco and any other compliant
  window manager. Titles are matched exactly as the title bar shows them.
- **Launch commands** on window rows run through `/bin/sh`, so `xed`,
  `gnome-calculator` or `firefox https://example.org` all work.
- **OCR** (`ocr_read`, `match_text`) uses Tesseract. Extra languages come from
  `tesseract-ocr-<lang>` packages.
- **Preview overlay.** The on-screen marker for the selected row uses the X
  SHAPE extension so it never blocks clicks.
- **Headless tests.** `xvfb-run -a uv run pytest` runs the whole suite,
  including the GUI and X11 window-manager tests, without a real display.

## Windows

`uv sync` pulls `pywin32` and `winsdk` automatically; window management uses
Win32 and OCR uses the built-in Windows OCR engine. Recording input from
elevated (UAC) windows requires running as Administrator.

## File format and design

See [docs/DESIGN.md](docs/DESIGN.md).
