#!/usr/bin/env bash
# Run the whole test suite, including the live X tests, under a private Xvfb
# so nothing touches the real desktop.  Needs: sudo apt install xvfb
#
#   ./scripts/test-live.sh            # everything
#   ./scripts/test-live.sh -k shape   # pytest arguments pass through

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

if ! command -v xvfb-run >/dev/null 2>&1; then
    echo "xvfb-run not found: sudo apt install xvfb" >&2
    exit 2
fi
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"

MACRO_RECORDER_LIVE_X=1 xvfb-run -a -s "-screen 0 1600x1000x24" \
    "$UV" run --no-sync pytest -q -p no:cacheprovider "$@"
