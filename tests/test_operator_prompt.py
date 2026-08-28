"""Asking an operator to do something, and hearing back.

The window and the CLI both write one marker file, and the waiting step polls for
it - so neither is a special case in the step, and a stand with no display is
still answerable.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from testcases.teststeps.duts import serials_for
from testcases.teststeps.operator import (
    ER_TICKET_HINT,
    ER_TICKET_PATTERN,
    RunDetail,
    await_operator,
)
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
    monkeypatch.setattr("testcases.teststeps.operator.spawn_operator_prompt",
                        lambda test_id, message, fields=(), choices=None, **kw: FakeWindow())
    monkeypatch.setattr("testcases.teststeps.operator.OPERATOR_POLL_INTERVAL_S", 0.001)

    await_operator(case, "do the thing")

    assert case.checks >= 3, "the wait returned on the stale marker instead of a fresh one"


def test_the_prompt_is_published_while_waiting_and_cleared_after(tmp_path, monkeypatch):
    """A recorded run shows how long the stand sat waiting on a person, which is
    otherwise indistinguishable from a hang."""
    case = FakeTestCase(tmp_path / "mytest-ack-test-prompt")
    windows = []
    monkeypatch.setattr("testcases.teststeps.operator.spawn_operator_prompt",
                        lambda test_id, message, fields=(), choices=None, **kw: windows.append(FakeWindow()) or windows[-1])
    monkeypatch.setattr("testcases.teststeps.operator.OPERATOR_POLL_INTERVAL_S", 0.001)

    await_operator(case, "move the load")

    assert case.state["operator_prompt"] is None, "the channel still names a finished request"
    assert windows and windows[0].terminated, "the window was left open after the wait"


def test_the_window_is_closed_even_when_the_wait_is_aborted(tmp_path, monkeypatch):
    """A fatal bound or an operator stop ends the wait too, and a window asking
    for something nobody is waiting for is worse than none."""
    case = FakeTestCase(tmp_path / "mytest-ack-test-prompt")
    window = FakeWindow()
    monkeypatch.setattr("testcases.teststeps.operator.spawn_operator_prompt",
                        lambda test_id, message, fields=(), choices=None, **kw: window)

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
    from testcases.teststeps.operator import RunDetail, prompt_for_run_details

    case = FakeTestCase(tmp_path / "mytest-ack-test-prompt")
    monkeypatch.setattr("testcases.teststeps.operator.spawn_operator_prompt",
                        lambda test_id, message, fields=(), choices=None, **kw: FakeWindow())
    monkeypatch.setattr("testcases.teststeps.operator.OPERATOR_POLL_INTERVAL_S", 0.001)
    _answer_with(case, {"DUT SN": "YD-014", "Load (lb)": "250"})

    details = prompt_for_run_details(case, FIELDS)

    assert details == {"dut_serial_number": "YD-014", "load_lb": "250"}
    assert case.state["dut_serial_number"] == "YD-014"
    assert case.state["load_lb"] == "250"
    assert case.state["operator_prompt"] is None, "the prompt outlived the answer"


def test_the_prompt_labels_are_what_the_window_is_asked_for(tmp_path, monkeypatch):
    """The label a person reads and the channel it lands in are written as a pair,
    so renaming a prompt cannot rename a channel stored runs are keyed by."""
    from testcases.teststeps.operator import RunDetail, prompt_for_run_details

    case = FakeTestCase(tmp_path / "mytest-ack-test-prompt")
    asked = []
    monkeypatch.setattr("testcases.teststeps.operator.spawn_operator_prompt",
                        lambda test_id, message, fields=(), choices=None, **kw: asked.extend(fields) or FakeWindow())
    monkeypatch.setattr("testcases.teststeps.operator.OPERATOR_POLL_INTERVAL_S", 0.001)
    _answer_with(case, {"DUT SN": "YD-014", "Load (lb)": "250"})

    prompt_for_run_details(case, FIELDS)

    assert asked == ["DUT SN", "Load (lb)"]


def test_a_run_without_its_details_does_not_start(tmp_path, monkeypatch):
    """An operator can dismiss the window with the CLI acknowledgement, which
    answers nothing - and a run that cannot be attributed to a DUT is not worth the
    hours it takes."""
    from testcases.teststeps.operator import RunDetail, prompt_for_run_details

    case = FakeTestCase(tmp_path / "mytest-ack-test-prompt")
    monkeypatch.setattr("testcases.teststeps.operator.spawn_operator_prompt",
                        lambda test_id, message, fields=(), choices=None, **kw: FakeWindow())
    monkeypatch.setattr("testcases.teststeps.operator.OPERATOR_POLL_INTERVAL_S", 0.001)
    _answer_with(case, None)  # a plain acknowledgement, no values

    with pytest.raises(RuntimeError, match="no answer for 'DUT SN'"):
        prompt_for_run_details(case, FIELDS)


def test_the_serial_is_picked_from_a_list_and_a_typo_is_refused(tmp_path, monkeypatch):
    """The window cannot produce anything but a listed value, but the CLI
    acknowledgement can - and a serial the record cannot match to a DUT is worse
    than no serial."""
    from testcases.teststeps.operator import prompt_for_run_details

    case = FakeTestCase(tmp_path / "mytest-ack-test-prompt")
    offered = {}
    monkeypatch.setattr(
        "testcases.teststeps.operator.spawn_operator_prompt",
        lambda test_id, message, fields=(), choices=None, **kw: offered.update(choices or {}) or FakeWindow(),
    )
    monkeypatch.setattr("testcases.teststeps.operator.OPERATOR_POLL_INTERVAL_S", 0.001)
    _answer_with(case, {"DUT SN": "YD-O14", "Load (lb)": "250"})  # letter O, not zero

    with pytest.raises(RuntimeError, match="is not one of the values"):
        prompt_for_run_details(case, FIELDS)

    assert offered == {"DUT SN": ("YD-014", "YD-015")}, "the dropdown was not offered its values"


def test_the_stands_serials_are_the_ones_offered():
    from testcases.ydrive.testcases.testcases import BrakeEnduranceTest

    serial = BrakeEnduranceTest.RUN_DETAIL_FIELDS[0]
    assert serial.channel == "dut_serial_number"
    assert serial.choices == serials_for(BrakeEnduranceTest.DUT)
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


# --- a ticket the results can be filed under -----------------------------------


TICKET_FIELDS = (
    RunDetail("ER Ticket", "er_ticket", pattern=ER_TICKET_PATTERN, hint=ER_TICKET_HINT),
)


def _prompt_with_answer(tmp_path, monkeypatch, answers, captured=None):
    """Run the details prompt against a canned answer, as the CLI path delivers it."""
    from testcases.teststeps.operator import prompt_for_run_details

    case = FakeTestCase(tmp_path / "mytest-ack-test-prompt")

    def spawn(test_id, message, fields=(), choices=None, **kw):
        if captured is not None:
            captured.update({
                "patterns": kw.get("patterns") or {},
                "hints": kw.get("hints") or {},
                "headline": kw.get("headline"),
                "message": message,
            })
        return FakeWindow()

    monkeypatch.setattr("testcases.teststeps.operator.spawn_operator_prompt", spawn)
    monkeypatch.setattr("testcases.teststeps.operator.OPERATOR_POLL_INTERVAL_S", 0.001)
    _answer_with(case, answers)
    return case, prompt_for_run_details


@pytest.mark.parametrize("typed", ["ER 64", "64", "er_64", "ER-", "ER-64-2", "bringup"])
def test_a_ticket_that_is_not_a_ticket_is_refused(tmp_path, monkeypatch, typed):
    """The window will not submit these, but `tools.operator_ack --answer` will.

    The ticket is a directory name wherever runs are filed by it, so free text
    becomes as many sibling pseudo-tickets as there are ways to type one."""
    case, prompt = _prompt_with_answer(tmp_path, monkeypatch, {"ER Ticket": typed})
    with pytest.raises(RuntimeError, match="is not a usable 'ER Ticket'"):
        prompt(case, TICKET_FIELDS)


@pytest.mark.parametrize("typed,stored", [
    ("ER-64", "ER-64"),
    ("er-64", "ER-64"),
    ("  ER-64  ", "ER-64"),
    ("Er-00", "ER-00"),
])
def test_the_ticket_is_stored_in_one_spelling(tmp_path, monkeypatch, typed, stored):
    """Upper-cased and stripped before it is stored.

    SMB is case-insensitive but case-preserving, so er-64 and ER-64 are one
    directory whose name depends on who typed first."""
    case, prompt = _prompt_with_answer(tmp_path, monkeypatch, {"ER Ticket": typed})
    assert prompt(case, TICKET_FIELDS) == {"er_ticket": stored}
    assert case.state["er_ticket"] == stored


def test_the_no_ticket_bucket_is_a_valid_ticket(tmp_path, monkeypatch):
    """ER-00 satisfies the pattern like any other, so an exploratory run has an
    answer that is not somebody else's ticket number."""
    case, prompt = _prompt_with_answer(tmp_path, monkeypatch, {"ER Ticket": "ER-00"})
    assert prompt(case, TICKET_FIELDS) == {"er_ticket": "ER-00"}


def test_a_ticket_number_outside_ascii_is_refused(tmp_path, monkeypatch):
    """[0-9], not \\d - which also matches digits outside ASCII, and a ticket
    number in Devanagari would become a directory nobody can type."""
    case, prompt = _prompt_with_answer(tmp_path, monkeypatch, {"ER Ticket": "ER-٦٤"})
    with pytest.raises(RuntimeError, match="is not a usable 'ER Ticket'"):
        prompt(case, TICKET_FIELDS)


def test_the_window_is_told_what_to_enforce(tmp_path, monkeypatch):
    """The pattern and its hint reach the window, so a typo is corrected in front
    of the person who made it rather than ending the run they were starting."""
    captured = {}
    case, prompt = _prompt_with_answer(
        tmp_path, monkeypatch, {"ER Ticket": "ER-64"}, captured=captured
    )
    prompt(case, TICKET_FIELDS)
    assert captured["patterns"] == {"ER Ticket": ER_TICKET_PATTERN}
    assert captured["hints"] == {"ER Ticket": ER_TICKET_HINT}


def test_an_unpatterned_field_is_stored_as_typed(tmp_path, monkeypatch):
    """Only a patterned field is upper-cased: a pattern is what says the field has
    one canonical spelling. Whitespace is stripped either way."""
    fields = (RunDetail("Note", "note"),)
    case, prompt = _prompt_with_answer(tmp_path, monkeypatch, {"Note": "  slow leg  "})
    assert prompt(case, fields) == {"note": "slow leg"}


# --- telling the operator their results are not being copied anywhere ------------


class RecordingTestCase(FakeTestCase):
    """A test case that answers every wait, and remembers what it was asked."""

    def __init__(self, ack_path, require_engine=True):
        super().__init__(ack_path)
        self.require_engine = require_engine
        self.prompts = []

    def set_state(self, name, value):
        super().set_state(name, value)
        if name == "operator_prompt" and value is not None:
            self.prompts.append(value)

    def check_should_continue(self):
        import json as _json

        self._ack_path.write_text(_json.dumps({"ER Ticket": "ER-64"}))


def _run_prompt(tmp_path, monkeypatch, status, require_engine=True, seen=None):
    from testcases.teststeps.operator import prompt_for_run_details

    case = RecordingTestCase(tmp_path / "mytest-ack-test-prompt", require_engine)
    monkeypatch.setattr("testcases.teststeps.operator.read_status", lambda: status)

    def spawn(test_id, message, fields=(), choices=None, **kw):
        if seen is not None:
            seen.append({"message": message, "headline": kw.get("headline")})
        return FakeWindow()

    monkeypatch.setattr("testcases.teststeps.operator.spawn_operator_prompt", spawn)
    monkeypatch.setattr("testcases.teststeps.operator.OPERATOR_POLL_INTERVAL_S", 0.001)
    prompt_for_run_details(case, TICKET_FIELDS)
    return case


def test_a_stand_that_is_not_mirroring_says_so_before_it_asks_anything(tmp_path, monkeypatch):
    """The one moment somebody is guaranteed to be looking. Its own dialog rather
    than a line above the fields, which is a line people stop reading by Thursday."""
    case = _run_prompt(tmp_path, monkeypatch, None)

    assert len(case.prompts) == 2, "the warning and the details should be two dialogs"
    assert "has never run on this machine" in case.prompts[0]
    assert case.prompts[1] == "enter this run's details"


def test_the_warning_does_not_stop_the_run(tmp_path, monkeypatch):
    """The record is safe locally and the mirror backfills, so refusing to start
    would spend stand time on a problem that no longer threatens the record."""
    case = _run_prompt(tmp_path, monkeypatch, None)

    assert case.state["er_ticket"] == "ER-64", "the run went ahead and was attributed"


def test_a_healthy_mirror_says_nothing(tmp_path, monkeypatch):
    import time as _time

    from protocol.mirror_status import MirrorStatus

    case = _run_prompt(tmp_path, monkeypatch, MirrorStatus(_time.time(), "//nas/x", True))

    assert case.prompts == ["enter this run's details"]


def test_a_run_that_records_nothing_is_not_warned_about_mirroring(tmp_path, monkeypatch):
    """A demo or a unit test declares require_engine=False. Nothing about those is
    being recorded, so nothing about them is being mirrored either."""
    case = _run_prompt(tmp_path, monkeypatch, None, require_engine=False)

    assert case.prompts == ["enter this run's details"]


def test_the_published_prompt_stays_one_line(tmp_path, monkeypatch):
    """operator_prompt is a telemetry column carried on every frame of the wait,
    and the dashboard shows it live. The window gets the paragraphs; the channel
    gets the headline."""
    case = _run_prompt(tmp_path, monkeypatch, None)

    assert "\n" not in case.prompts[0]
    assert case.prompts[0].startswith("The results mirror has never run")


def test_a_dut_with_no_catalogued_units_cannot_build_a_serial_prompt():
    """An empty `choices` is how a field says it is free text, so an empty
    dropdown would silently make the serial the one thing it must never be -
    unchecked."""
    from testcases.teststeps.operator import run_detail_fields

    with pytest.raises(ValueError, match="no DUT serial numbers catalogued"):
        run_detail_fields("example_dut")


# --- what the window refuses to submit --------------------------------------------


def test_the_window_refuses_a_ticket_that_does_not_match():
    """The point of checking in the window at all: the person who made the typo is
    standing in front of it, so they fix it instead of losing the run."""
    answers = {"ER Ticket": "ER 64"}

    said = operator_prompt.normalise_and_check(
        answers, {"ER Ticket": ER_TICKET_PATTERN}, {"ER Ticket": ER_TICKET_HINT}
    )

    assert said == f"ER Ticket should look like {ER_TICKET_HINT}"


def test_the_window_submits_the_canonical_spelling():
    """Upper-cased in place, so what leaves the window is what the step would have
    stored anyway - the two cannot disagree about the answer."""
    answers = {"ER Ticket": "er-64"}

    assert operator_prompt.normalise_and_check(answers, {"ER Ticket": ER_TICKET_PATTERN}) is None
    assert answers == {"ER Ticket": "ER-64"}


def test_the_window_shows_the_pattern_when_there_is_no_hint():
    said = operator_prompt.normalise_and_check({"X": "no"}, {"X": "^YES$"})
    assert said == "X should look like ^YES$"


def test_a_pattern_for_a_field_nobody_was_asked_is_ignored():
    """A typo on a command line, not a reason for the button to stop working."""
    assert operator_prompt.normalise_and_check({"X": "ok"}, {"Typo": "^never$"}) is None


def test_an_unpatterned_answer_is_left_exactly_as_typed():
    answers = {"Load (lb)": "250", "ER Ticket": "er-1"}
    operator_prompt.normalise_and_check(answers, {"ER Ticket": ER_TICKET_PATTERN})
    assert answers["Load (lb)"] == "250"


def test_the_warning_shouts_one_line_and_explains_underneath(tmp_path, monkeypatch):
    """The headline is what gets read across a workshop; the body is for whoever
    fixes it. Passed separately so the window can render them differently, rather
    than the window guessing which line matters."""
    from protocol.mirror_status import WARNING_HEADLINE

    seen = []
    _run_prompt(tmp_path, monkeypatch, None, seen=seen)

    warning, details = seen[0], seen[1]
    assert warning["headline"] == WARNING_HEADLINE
    assert "\n" not in warning["headline"], "a shouted line must not wrap on its own newlines"
    assert "never run on this machine" in warning["message"]
    assert "Setup-StandBox.ps1" in warning["message"]
    assert details["headline"] is None, "the ordinary details prompt keeps the generic title"


def test_a_healthy_mirror_shouts_nothing(tmp_path, monkeypatch):
    import time as _time

    from protocol.mirror_status import MirrorStatus

    seen = []
    _run_prompt(tmp_path, monkeypatch, MirrorStatus(_time.time(), "//nas/x", True), seen=seen)

    assert len(seen) == 1 and seen[0]["headline"] is None
