#!/usr/bin/env bash
# Set up Macro Recorder on Linux Mint (or any Debian/Ubuntu-based X11 desktop).
#
# - installs the system packages the app needs (Tk for the GUI, Tesseract for OCR)
# - installs uv if it is missing, then syncs the Python environment
# - adds a "Macro Recorder" launcher to the applications menu
#
# Safe to re-run.  Run it from anywhere: it locates the repository itself.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Installing system packages (sudo)"
sudo apt-get update -qq
sudo apt-get install -y python3-tk tesseract-ocr

if ! command -v uv >/dev/null 2>&1; then
    echo "==> Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
UV="$(command -v uv)"

echo "==> Syncing the Python environment"
(cd "$REPO" && "$UV" sync)

echo "==> Adding the applications-menu launcher"
APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"
sed -e "s|@UV@|$UV|g" -e "s|@REPO@|$REPO|g" \
    "$REPO/packaging/macro-recorder.desktop" > "$APPS_DIR/macro-recorder.desktop"
chmod +x "$APPS_DIR/macro-recorder.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" || true

if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
    echo
    echo "NOTE: you are in a Wayland session. Global input recording and playback need X11;"
    echo "      pick 'Cinnamon' (not 'Cinnamon (Wayland)') at the login screen."
fi

echo
echo "Done. Start it from the menu, or with:  cd '$REPO' && uv run macro-recorder"
