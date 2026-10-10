"""Run window launch and exec_program commands.

Commands go through ``/bin/sh -c`` in a new session, so stopping playback
can signal the whole process group.  The application's virtualenv is hidden
from the child: ``VIRTUAL_ENV`` is removed and the venv's ``bin`` directory
is stripped from ``PATH``.  A child that is not waited for is reaped by a
daemon thread so it never lingers as a zombie.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

POLL_S = 0.05
TERM_GRACE_S = 2.0


class ProcessError(RuntimeError):
    """A command could not be started or ran past its timeout."""


def child_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a child: no ``VIRTUAL_ENV``, no venv ``bin`` on ``PATH``."""
    env = dict(os.environ if environ is None else environ)
    venv_bins = set()
    venv = env.pop("VIRTUAL_ENV", None)
    if venv:
        venv_bins.add(os.path.normpath(os.path.join(venv, "bin")))
    if sys.prefix != sys.base_prefix:
        venv_bins.add(os.path.normpath(os.path.join(sys.prefix, "bin")))
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(
            p for p in env["PATH"].split(os.pathsep) if p and os.path.normpath(p) not in venv_bins)
    return env


def run(command: str, cwd: str | None, wait: bool, timeout_ms: float,
        stop_event: threading.Event) -> int | None:
    """Run ``command`` through ``/bin/sh``.

    ``cwd`` falls back to ``$HOME`` when it is None or not a directory.
    Without ``wait`` the command is left running and None is returned.  With
    ``wait`` the exit code is returned; ``timeout_ms`` > 0 limits the wait.
    When ``stop_event`` is set, or the timeout passes, the process group gets
    SIGTERM (SIGKILL two seconds later).  A stop returns None; a timeout
    raises ProcessError, as does a command that cannot be started.
    """
    if not command.strip():
        raise ProcessError("The command is empty")
    if not cwd or not os.path.isdir(cwd):
        cwd = str(Path.home())
    try:
        proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=cwd, env=child_env(),
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        raise ProcessError("Could not run %r: %s" % (command, e)) from e
    if not wait:
        threading.Thread(target=proc.wait, name="reaper-%d" % proc.pid, daemon=True).start()
        return None
    deadline = time.monotonic() + timeout_ms / 1000.0 if timeout_ms and timeout_ms > 0 else None
    while True:
        try:
            return proc.wait(timeout=POLL_S)
        except subprocess.TimeoutExpired:
            pass
        if stop_event.is_set():
            _terminate(proc)
            return None
        if deadline is not None and time.monotonic() >= deadline:
            _terminate(proc)
            raise ProcessError("%r did not finish within %d ms" % (command, int(timeout_ms)))


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM the process group, then SIGKILL it if it does not exit."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError as e:
            log.warning("Could not signal process group %d: %s", proc.pid, e)
            break
        try:
            proc.wait(timeout=TERM_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            continue
    try:
        proc.wait(timeout=TERM_GRACE_S)
    except subprocess.TimeoutExpired:
        log.warning("Process %d did not exit after SIGKILL", proc.pid)
