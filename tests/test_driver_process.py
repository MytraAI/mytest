"""Driver processes have to outlive the console interrupt that stops a test.

Ctrl+C reaches every process attached to the console. A driver killed by it
dies at the same instant teardown starts commanding it, so "drop the 48 V bus"
reaches a socket nobody is serving and the stand is left energized by the
keystroke meant to stop it.
"""
from __future__ import annotations

from hardware import driver_process


class FakePopen:
    last = None

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        FakePopen.last = self


def _start(monkeypatch, platform):
    monkeypatch.setattr(driver_process, "sys", type("sys", (), {"platform": platform}))
    monkeypatch.setattr(driver_process.subprocess, "Popen", FakePopen)
    driver_process.start_driver(["python", "-m", "hardware.odrive.main"])
    return FakePopen.last


def test_a_driver_is_started_outside_the_console_s_process_group_on_windows(monkeypatch):
    started = _start(monkeypatch, "win32")
    assert started.kwargs["creationflags"] == 0x00000200, "CREATE_NEW_PROCESS_GROUP"
    assert "start_new_session" not in started.kwargs, "not a Windows argument"


def test_a_driver_is_started_in_its_own_session_on_posix(monkeypatch):
    started = _start(monkeypatch, "darwin")
    assert started.kwargs["start_new_session"] is True
    assert "creationflags" not in started.kwargs, "not a POSIX argument"


def test_the_command_itself_is_passed_through_unchanged(monkeypatch):
    started = _start(monkeypatch, "linux")
    assert started.args == ["python", "-m", "hardware.odrive.main"]
