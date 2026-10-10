"""CommandRegistry: per-mode enablement, availability, conditions and subscriptions."""

from __future__ import annotations

import pytest

from macro_recorder.gui.commands import ALL_MODES, IDLE_ONLY, Command, CommandRegistry
from macro_recorder.model.protocols import Mode


def make(registry: CommandRegistry, name: str, modes=IDLE_ONLY, when=None) -> list:
    calls: list = []
    registry.register(Command(name, name.title(), lambda *a: calls.append(a), frozenset(modes),
                              when=when))
    return calls


def test_runs_only_in_enabled_modes():
    registry = CommandRegistry()
    calls = make(registry, "save")
    assert registry.enabled("save")
    assert registry.run("save")
    registry.set_mode(Mode.PLAYING)
    assert not registry.enabled("save")
    assert not registry.run("save")
    assert calls == [()]


def test_paused_allows_only_its_commands():
    registry = CommandRegistry()
    make(registry, "play", {Mode.IDLE, Mode.PAUSED})
    make(registry, "stop", {Mode.PLAYING, Mode.PAUSED})
    make(registry, "breakpoint", {Mode.IDLE, Mode.PAUSED})
    make(registry, "delete")
    registry.set_mode(Mode.PAUSED)
    assert [n for n in registry.names() if registry.enabled(n)] == ["play", "stop", "breakpoint"]


def test_run_passes_arguments():
    registry = CommandRegistry()
    calls = make(registry, "open_recent")
    registry.run("open_recent", "/tmp/a.json")
    assert calls == [("/tmp/a.json",)]


def test_when_condition_is_checked_each_time():
    registry = CommandRegistry()
    state = {"selected": False}
    calls = make(registry, "copy", when=lambda: state["selected"])
    assert not registry.run("copy")
    state["selected"] = True
    assert registry.run("copy")
    assert calls == [()]


def test_failing_condition_disables_the_command():
    registry = CommandRegistry()
    make(registry, "copy", when=lambda: 1 / 0)
    assert not registry.enabled("copy")


def test_unavailable_command_is_disabled_in_every_mode():
    registry = CommandRegistry()
    make(registry, "record", ALL_MODES)
    registry.set_available("record", False)
    for mode in Mode:
        registry.set_mode(mode)
        assert not registry.enabled("record")
    registry.set_available("record", True)
    assert registry.enabled("record")


def test_subscribers_hear_mode_and_availability_changes_once():
    registry = CommandRegistry()
    make(registry, "record")
    heard: list = []
    unsubscribe = registry.subscribe(lambda: heard.append(registry.mode))
    registry.set_mode(Mode.RECORDING)
    registry.set_mode(Mode.RECORDING)           # no change, no notification
    registry.set_available("record", False)
    registry.set_available("record", False)     # no change
    registry.refresh()
    assert heard == [Mode.RECORDING, Mode.RECORDING, Mode.RECORDING]
    unsubscribe()
    registry.set_mode(Mode.IDLE)
    assert len(heard) == 3


def test_duplicate_and_unknown_names():
    registry = CommandRegistry()
    make(registry, "save")
    with pytest.raises(ValueError):
        make(registry, "save")
    with pytest.raises(KeyError):
        registry.enabled("missing")
    with pytest.raises(KeyError):
        registry.set_available("missing", False)
    assert "save" in registry and "missing" not in registry
    assert registry.get("save").label == "Save"
