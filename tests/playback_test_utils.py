"""Test helpers to keep playback hermetic.

Playing a macro normally drives the real OS: it moves the cursor, presses
buttons/keys, and installs a global keyboard hook for the stop key.  Tests that
exercise the player only care about callbacks, timing, and variable state — not
real input — so they replace pynput's controllers and listener with mocks.

Usage in a test module::

    from tests.playback_test_utils import start_player_io_patches, stop_player_io_patches

    def setUpModule():
        start_player_io_patches()

    def tearDownModule():
        stop_player_io_patches()

The module name intentionally does not match ``test_*`` so unittest discovery
does not collect it.
"""

from unittest import mock

_patchers = []


def start_player_io_patches() -> None:
    """Replace pynput controllers/listener in macro_recorder.player with mocks."""
    global _patchers
    _patchers = [
        mock.patch("macro_recorder.player.mouse.Controller"),
        mock.patch("macro_recorder.player.keyboard.Controller"),
        mock.patch("macro_recorder.player.keyboard.Listener"),
    ]
    for p in _patchers:
        p.start()


def stop_player_io_patches() -> None:
    for p in _patchers:
        p.stop()
    _patchers.clear()
