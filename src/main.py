"""CLI entry point for the Macro Recorder.

Subcommands
-----------
record  — capture mouse and keyboard input to a JSON file
play    — replay a recorded macro JSON file
list    — print a formatted table of events from a macro file

Usage
-----
    python -m src.main record [output.json] [--stop-key KEY]
    python -m src.main play <file.json> [--speed FLOAT] [--repeat INT] [--stop-key KEY]
    python -m src.main list <file.json>
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from src.event_types import EventType
from src.macro import load_macro, save_macro, MacroEvent
from src.recorder import Recorder
from src.player import Player, WindowNotFoundError


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Macro Recorder — record and replay system-wide mouse and keyboard input.",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    # --- record ---
    rec = subs.add_parser("record", help="Record a new macro to a JSON file.")
    rec.add_argument(
        "output",
        nargs="?",
        default=None,
        metavar="OUTPUT",
        help="Output JSON path (default: recording_YYYYMMDD_HHMMSS.json).",
    )
    rec.add_argument(
        "--stop-key",
        default="Key.f6",
        metavar="KEY",
        help="Key to stop recording (default: Key.f6).",
    )

    # --- play ---
    play = subs.add_parser("play", help="Replay a recorded macro.")
    play.add_argument("file", metavar="FILE", help="Macro JSON file to replay.")
    play.add_argument(
        "--speed",
        type=float,
        default=1.0,
        metavar="MULTIPLIER",
        help="Playback speed multiplier, e.g. 0.5 or 2.0 (default: 1.0).",
    )
    play.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help="Number of times to repeat (0 = infinite, default: 1).",
    )
    play.add_argument(
        "--stop-key",
        default="Key.esc",
        metavar="KEY",
        help="Key to abort playback (default: Key.esc).",
    )
    play.add_argument(
        "--window-timeout",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="Seconds to wait for a target window before applying the missing "
             "policy (default: 5.0).",
    )
    play.add_argument(
        "--on-missing-window",
        choices=("skip", "halt"),
        default="skip",
        help="What to do when a target window is not found (default: skip).",
    )

    # --- list ---
    lst = subs.add_parser("list", help="List all events in a macro file.")
    lst.add_argument("file", metavar="FILE", help="Macro JSON file to inspect.")

    return parser


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_record(args: argparse.Namespace) -> None:
    """Record system input until the stop key is pressed, then save to JSON."""
    output = args.output or (
        "recording_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"
    )
    recorder = Recorder(stop_key=args.stop_key)
    print("Recording... press %s to stop." % args.stop_key)
    groups = recorder.start()  # blocks
    save_macro(groups, output)
    total = sum(len(g.events) for g in groups)
    print("Recorded %d event(s) in %d group(s) → %s"
          % (total, len(groups), output))


def cmd_play(args: argparse.Namespace) -> None:
    """Load a macro file and replay it with the given options."""
    groups = load_macro(args.file)  # raises FileNotFoundError if missing
    repeat_str = "infinite" if args.repeat == 0 else str(args.repeat)
    print(
        "Playing %s  (speed=%.2fx, repeat=%s) ... press %s to stop."
        % (args.file, args.speed, repeat_str, args.stop_key)
    )
    player = Player(
        speed=args.speed,
        repeat=args.repeat,
        stop_key=args.stop_key,
        window_timeout=args.window_timeout,
        on_missing_window=args.on_missing_window,
    )
    player.play(groups)  # blocks
    print("Playback complete.")


def cmd_list(args: argparse.Namespace) -> None:
    """Print a formatted table of events from a macro file, grouped by window."""
    groups = load_macro(args.file)
    header = "%-5s  %-16s  %10s  %s" % ("#", "Type", "Timestamp", "Parameters")
    i = 0
    total = 0
    for group in groups:
        scope = group.window if group.window else "(no window)"
        rect = ""
        if group.recorded_rect:
            r = group.recorded_rect
            rect = "  [%d,%d %dx%d]" % (r.left, r.top, r.width, r.height)
        print("\n=== Window: %s%s ===" % (scope, rect))
        print(header)
        print("-" * len(header))
        for ev in group.events:
            print("%-5d  %-16s  %10.4f  %s"
                  % (i, ev.type, ev.ts, _format_params(ev)))
            i += 1
            total += 1
    print("\n%d event(s) total in %d group(s)." % (total, len(groups)))


def _format_params(ev: MacroEvent) -> str:
    """Return a compact human-readable summary of an event's parameters."""
    if ev.type == EventType.MOUSE_MOVE:
        return "x=%d, y=%d" % (ev.x, ev.y)
    if ev.type in (EventType.KEY_PRESS, EventType.KEY_RELEASE):
        return "key=%s" % ev.key
    if ev.type == EventType.MOUSE_CLICK:
        direction = "down" if ev.pressed else "up"
        return "x=%d, y=%d, btn=%s (%s)" % (ev.x, ev.y, ev.button, direction)
    if ev.type == EventType.MOUSE_SCROLL:
        return "x=%d, y=%d, dx=%d, dy=%d" % (ev.x, ev.y, ev.dx, ev.dy)
    if ev.type == EventType.VAR_SET:
        return "%s = %s" % (ev.var_name, ev.expr)
    if ev.type == EventType.OCR_READ:
        return "%s = OCR(%s,%s -> %s,%s)" % (ev.var_name, ev.x, ev.y, ev.dx, ev.dy)
    if ev.type == EventType.MATCH_IMAGE:
        region = "monitor %s" % ev.monitor if ev.monitor is not None else \
            "%s,%s -> %s,%s" % (ev.x, ev.y, ev.dx, ev.dy)
        return "%s = wait image %r in (%s) conf=%s" % (
            ev.var_name, ev.image_path, region, ev.tolerance)
    if ev.type == EventType.MATCH_TEXT:
        region = "monitor %s" % ev.monitor if ev.monitor is not None else \
            "%s,%s -> %s,%s" % (ev.x, ev.y, ev.dx, ev.dy)
        return "%s = wait text %r in (%s) tol=%s" % (
            ev.var_name, ev.expr, region, ev.tolerance)
    if ev.type == EventType.TYPE_TEXT:
        return 'type "%s"' % ev.expr
    if ev.type == EventType.GOTO:
        return "goto %s" % _goto_target(ev)
    if ev.type == EventType.GOTO_IF:
        return "if (%s) goto %s" % (ev.expr, _goto_target(ev))
    return ""


def _goto_target(ev: MacroEvent) -> str:
    """Goto destination text: an instruction number (``#N``) or a label."""
    return "#%s" % ev.target_index if ev.target_index is not None else str(ev.target)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse arguments and dispatch to the appropriate subcommand handler."""
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "record":
            cmd_record(args)
        elif args.command == "play":
            cmd_play(args)
        elif args.command == "list":
            cmd_list(args)
    except FileNotFoundError as e:
        print("Error: %s" % e, file=sys.stderr)
        sys.exit(1)
    except WindowNotFoundError as e:
        print("Error: %s" % e, file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print("Error: %s" % e, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
