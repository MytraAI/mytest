"""Asking an operator to do something, and hearing back.

The window and the CLI both write one marker file, and the waiting step polls for
it - so neither is a special case in the step, and a stand with no display is
still answerable.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from testcases.ydrive.teststeps.teststeps import RunDetail, await_operator
from tools import operator_ack, operator_prompt


class FakeTestCase:
    test_id = "test-prompt"

    def __init__(self, ack_path):
        self.state = {}
        self._ack_path = ack_path
        self.checks = 0

    def set_state(self, name, value):
        self.state[name] = value

    def operator_ack_path(self):
        return self._ack_path

    def check_should_continue(self):
        self.checks += 1
        # Acknowledge from "elsewhere" after a few ticks, the way an operator
        # clicking the window or running the CLI tool does.
        if self.checks == 3:
            self._ack_path.touch()


class FakeWindow:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True


def test_the_window_and_the_cli_write_the_same_marker(tmp_path, monkeypatch):
    """Whichever the operator uses, the test is polling for one thing - and the
    convention is TestCase.operator_ack_path()'s, not two copies of a guess."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    from_window = operator_prompt.acknowledge("run-1")
    from_window.unlink()
    from_cli = operator_ack.acknowledge("run-1")

    assert from_window == from_cli == tmp_path / "mytest-ack-run-1"


def test_a_stale_acknowledgement_does_not_skip_the_wait(tmp_path, monkeypatch):
    """A marker left by an earlier run - or a double-click - must not let the next
    wait through without a person answering it."""
    ack = tmp_path / "mytest-ack-test-prompt"
    ack.touch()
    case = FakeTestCase(ack)
    monkeypatch.setattr("testcases.ydrive.teststeps.teststeps.spawn_operator_prompt",
                        lambda test_id, message, fields=(), choices=None: FakeWindow())
    monkeypatch.setattr("testcases.ydrive.teststeps.teststeps.OPERATOR_POLL_INTERVAL_S", 0.001)

    await_operator(case, "do the thing")

    assert case.checks >= 3, "the wait returned on the stale marker instead of a fresh one"


def test_the_prompt_is_published_while_waiting_and_cleared_after(tmp_path, monkeypatch):
    """A recorded run shows how long the stand sat waiting on a person, which is
    otherwise indistinguishable from a hang."""
    case = FakeTestCase(tmp_path / "mytest-ack-test-prompt")
    windows = []
    monkeypatch.setattr("testcases.ydrive.teststeps.teststeps.spawn_operator_prompt",
                        lambda test_id, message, fields=(), choices=None: windows.append(FakeWindow()) or windows[-1])
    monkeypatch.setattr("testcases.ydrive.teststeps.teststeps.OPERATOR_POLL_INTERVAL_S", 0.001)

    await_operator(case, "move the load")

    assert case.state["operator_prompt"] is None, "the channel still names a finished request"
    assert windows and windows[0].terminated, "the window was left open after the wait"


def test_the_window_is_closed_even_when_the_wait_is_aborted(tmp_path, monkeypatch):
    """A fatal bound or an operator stop ends the wait too, and a window asking
    for something nobody is waiting for is worse than none."""
    case = FakeTestCase(tmp_path / "mytest-ack-test-prompt")
    window = FakeWindow()
    monkeypatch.setattr("testcases.ydrive.teststeps.teststeps.spawn_operator_prompt",
                        lambda test_id, message, fields=(), choices=None: window)

    # Not on the first call: @step checks at its own entry, before the window is
    # spawned, and an abort there has no window to close. The case worth pinning
    # is an abort while the wait is already up.
    calls = []

    def boom():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("fatal bound")

    case.check_should_continue = boom
    with pytest.raises(RuntimeError, match="fatal bound"):
        await_operator(case, "move the load")

    assert window.terminated


def test_a_stand_with_no_display_still_runs(monkeypatch):
    """The window is a convenience over the marker file. tkinter missing, or no
    display, must not fail a run that is still answerable from a terminal."""
    monkeypatch.setitem(__import__("sys").modules, "tkinter", None)

    assert operator_prompt.show("run-1", "do the thing") == 2


def test_the_window_is_launched_without_a_console_on_windows(monkeypatch):
    """python.exe opens a console window behind a GUI on Windows - an empty black
    box on the stand's screen for as long as the prompt is up. pythonw.exe is the
    same interpreter without one."""
    from testcases import utils

    # A Windows path is not parseable by pathlib on a POSIX machine, so the
    # interpreter path here is a plain one - what is being checked is that the
    # sibling GUI executable is chosen, and that it falls back when absent.
    monkeypatch.setattr(utils.sys, "platform", "win32")
    monkeypatch.setattr(utils.sys, "executable", "/opt/python314/python.exe")
    monkeypatch.setattr(utils.Path, "exists", lambda self: True)
    assert utils._windowless_python() == "/opt/python314/pythonw.exe"

    monkeypatch.setattr(utils.Path, "exists", lambda self: False)
    assert utils._windowless_python() == "/opt/python314/python.exe", "must fall back"


def test_every_other_platform_uses_this_interpreter(monkeypatch):
    from testcases import utils

    monkeypatch.setattr(utils.sys, "platform", "darwin")
    assert utils._windowless_python() == utils.sys.executable


# --- the details that identify a run ------------------------------------------


FIELDS = (
    RunDetail("DUT SN", "dut_serial_number", ("YD-014", "YD-015")),
    RunDetail("Load (lb)", "load_lb"),
)


def _answer_with(case, answers):
    """Stand in for an operator filling the window in, or the CLI's --answer."""
    import json

    def check():
        case.checks += 1
        if case.checks == 2:
            case._ack_path.write_text(json.dumps(answers) if answers is not None else "")

    case.check_should_continue = check


def test_the_answers_are_published_as_run_state(tmp_path, monkeypatch):
    """Published, so the engine merges them into every recorded row - a stored run
    then says which DUT it was and under what load without a separate note."""
    from testcases.ydrive.teststeps.teststeps import RunDetail, await_operator_details

    case = FakeTestCase(tmp_path / "mytest-ack-test-prompt")
    monkeypatch.setattr("testcases.ydrive.teststeps.teststeps.spawn_operator_prompt",
                        lambda test_id, message, fields=(), choices=None: FakeWindow())
    monkeypatch.setattr("testcases.ydrive.teststeps.teststeps.OPERATOR_POLL_INTERVAL_S", 0.001)
    _answer_with(case, {"DUT SN": "YD-014", "Load (lb)": "250"})

    details = await_operator_details(case, FIELDS)

    assert details == {"dut_serial_number": "YD-014", "load_lb": "250"}
    assert case.state["dut_serial_number"] == "YD-014"
    assert case.state["load_lb"] == "250"
    assert case.state["operator_prompt"] is None, "the prompt outlived the answer"


def test_the_prompt_labels_are_what_the_window_is_asked_for(tmp_path, monkeypatch):
    """The label a person reads and the channel it lands in are written as a pair,
    so renaming a prompt cannot rename a channel stored runs are keyed by."""
    from testcases.ydrive.teststeps.teststeps import RunDetail, await_operator_details

    case = FakeTestCase(tmp_path / "mytest-ack-test-prompt")
    asked = []
    monkeypatch.setattr("testcases.ydrive.teststeps.teststeps.spawn_operator_prompt",
                        lambda test_id, message, fields=(), choices=None: asked.extend(fields) or FakeWindow())
    monkeypatch.setattr("testcases.ydrive.teststeps.teststeps.OPERATOR_POLL_INTERVAL_S", 0.001)
    _answer_with(case, {"DUT SN": "YD-014", "Load (lb)": "250"})

    await_operator_details(case, FIELDS)

    assert asked == ["DUT SN", "Load (lb)"]


def test_a_run_without_its_details_does_not_start(tmp_path, monkeypatch):
    """An operator can dismiss the window with the CLI acknowledgement, which
    answers nothing - and a run that cannot be attributed to a DUT is not worth the
    hours it takes."""
    from testcases.ydrive.teststeps.teststeps import RunDetail, await_operator_details

    case = FakeTestCase(tmp_path / "mytest-ack-test-prompt")
    monkeypatch.setattr("testcases.ydrive.teststeps.teststeps.spawn_operator_prompt",
                        lambda test_id, message, fields=(), choices=None: FakeWindow())
    monkeypatch.setattr("testcases.ydrive.teststeps.teststeps.OPERATOR_POLL_INTERVAL_S", 0.001)
    _answer_with(case, None)  # a plain acknowledgement, no values

    with pytest.raises(RuntimeError, match="no answer for 'DUT SN'"):
        await_operator_details(case, FIELDS)


def test_the_serial_is_picked_from_a_list_and_a_typo_is_refused(tmp_path, monkeypatch):
    """The window cannot produce anything but a listed value, but the CLI
    acknowledgement can - and a serial the record cannot match to a DUT is worse
    than no serial."""
    from testcases.ydrive.teststeps.teststeps import await_operator_details

    case = FakeTestCase(tmp_path / "mytest-ack-test-prompt")
    offered = {}
    monkeypatch.setattr(
        "testcases.ydrive.teststeps.teststeps.spawn_operator_prompt",
        lambda test_id, message, fields=(), choices=None: offered.update(choices or {}) or FakeWindow(),
    )
    monkeypatch.setattr("testcases.ydrive.teststeps.teststeps.OPERATOR_POLL_INTERVAL_S", 0.001)
    _answer_with(case, {"DUT SN": "YD-O14", "Load (lb)": "250"})  # letter O, not zero

    with pytest.raises(RuntimeError, match="is not one of the values"):
        await_operator_details(case, FIELDS)

    assert offered == {"DUT SN": ("YD-014", "YD-015")}, "the dropdown was not offered its values"


def test_the_stands_serials_are_the_ones_offered():
    from testcases.ydrive.testcases.testcases import BrakeEnduranceTest

    serial = BrakeEnduranceTest.RUN_DETAIL_FIELDS[0]
    assert serial.channel == "dut_serial_number"
    assert serial.choices == BrakeEnduranceTest.DUT_SERIAL_NUMBERS
    assert "YDRIVE1" in serial.choices
    others = [f for f in BrakeEnduranceTest.RUN_DETAIL_FIELDS if f is not serial]
    assert all(f.choices == () for f in others), "the ticket and the load are free text"


def test_the_details_reach_the_verdict_as_well_as_the_channels():
    """The channels carry them per frame, which is right for reading one run back
    and useless for finding every run against a ticket."""
    from testcases.ydrive.testcases.testcases import BrakeEnduranceTest

    test = BrakeEnduranceTest(require_engine=False)
    test.run_details = {"dut_serial_number": "YD-014", "er_ticket": "ER-2291", "load_lb": "250"}

    metadata = test.result_metadata()

    assert metadata["dut_serial_number"] == "YD-014"
    assert metadata["er_ticket"] == "ER-2291"
    assert metadata["load_lb"] == "250"


def test_every_asked_field_is_seeded_so_the_engine_keeps_it():
    """The engine fixes a file's header from the first frame and drops channels that
    appear later - an unseeded channel would be answered and then lost."""
    from testcases.ydrive.channels import DEFAULT_STATE
    from testcases.ydrive.testcases.testcases import BrakeEnduranceTest

    for field in BrakeEnduranceTest.RUN_DETAIL_FIELDS:
        assert field.channel in DEFAULT_STATE, f"{field.channel} is not seeded"
