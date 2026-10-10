"""scripts/desktop-entry.sh escapes paths for the applications-menu entry."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "desktop-entry.sh"
TEMPLATE = ROOT / "packaging" / "macro-recorder.desktop"


def unescape_string(value: str) -> str:
    """The Desktop Entry string escapes: \\s \\n \\t \\r \\\\."""
    out, i = [], 0
    table = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value) and value[i + 1] in table:
            out.append(table[value[i + 1]])
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def split_exec(value: str) -> list[str]:
    """Arguments of an Exec value as the specification reads them."""
    text = unescape_string(value)
    args, current, i, quoted, started = [], [], 0, False, False
    while i < len(text):
        c = text[i]
        if quoted:
            if c == "\\" and i + 1 < len(text) and text[i + 1] in '"`$\\':
                current.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                quoted = False
            else:
                current.append(c)
        elif c == '"':
            quoted, started = True, True
        elif c == " ":
            if current or started:
                args.append("".join(current))
            current, started = [], False
        else:
            current.append(c)
        i += 1
    if current or started:
        args.append("".join(current))
    return [a.replace("%%", "%") for a in args]


def entry(uv: str, repo: str) -> dict[str, str]:
    out = subprocess.run(["bash", str(SCRIPT), str(TEMPLATE), uv, repo],
                         check=True, capture_output=True, text=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


@pytest.mark.parametrize("repo", [
    "/home/u/Macro-Recorder",
    "/home/u/My Code/Macro-Recorder",
    "/home/u/R&D/Macro|Recorder",
    '/home/u/a "b" $HOME `x` \\back 100%/Macro-Recorder',
])
def test_paths_survive_the_round_trip(repo):
    uv = "/home/u/.local/bin/uv"
    fields = entry(uv, repo)
    assert split_exec(fields["Exec"]) == [uv, "run", "--project", repo, "macro-recorder"]
    assert unescape_string(fields["Path"]) == repo
    assert "@" not in fields["Exec"] and "@" not in fields["Path"]
