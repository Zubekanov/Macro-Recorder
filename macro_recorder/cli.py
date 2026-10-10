"""Command line: ``record``, ``play``, ``list`` and ``validate``.

``list`` and ``validate`` read a file and never touch the X server; the
platform modules are imported only inside the factories that ``record`` and
``play`` call, so those two commands work without a display or python-xlib.

Decisions where the design is silent:

- ``play`` uses the default playback settings with the command-line options
  on top, and ``record`` the default recording settings, so a command line
  behaves the same whatever the GUI last saved.
- ``play`` ignores breakpoints: a terminal has no Continue button.
- ``record`` without an output path writes ``recording-YYYYmmdd-HHMMSS.json``
  in the current directory.
- ``validate`` exits 0 when the file has only warnings.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Callable, Sequence

from macro_recorder.gui.settings import RecordingSettings, Settings
from macro_recorder.model import fileformat, validate
from macro_recorder.model.actions.misc import BUTTON_SETS
from macro_recorder.model.catalogue import spec_for
from macro_recorder.model.document import MacroDocument
from macro_recorder.model.keys import display_key, parse_combo
from macro_recorder.platform import session

if TYPE_CHECKING:
    from macro_recorder.model.protocols import (
        InputBackend, InputListener, Screen, StopKeyWatcher, WindowManager,
    )

POLL_S = 0.05
ASK_POLL_S = 0.1
EXIT_ERROR = 1
EXIT_SESSION = 2
EXIT_INTERRUPTED = 130

_sleep = time.sleep


def _error(text: str) -> None:
    print("Error: %s" % text, file=sys.stderr)


# ---------------------------------------------------------------------------
# Platform factories (replaced by fakes in tests)
# ---------------------------------------------------------------------------

def make_listener() -> InputListener:
    """The XRecord listener ``record`` reads."""
    from macro_recorder.platform.input import XRecordListener
    return XRecordListener()


def make_window_manager() -> WindowManager:
    """The EWMH window manager for recording markers and playback."""
    from macro_recorder.platform.windows import X11WindowManager
    return X11WindowManager()


def make_input() -> InputBackend:
    """The XTest injector ``play`` drives."""
    from macro_recorder.platform.input import XTestInput
    return XTestInput()


def make_screen() -> Screen:
    """Screen capture, pixel reads, image find and OCR."""
    from macro_recorder.engine.screen import XScreen
    return XScreen()


def make_stop_watcher(input: InputBackend) -> StopKeyWatcher:
    """The Esc watcher; it shares the injector's record of synthetic keys."""
    from macro_recorder.platform.input import XI2StopKeyWatcher
    return XI2StopKeyWatcher(synthetic=getattr(input, "synthetic", None))


# ---------------------------------------------------------------------------
# The player host
# ---------------------------------------------------------------------------

class CliHost:
    """``PlayerHost`` for a terminal.

    Log lines are printed only when ``verbose``; row progress never.  ``ask``
    shows a zenity dialog when zenity is installed, otherwise prints the
    message and answers its first button.  The final error is printed to
    stderr and kept in :attr:`error`.
    """

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self._stopped: Callable[[], bool] = lambda: False
        self.error: str | None = None

    def attach(self, stopped: Callable[[], bool]) -> None:
        """Give ``ask`` the player's stop flag so a stop closes an open dialog."""
        self._stopped = stopped

    def on_row(self, row_id: int) -> None:
        pass

    def on_log(self, line: str) -> None:
        if self.verbose:
            print(line, file=sys.stdout, flush=True)

    def on_paused(self, row_id: int, variables: object) -> None:
        pass

    def on_finished(self, error: str | None) -> None:
        self.error = error
        if error:
            print("Error: %s" % error, file=sys.stderr, flush=True)

    def ask(self, title: str, text: str, buttons: str) -> str | None:
        choices = BUTTON_SETS[buttons]
        if self._stopped():
            return None
        zenity = shutil.which("zenity")
        if zenity is not None:
            try:
                return self._ask_zenity(zenity, title, text, choices)
            except OSError as e:
                self.on_log("zenity failed to start: %s" % e)
        return self._ask_fallback(title, text, choices)

    def _ask_zenity(self, zenity: str, title: str, text: str,
                    choices: tuple[str, ...]) -> str | None:
        """Run zenity until it is answered or playback stops (None)."""
        command = [zenity, "--no-markup", "--title", title or "Macro Recorder", "--text", text]
        if choices == ("ok",):
            command[1:1] = ["--info"]
        else:
            command[1:1] = ["--question", "--ok-label", choices[0].capitalize(),
                            "--cancel-label", choices[1].capitalize()]
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        while True:
            try:
                code = proc.wait(timeout=ASK_POLL_S)
                break
            except subprocess.TimeoutExpired:
                if self._stopped():
                    proc.terminate()
                    proc.wait()
                    return None
        if self._stopped():
            return None
        return choices[0] if code == 0 else choices[-1]

    def _ask_fallback(self, title: str, text: str, choices: tuple[str, ...]) -> str:
        """Print the message and answer the first button."""
        if title:
            print(title, file=sys.stdout)
        print(text, file=sys.stdout)
        print("Answered %s (no zenity)" % choices[0], file=sys.stdout, flush=True)
        return choices[0]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _load(path: str) -> MacroDocument | None:
    """The document, or None after printing why it cannot be read."""
    try:
        return fileformat.load(path)
    except fileformat.FormatError as e:
        _error(str(e))
    except OSError as e:
        _error("cannot read %s: %s" % (path, e.strerror or e))
    return None


def _session_refusal() -> int | None:
    """Exit code 2 after printing the problem, or None when X11 input works."""
    problem = session.check()
    if problem is None:
        return None
    _error(problem.message)
    return EXIT_SESSION


def problem_lines(problems: Sequence[validate.Problem]) -> list[str]:
    """``Row N: message`` per problem, warnings prefixed ``warning:``."""
    return [("warning: %s" % p) if p.severity == "warning" else str(p) for p in problems]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def list_lines(document: MacroDocument) -> list[str]:
    """One line per row: number, enabled marker, delay, label, action, value, comment.

    A window row starts a group: a blank line comes before it and the rows
    of an enabled group are indented under it.
    """
    rows = document.rows
    label_width = max((len(r.label) for r in rows), default=0)
    number_width = len(str(len(rows)))
    lines: list[str] = []
    in_group = False
    for number, row in enumerate(rows, 1):
        spec = spec_for(row)
        if row.is_window:
            if lines:
                lines.append("")
            in_group = row.enabled
            indent = ""
        else:
            indent = "  " if in_group else ""
        marker = "  " if row.enabled else "||"
        delay = "%d ms" % row.delay_ms if row.delay_ms else ""
        parts = ["%*d %s %8s" % (number_width, number, marker, delay)]
        if label_width:
            parts.append("%-*s" % (label_width, row.label))
        parts.append("%s%s: %s" % (indent, spec.name, spec.describe(row)))
        line = "  ".join(parts)
        if row.comment:
            line += "  # %s" % row.comment
        lines.append(line.rstrip())
    return lines


def cmd_list(args: argparse.Namespace) -> int:
    document = _load(args.file)
    if document is None:
        return EXIT_ERROR
    for line in list_lines(document):
        print(line)
    return 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def cmd_validate(args: argparse.Namespace) -> int:
    try:
        document = fileformat.load(args.file)
    except fileformat.FormatError as e:
        for problem in e.problems:
            print(problem)
        return EXIT_ERROR
    except OSError as e:
        _error("cannot read %s: %s" % (args.file, e.strerror or e))
        return EXIT_ERROR
    problems = validate.check(document)
    for line in problem_lines(problems):
        print(line)
    if any(p.severity == "error" for p in problems):
        return EXIT_ERROR
    if not problems:
        print("%s: no problems" % args.file)
    return 0


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------

def _stop_key(text: str) -> str:
    """argparse type: one keysym name, not a combination."""
    try:
        names = parse_combo(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    if len(names) != 1:
        raise argparse.ArgumentTypeError("%r is a combination; give one key" % text)
    return names[0]


def default_output() -> str:
    return time.strftime("recording-%Y%m%d-%H%M%S.json")


def cmd_record(args: argparse.Namespace) -> int:
    refusal = _session_refusal()
    if refusal is not None:
        return refusal
    from macro_recorder.engine.recorder import Recorder, RecorderError
    from macro_recorder.model.bundling import rows_from_stream

    output = args.output or default_output()
    try:
        recorder = Recorder(make_listener(), make_window_manager(), session.own_windows,
                            stop_key=args.stop_key)
        recorder.start()
    except (OSError, RecorderError) as e:
        _error(str(e))
        return EXIT_ERROR
    print("Recording... press %s to stop" % display_key(args.stop_key), flush=True)
    failure: str | None = None
    cursor = 0
    try:
        while recorder.recording:
            try:
                _, cursor = recorder.events_since(cursor)
            except RecorderError as e:
                failure = str(e)
                break
            _sleep(POLL_S)
    except KeyboardInterrupt:
        failure = "recording interrupted"
    finally:
        recorder.stop()
    rows = rows_from_stream(recorder.result(), RecordingSettings())
    try:
        fileformat.save(MacroDocument(rows), output)
    except (OSError, fileformat.FormatError) as e:
        _error("cannot save %s: %s" % (output, e))
        return EXIT_ERROR
    print("Saved %d rows to %s" % (len(rows), output))
    if failure is not None:
        _error("%s; what was captured is saved" % failure)
        return EXIT_ERROR
    return 0


# ---------------------------------------------------------------------------
# play
# ---------------------------------------------------------------------------

def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not a number" % text) from None
    if not value > 0 or value == float("inf"):
        raise argparse.ArgumentTypeError("must be above 0")
    return value


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not a whole number" % text) from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def playback_settings(args: argparse.Namespace) -> Settings:
    """Default settings with the command-line playback options applied."""
    settings = Settings()
    playback = settings.playback
    if args.speed is not None:
        playback.speed = args.speed
    if args.repeat is not None:
        playback.repeat = args.repeat
    if args.window_timeout is not None:
        playback.window_timeout_ms = args.window_timeout
    if args.on_missing is not None:
        playback.on_missing = args.on_missing
    return settings


def playable(document: MacroDocument, no_filter: bool) -> MacroDocument:
    """The program the CLI plays: breakpoints cleared, the filter dropped on request."""
    rows = [replace(r, breakpoint=False) if r.breakpoint else r for r in document.rows]
    return MacroDocument(rows, filter=() if no_filter else document.filter,
                         path_mode=document.path_mode, path=document.path)


def _wait_for(thread: threading.Thread) -> None:
    """Join in short slices so Ctrl+C reaches the main thread."""
    while thread.is_alive():
        thread.join(POLL_S)


def cmd_play(args: argparse.Namespace) -> int:
    refusal = _session_refusal()
    if refusal is not None:
        return refusal
    document = _load(args.file)
    if document is None:
        return EXIT_ERROR
    problems = validate.check(document)
    errors = [p for p in problems if p.severity == "error"]
    if errors:
        for p in errors:
            print(str(p), file=sys.stderr)
        _error("%s has %d problem(s); not playing" % (args.file, len(errors)))
        return EXIT_ERROR
    if args.verbose:
        for line in problem_lines(problems):
            print(line, file=sys.stderr)

    from macro_recorder.engine.player import Player

    host = CliHost(verbose=args.verbose)
    try:
        input = make_input()
    except OSError as e:
        _error(str(e))
        return EXIT_ERROR
    try:
        player = Player(playable(document, args.no_filter), playback_settings(args), host,
                        input, make_window_manager(), make_screen(),
                        stop_key_watcher=make_stop_watcher(input))
        host.attach(player.stopped)
        thread = threading.Thread(target=player.play, name="player", daemon=True)
        thread.start()
        try:
            _wait_for(thread)
        except KeyboardInterrupt:
            player.stop()
            thread.join()
            print("Stopped", file=sys.stderr)
            return EXIT_INTERRUPTED
    except OSError as e:
        _error(str(e))
        return EXIT_ERROR
    finally:
        input.close()
    return EXIT_ERROR if host.error else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="macro-recorder-cli",
                                     description="Record, play and check macro files.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    record = commands.add_parser("record", help="record a macro until the stop key")
    record.add_argument("output", nargs="?", help="file to write (default: recording-<time>.json)")
    record.add_argument("--stop-key", type=_stop_key, default="F6", metavar="KEY",
                        help="key that ends the recording (default: F6)")
    record.set_defaults(run=cmd_record)

    play = commands.add_parser("play", help="play a macro; Esc stops it")
    play.add_argument("file")
    play.add_argument("--speed", type=_positive_float, help="playback speed (default: 1)")
    play.add_argument("--repeat", type=_non_negative_int,
                      help="passes over the macro, 0 until stopped (default: 1)")
    play.add_argument("--window-timeout", type=_non_negative_int, metavar="MS",
                      help="how long to wait for a window, in ms (default: 5000)")
    play.add_argument("--on-missing", choices=("skip", "halt"),
                      help="what to do when a window is missing (default: skip)")
    play.add_argument("--no-filter", action="store_true",
                      help="play the rows the document's filter would skip")
    play.add_argument("--verbose", action="store_true", help="print each row as it plays")
    play.set_defaults(run=cmd_play)

    listing = commands.add_parser("list", help="print the rows of a macro")
    listing.add_argument("file")
    listing.set_defaults(run=cmd_list)

    check = commands.add_parser("validate", help="check a macro for problems")
    check.add_argument("file")
    check.set_defaults(run=cmd_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return its exit code."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
