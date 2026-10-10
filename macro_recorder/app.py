"""GUI entry point: logging, session check, settings, the Tk root and the shell."""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from macro_recorder.gui import settings as settings_module
from macro_recorder.platform import session

log = logging.getLogger("macro_recorder")

LOG_FILE_BYTES = 1_000_000
LOG_FILE_BACKUPS = 2


class QueueLogHandler(logging.Handler):
    """Feeds WARNING and up into the Output Log through the shell's queue."""

    def __init__(self, post: Callable[[str, Any], None], level: int = logging.WARNING) -> None:
        super().__init__(level)
        self._post = post

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._post("log", "%s: %s" % (record.levelname.capitalize(), record.getMessage()))
        except Exception:
            self.handleError(record)


def configure_logging(directory: Path | None = None) -> Path:
    """Log INFO and up to ``$XDG_CACHE_HOME/macro-recorder/app.log``; returns the path."""
    directory = directory or settings_module.cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "app.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_FILE_BYTES,
                                                   backupCount=LOG_FILE_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return path


def install_error_hooks(root: Any, report: Callable[[str], None],
                        post: Callable[[str, Any], None]) -> None:
    """Log and surface exceptions from Tk callbacks and from worker threads.

    ``report(message)`` runs on the main thread; thread errors reach it
    through the queue.
    """
    def tk_error(exc_type: type, value: BaseException, tb: Any) -> None:
        log.error("Unhandled error in the interface", exc_info=(exc_type, value, tb))
        report("%s: %s" % (exc_type.__name__, value))

    def thread_error(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        log.error("Unhandled error in thread %s", name,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        message = "%s: %s" % (args.exc_type.__name__, args.exc_value)
        post("call", lambda: report(message))

    root.report_callback_exception = tk_error
    threading.excepthook = thread_error


def main(argv: list[str] | None = None) -> int:
    """Start the GUI; an optional argument names a macro file to open."""
    argv = sys.argv[1:] if argv is None else argv
    log_path = configure_logging()
    problem = session.check()
    settings = settings_module.load_settings()

    import tkinter as tk

    from macro_recorder.gui import dialogs
    from macro_recorder.gui.main_window import MainWindow, X11ToplevelIds
    from macro_recorder.gui.playback import Platform, default_factories

    try:
        root = tk.Tk(className="macro-recorder")
    except tk.TclError as e:
        print("Cannot start the interface: %s" % e, file=sys.stderr)
        return 2
    platform = Platform(default_factories(session.own_windows)) if problem is None else None
    resolver = X11ToplevelIds() if problem is None else None
    window = MainWindow(root, settings, own_windows=session.own_windows, platform=platform,
                        session_problem=problem, resolve_xid=resolver)
    logging.getLogger().addHandler(QueueLogHandler(window.post))

    showing_error = [False]

    def report(message: str) -> None:
        if showing_error[0]:
            return
        showing_error[0] = True
        # The dialog's grab would block the stop key and the Stop button.
        window.stop_playback()
        try:
            dialogs.show_message(root, "Error", "%s\n\nDetails are in %s" % (message, log_path))
        finally:
            showing_error[0] = False

    install_error_hooks(root, report, window.post)
    log.info("Started; session problem: %s", problem.code if problem else "none")
    if problem is not None:
        log.warning("Recording and playback are disabled: %s", problem.message)
        root.after(200, lambda: window.show_message("Recording and playback unavailable",
                                                    problem.message))
    else:
        root.after(100, window.start_hotkeys)
    if argv:
        root.after_idle(lambda: window.open_path(argv[0]))
    try:
        root.mainloop()
    except KeyboardInterrupt:
        log.info("Interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
