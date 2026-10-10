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
# How often a reaper checks whether an unwaited child's group still exists.
GROUP_POLL_S = 0.5


class ProcessError(RuntimeError):
    """A command could not be started or ran past its timeout."""


class ProcessTimeout(ProcessError):
    """A waited-for command ran past its timeout and was terminated."""


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
        stop_event: threading.Event,
        children: list[subprocess.Popen] | None = None) -> int | None:
    """Run ``command`` through ``/bin/sh``.

    ``cwd`` falls back to ``$HOME`` when it is None or not a directory.
    Without ``wait`` the command is left running and None is returned; when
    ``children`` is given the process is appended to it, so the caller can
    end its group later with :func:`terminate`.  With ``wait`` the exit code
    is returned; ``timeout_ms`` > 0 limits the wait.  When ``stop_event`` is
    set, or the timeout passes, the process group is terminated.  A stop
    returns None; a timeout raises ProcessTimeout and a command that cannot
    be started raises ProcessError.
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
        if children is not None:
            children.append(proc)
        threading.Thread(target=_reap, args=(proc, children), name="reaper-%d" % proc.pid,
                         daemon=True).start()
        return None
    deadline = time.monotonic() + timeout_ms / 1000.0 if timeout_ms and timeout_ms > 0 else None
    while True:
        try:
            return proc.wait(timeout=POLL_S)
        except subprocess.TimeoutExpired:
            pass
        if stop_event.is_set():
            terminate([proc])
            return None
        if deadline is not None and time.monotonic() >= deadline:
            terminate([proc])
            raise ProcessTimeout("%r did not finish within %d ms" % (command, int(timeout_ms)))


def _reap(proc: subprocess.Popen, children: list[subprocess.Popen] | None) -> None:
    """Reap an unwaited child, then drop it from ``children`` once its group is gone.

    The kernel does not reuse a number while a process group of that number
    exists, so dropping the entry soon after the group ends keeps
    :func:`terminate` from signalling an unrelated group that later gets the
    same number.
    """
    proc.wait()
    if children is None:
        return
    while _group_alive(proc):
        time.sleep(GROUP_POLL_S)
    try:
        children.remove(proc)
    except ValueError:
        pass


def terminate(procs: list[subprocess.Popen]) -> None:
    """End the process groups of ``procs``: SIGTERM, then SIGKILL after the grace period.

    The groups are signalled together.  A group counts as gone only when no
    member is left, so a grandchild that ignores SIGTERM is killed even after
    its group leader has exited.
    """
    procs = list(procs)         # reapers may drop entries meanwhile
    groups = [p for p in procs if _signal_group(p, signal.SIGTERM)]
    deadline = time.monotonic() + TERM_GRACE_S
    while groups and time.monotonic() < deadline:
        time.sleep(POLL_S)
        groups = [p for p in groups if _group_alive(p)]
    for p in groups:
        _signal_group(p, signal.SIGKILL)
    for p in procs:
        try:
            p.wait(timeout=TERM_GRACE_S)
        except subprocess.TimeoutExpired:
            log.warning("Process %d did not exit after SIGKILL", p.pid)


def _signal_group(proc: subprocess.Popen, sig: int) -> bool:
    """Send ``sig`` to the group led by ``proc``.  False when the group is gone."""
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError as e:
        log.warning("Could not signal process group %d: %s", proc.pid, e)
        return False
    return True


def _group_alive(proc: subprocess.Popen) -> bool:
    """True while any member of the group led by ``proc`` exists (the leader is reaped first)."""
    proc.poll()
    return _signal_group(proc, 0)
