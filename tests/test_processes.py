"""Running launch and exec_program commands with real shell commands."""

import os
import subprocess
import sys
import threading
import time

import pytest

from macro_recorder.engine.processes import ProcessError, child_env, run


def _wait_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        with open("/proc/%d/stat" % pid) as f:
            if f.read().split()[2] == "Z":
                return True
        time.sleep(0.02)
    return False


def test_exit_codes():
    stop = threading.Event()
    assert run("true", None, True, 0, stop) == 0
    assert run("false", None, True, 0, stop) == 1
    assert run("exit 3", None, True, 0, stop) == 3


def test_runs_in_cwd_or_home(tmp_path):
    stop = threading.Event()
    assert run("pwd > out.txt", str(tmp_path), True, 0, stop) == 0
    assert (tmp_path / "out.txt").read_text().strip() == str(tmp_path)
    out = tmp_path / "home.txt"
    assert run("pwd > '%s'" % out, None, True, 0, stop) == 0
    assert out.read_text().strip() == os.path.expanduser("~")
    assert run("pwd > '%s'" % out, str(tmp_path / "missing"), True, 0, stop) == 0
    assert out.read_text().strip() == os.path.expanduser("~")


def test_unwaited_returns_none_and_is_reaped(tmp_path):
    stop = threading.Event()
    marker = tmp_path / "done"
    assert run("touch '%s'" % marker, None, False, 0, stop) is None
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists()
    time.sleep(0.1)
    assert not any(t.name.startswith("reaper-") for t in threading.enumerate())


def test_stop_kills_the_process_group(tmp_path):
    stop = threading.Event()
    pidfile = tmp_path / "pid"
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    result = run("sleep 30 & echo $! > '%s'; wait" % pidfile, None, True, 0, stop)
    assert result is None
    assert time.monotonic() - started < 3
    assert _wait_gone(int(pidfile.read_text()))


def test_timeout_kills_and_raises():
    stop = threading.Event()
    started = time.monotonic()
    with pytest.raises(ProcessError, match="did not finish within 200 ms"):
        run("sleep 30", None, True, 200, stop)
    assert time.monotonic() - started < 3


def test_empty_and_unstartable_commands(monkeypatch):
    stop = threading.Event()
    with pytest.raises(ProcessError, match="empty"):
        run("  ", None, True, 0, stop)

    def refuse(*args, **kwargs):
        raise OSError("no such file")
    monkeypatch.setattr(subprocess, "Popen", refuse)
    with pytest.raises(ProcessError, match="Could not run"):
        run("anything", None, False, 0, stop)


def test_child_env_hides_the_virtualenv():
    venv = "/opt/venv"
    env = child_env({"VIRTUAL_ENV": venv, "PATH": "/opt/venv/bin:/usr/bin:/bin", "HOME": "/h"})
    assert "VIRTUAL_ENV" not in env
    assert env["PATH"].split(os.pathsep)[-2:] == ["/usr/bin", "/bin"]
    assert "/opt/venv/bin" not in env["PATH"].split(os.pathsep)
    if sys.prefix != sys.base_prefix:
        assert os.path.join(sys.prefix, "bin") not in env["PATH"].split(os.pathsep)


def test_child_sees_the_cleaned_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/opt/venv")
    monkeypatch.setenv("PATH", "/opt/venv/bin" + os.pathsep + os.environ["PATH"])
    out = tmp_path / "env.txt"
    assert run("printf '%%s|%%s' \"${VIRTUAL_ENV-unset}\" \"$PATH\" > '%s'" % out,
               None, True, 0, threading.Event()) == 0
    venv, path = out.read_text().split("|")
    assert venv == "unset"
    assert "/opt/venv/bin" not in path.split(os.pathsep)


def test_new_session(tmp_path):
    out = tmp_path / "sid"
    assert run("ps -o sid= -p $$ > '%s'" % out, None, True, 0, threading.Event()) == 0
    assert int(out.read_text()) != os.getsid(0)
