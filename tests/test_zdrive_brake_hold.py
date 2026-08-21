"""zdrive's brake-hold sequence, and the orderings a gravity-loaded axis depends
on.

None of this talks to hardware. What it checks is the logic that decides whether
the load is ever held by nothing, whether the measurement reaches the recorded
file, and whether the steps that must not move the load do not - all of which are
ordinary code, and all of which fail in ways a live run makes look mechanical.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest


def _code_of(target) -> str:
    """The source of `target` with docstrings stripped, so an assertion about what
    the code does is not satisfied - or defeated - by prose describing it.

    Dedented with textwrap, not inspect.cleandoc: cleandoc unindents the first
    line and the rest independently, which parses for a plain function but leaves
    a method's body dangling under its own `def`."""
    source = textwrap.dedent(inspect.getsource(target))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)
    return ast.unparse(tree)

from testcases.zdrive.channels import DEFAULT_STATE
from testcases.zdrive.rulebooks.zdrive_rulebook import (
    BRAKE_HOLD_TEST_NAME,
    TEST_NAMES,
    ZDRIVE_RULEBOOK,
)
from testcases.zdrive.testcases.testcases import BrakeHoldTest
from testcases.zdrive.teststeps import teststeps


# --- the stroke -------------------------------------------------------------


def test_up_is_negative_and_the_bottom_is_the_origin():
    """Every target in the sequence is `origin + one of these`, so a sign error
    here drives the load into the stop instead of up the stroke."""
    assert teststeps.TOP_OF_STROKE == -55.0
    assert teststeps.BOTTOM_OF_STROKE == 0.0
    assert teststeps.TOP_OF_STROKE < teststeps.BOTTOM_OF_STROKE


def test_the_hold_position_sits_inside_the_stroke():
    """The hold position is this test's choice, not the stand's limit - but a sign
    error or a stray zero would drive a loaded axis into the mechanical top, so it
    has to stay between the bottom and TOP_OF_STROKE."""
    assert teststeps.TOP_OF_STROKE < BrakeHoldTest.TOP_POSITION < BrakeHoldTest.BOTTOM_POSITION
    assert BrakeHoldTest.BOTTOM_POSITION == teststeps.BOTTOM_OF_STROKE
    assert BrakeHoldTest.HOLD_S == 5.0


def test_the_move_timeout_covers_the_hold_position_at_the_velocity_limit():
    """The move that matters is the one this test actually commands. 20 turns at
    10 turns/s is 2 s, and a loaded axis climbs slower than an empty one."""
    lift_s = abs(BrakeHoldTest.TOP_POSITION) / teststeps.VELOCITY_LIMIT
    assert teststeps.DEFAULT_ARRIVAL_TIMEOUT_S > lift_s * 3


def test_the_move_timeout_covers_the_stroke_at_the_configured_velocity_limit():
    """55 turns at 10 turns/s is 5.5 s. A timeout under that would abort a
    healthy move; far over it and a stalled axis hangs the run."""
    stroke_s = abs(teststeps.TOP_OF_STROKE) / teststeps.VELOCITY_LIMIT
    assert teststeps.DEFAULT_ARRIVAL_TIMEOUT_S > stroke_s * 2
    assert teststeps.DEFAULT_ARRIVAL_TIMEOUT_S < stroke_s * 10


# --- the streams evaluation runs against ------------------------------------


def test_every_test_starts_the_runner_against_all_three_streams():
    """A bound whose channel is absent from a frame returns no result, so a runner
    given fewer streams evaluates part of the rulebook and reports a clean pass
    for the rest. There is nothing in a passing run that reveals it."""
    from testcases.zdrive.testcases.testcases import ManualTest
    for case in (BrakeHoldTest, ManualTest):
        source = _code_of(case.main_execution)
        assert "runner.start" in source, f"{case.__name__} never starts evaluation"
        for stream in ("testbed.telemetry", "testbed.bus_telemetry", "testbed.tc_daq_telemetry"):
            assert stream in source, f"{case.__name__} does not evaluate {stream}"


# --- identifying the run ----------------------------------------------------


def _answering(tmp_path, monkeypatch, answers):
    """A fake test case whose operator answers `answers` (a dict of label ->
    value) the moment the prompt opens."""
    import json as _json

    ack = tmp_path / "mytest-ack-fake-run"
    test_case = _FakeTestCase(ack)
    captured = {}

    def fake_spawn(test_id, message, fields=(), choices=None):
        captured["fields"] = list(fields)
        captured["choices"] = dict(choices or {})
        ack.write_text(_json.dumps(answers) if answers is not None else "")
        return None

    monkeypatch.setattr(teststeps, "spawn_operator_prompt", fake_spawn)
    return test_case, captured


def test_the_run_details_are_asked_for_and_published(tmp_path, monkeypatch):
    """The answers have to land on state channels, not just in a local: the engine
    merges run state into every recorded row, which is what makes a stored run
    attributable to a DUT without a separate note."""
    fields = BrakeHoldTest.RUN_DETAIL_FIELDS
    answers = {"DUT SN": "ZDRIVE2IN", "ER Ticket": "ER-4021", "Load (lb)": "20"}
    test_case, captured = _answering(tmp_path, monkeypatch, answers)

    details = teststeps.prompt_for_SN_ER_load(test_case, fields)

    assert details == {"dut_serial_number": "ZDRIVE2IN", "er_ticket": "ER-4021", "load_lb": "20"}
    assert test_case.state["dut_serial_number"] == "ZDRIVE2IN"
    assert test_case.state["er_ticket"] == "ER-4021"
    assert test_case.state["load_lb"] == "20"
    assert captured["fields"] == ["DUT SN", "ER Ticket", "Load (lb)"]


def test_a_missing_answer_is_refused(tmp_path, monkeypatch):
    """A run that cannot be attributed to a DUT is not worth the hours it takes,
    so an unanswered field fails the run rather than storing a blank."""
    test_case, _ = _answering(
        tmp_path, monkeypatch, {"DUT SN": "ZDRIVE2IN", "ER Ticket": "", "Load (lb)": "20"}
    )
    with pytest.raises(RuntimeError, match="no answer for 'ER Ticket'"):
        teststeps.prompt_for_SN_ER_load(test_case, BrakeHoldTest.RUN_DETAIL_FIELDS)


def test_a_plain_acknowledgement_is_refused_when_values_were_asked_for(tmp_path, monkeypatch):
    """`operator_ack` with no --answer leaves an empty marker. That is a valid
    plain acknowledgement elsewhere, and here it means nobody answered."""
    test_case, _ = _answering(tmp_path, monkeypatch, None)
    with pytest.raises(RuntimeError, match="no answer for"):
        teststeps.prompt_for_SN_ER_load(test_case, BrakeHoldTest.RUN_DETAIL_FIELDS)


def test_a_serial_outside_its_choices_is_refused(tmp_path, monkeypatch):
    """The window cannot produce a value outside a dropdown, but
    `operator_ack --answer` can. A serial the record cannot match to a DUT is
    worse than no serial."""
    fields = (teststeps.RunDetail("DUT SN", "dut_serial_number", ("ZDRIVE1", "ZDRIVE2")),)
    test_case, captured = _answering(tmp_path, monkeypatch, {"DUT SN": "ZDRIVE9"})
    with pytest.raises(RuntimeError, match="not one of the values"):
        teststeps.prompt_for_SN_ER_load(test_case, fields)
    assert captured["choices"] == {"DUT SN": ("ZDRIVE1", "ZDRIVE2")}


def test_the_serial_is_a_dropdown_of_this_stand_s_one_dut():
    """Free text would let a typo into the one field a stored run is matched to a
    DUT by. This stand has a single DUT, so the prompt offers exactly it."""
    serial = next(f for f in BrakeHoldTest.RUN_DETAIL_FIELDS if f.channel == "dut_serial_number")
    assert serial.choices == ("ZDRIVE2IN",)
    assert BrakeHoldTest.DUT_SERIAL_NUMBERS == ("ZDRIVE2IN",)

    # The ticket and the load are answers nobody can enumerate in advance.
    for field in BrakeHoldTest.RUN_DETAIL_FIELDS:
        if field.channel != "dut_serial_number":
            assert field.choices == ()


def test_every_run_detail_channel_is_seeded():
    """These are published by channel name through a loop, so the source scan in
    test_every_published_state_channel_is_seeded cannot see them. Unseeded, the
    engine fixes the header before they exist and the run is unattributable."""
    for field in BrakeHoldTest.RUN_DETAIL_FIELDS:
        assert field.channel in DEFAULT_STATE, f"{field.channel} is published but never seeded"


def test_the_run_details_reach_the_verdict():
    """The state channels carry them per frame, which is right for reading one run
    back and wrong for finding every run against a ticket."""
    test = BrakeHoldTest(test_id="x", require_engine=False)
    test.run_details = {"dut_serial_number": "ZDRIVE2IN", "er_ticket": "ER-1"}
    metadata = test.result_metadata()
    assert metadata["dut_serial_number"] == "ZDRIVE2IN"
    assert metadata["er_ticket"] == "ER-1"
    assert metadata["hold_s"] == BrakeHoldTest.HOLD_S


def test_the_load_is_held_at_the_top_until_the_operator_acknowledges():
    """The pause sits between arriving at the top and handing the load to the
    brake, so what holds it while a person looks at the rig is the controller."""
    source = _code_of(BrakeHoldTest.main_execution)
    lift = source.index("origin + self.TOP_POSITION")
    prompt = source.index("await_operator", lift)
    brake_hold = source.index("hold_on_brake")
    assert lift < prompt < brake_hold


def test_the_brake_only_hold_stays_a_fixed_dwell():
    """The operator pause is under the controller. The brake-only dwell is the
    measurement, and stays HOLD_S so brake_slip_turns is comparable between
    runs."""
    source = _code_of(BrakeHoldTest.main_execution)
    assert "hold_on_brake(self, self.HOLD_S)" in source


def test_the_details_are_asked_before_anything_is_energized_or_released():
    """It needs a person and not the stand. Asked after prepare_for_operation the
    bus would be live while somebody types; asked after the release, the load
    would be held by nothing while they did."""
    source = _code_of(BrakeHoldTest.main_execution)
    assert source.index("prompt_for_SN_ER_load") < source.index("prepare_for_operation")
    assert source.index("prompt_for_SN_ER_load") < source.index("release_brake_for_positioning")


# --- waiting for a person ---------------------------------------------------


class _FakeTestCase:
    """The parts of a TestCase await_operator() touches, and nothing else."""

    def __init__(self, ack_path):
        self.test_id = "fake-run"
        self.state = {}
        self.continues = 0
        self._ack_path = ack_path

    def operator_ack_path(self):
        return self._ack_path

    def set_state(self, name, value):
        self.state[name] = value

    def check_should_continue(self):
        self.continues += 1

    def wait_for(self, duration_s):
        pass


def test_awaiting_the_operator_returns_when_the_marker_appears(tmp_path, monkeypatch):
    """Actually runs the step. The wait is a marker file and a poll loop, and a
    call that does not match tools/operator_prompt's signature - or an ack
    convention that does not match tools/operator_ack's - fails only here or on
    live hardware, with a released brake and an unheld load."""
    ack = tmp_path / "mytest-ack-fake-run"
    test_case = _FakeTestCase(ack)
    spawned = {}

    class _FakeWindow:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

    window = _FakeWindow()

    def fake_spawn(test_id, message, *args, **kwargs):
        spawned["test_id"] = test_id
        spawned["message"] = message
        ack.write_text("")  # the operator clicks the moment the window is up
        return window

    monkeypatch.setattr(teststeps, "spawn_operator_prompt", fake_spawn)
    teststeps.await_operator(test_case, "do the thing")

    assert spawned == {"test_id": "fake-run", "message": "do the thing"}
    assert window.terminated
    assert not ack.exists()  # consumed, so the next prompt is not skipped
    assert test_case.state["operator_prompt"] is None
    assert test_case.continues >= 1


def test_awaiting_the_operator_ignores_a_stale_ack_from_an_earlier_run(tmp_path, monkeypatch):
    """A marker left by a previous run would otherwise satisfy this one
    instantly - skipping the hand-positioning the origin depends on."""
    ack = tmp_path / "mytest-ack-fake-run"
    ack.write_text("")
    test_case = _FakeTestCase(ack)
    seen = {}

    def fake_spawn(test_id, message, *args, **kwargs):
        seen["stale_cleared"] = not ack.exists()
        ack.write_text("")
        return None

    monkeypatch.setattr(teststeps, "spawn_operator_prompt", fake_spawn)
    teststeps.await_operator(test_case, "do the thing")

    assert seen["stale_cleared"]


def test_awaiting_the_operator_publishes_the_prompt_while_it_waits(monkeypatch, tmp_path):
    """A run that stops here has to show what it was waiting for, or it reads as
    a hang in the recorded file."""
    ack = tmp_path / "mytest-ack-fake-run"
    test_case = _FakeTestCase(ack)
    published = []

    def fake_spawn(test_id, message, *args, **kwargs):
        published.append(test_case.state.get("operator_prompt"))
        ack.write_text("")
        return None

    monkeypatch.setattr(teststeps, "spawn_operator_prompt", fake_spawn)
    teststeps.await_operator(test_case, "move the drive")

    assert published == ["move the drive"]


def test_awaiting_the_operator_clears_the_prompt_when_the_run_is_ending(tmp_path, monkeypatch):
    """A fatal bound raising out of the poll loop must still clear the prompt and
    close the window - the run is over and nobody is being asked anything."""
    ack = tmp_path / "mytest-ack-fake-run"
    test_case = _FakeTestCase(ack)

    class _FakeWindow:
        terminated = False

        def terminate(self):
            type(self).terminated = True

    # The @step decorator checks once before the body, so the bound fires on the
    # next check - the first tick of the poll loop.
    checks = []

    def exploding_check():
        checks.append(1)
        if len(checks) > 1:
            raise RuntimeError("fatal bound")

    monkeypatch.setattr(teststeps, "spawn_operator_prompt", lambda *a, **k: _FakeWindow())
    test_case.check_should_continue = exploding_check

    with pytest.raises(RuntimeError, match="fatal bound"):
        teststeps.await_operator(test_case, "move the drive")

    assert _FakeWindow.terminated
    assert test_case.state["operator_prompt"] is None


# --- what holds the load ----------------------------------------------------


def test_engaging_the_brake_grabs_before_the_axis_idles():
    """The brake must be holding before the controller lets go. Reversed, the
    load is held by nothing, and on this axis that means descending."""
    source = inspect.getsource(teststeps.engage_brake)
    assert source.index("power_brake_bus(False)") < source.index('set_axis_state("IDLE")')


def test_releasing_the_brake_arms_and_confirms_before_powering_the_rail():
    """The controller must have taken the load before the brake lets go, and
    arming is asynchronous and can be declined - so the confirmation has to sit
    between the request and the release."""
    source = inspect.getsource(teststeps.release_brake)
    armed = source.index('set_axis_state("CLOSED_LOOP_CONTROL")')
    confirmed = source.index("_await_axis_armed")
    released = source.index("power_brake_bus(True)")
    assert armed < confirmed < released


def test_only_one_step_leaves_the_load_held_by_nothing():
    """Idling the axis while the brake is released is the one genuinely unheld
    state on this stand. It is safe only at the bottom of the stroke, so it must
    live in exactly one named place rather than being reachable by accident."""
    unheld = []
    for name in dir(teststeps):
        attribute = getattr(teststeps, name)
        if not callable(attribute) or not hasattr(attribute, "__module__"):
            continue
        if attribute.__module__ != teststeps.__name__:
            continue
        try:
            source = inspect.getsource(attribute)
        except (OSError, TypeError):
            continue
        if 'set_axis_state("IDLE")' in source and "power_brake_bus(False)" not in source:
            unheld.append(name)
    assert unheld == ["release_brake_for_positioning"], (
        f"these idle the axis without engaging the brake first: {unheld}"
    )


def test_positioning_release_still_hands_over_controller_first():
    """Even the deliberately-unheld step must not let the brake be the thing that
    releases a load the controller has not taken: arm, release, then idle."""
    source = inspect.getsource(teststeps.release_brake_for_positioning)
    assert source.index("release_brake(test_case)") < source.index('set_axis_state("IDLE")')


def test_the_positioning_release_documents_where_it_is_safe():
    """A future reader has to be able to tell that this one is different."""
    doc = teststeps.release_brake_for_positioning.__doc__
    assert "BOTTOM OF THE STROKE" in doc
    assert "NOTHING" in doc


# --- the measurement --------------------------------------------------------


def test_the_hold_measures_between_engaging_and_releasing():
    """The slip has to be sampled after the brake has taken the load and again
    after the dwell - not from before the handover, which would fold the
    controller's own settling into the number."""
    source = inspect.getsource(teststeps.hold_on_brake)
    engaged = source.index("engage_brake(test_case)")
    first_read = source.index("held_from =")
    waited = source.index("wait_for(hold_s)")
    second_read = source.index("held_to =")
    assert engaged < first_read < waited < second_read


def test_the_hold_publishes_its_slip():
    source = inspect.getsource(teststeps.hold_on_brake)
    assert 'set_state("brake_slip_turns"' in source


def test_every_published_state_channel_is_seeded():
    """The engine fixes a wide file's header from its first frames and drops a
    channel that appears later. brake_slip_turns is written tens of seconds into a
    run, so unseeded it would be absent from the recorded file while the run
    reported a clean pass - the measurement missing and nothing saying so."""
    published = set()
    for name in dir(teststeps):
        attribute = getattr(teststeps, name)
        if not callable(attribute) or getattr(attribute, "__module__", None) != teststeps.__name__:
            continue
        try:
            source = inspect.getsource(attribute)
        except (OSError, TypeError):
            continue
        for line in source.splitlines():
            if "set_state(" in line and '"' in line:
                published.add(line.split('set_state("', 1)[1].split('"', 1)[0])
    published |= {"brake_holds", "position_origin"}  # published by the test case itself
    missing = sorted(published - set(DEFAULT_STATE))
    assert not missing, f"published but never seeded, so the engine drops them: {missing}"


# --- the sequence -----------------------------------------------------------


def test_the_sequence_prompts_the_operator_only_after_releasing_for_positioning():
    """The operator cannot move the load until it is released, and must not be
    asked to before it is."""
    source = inspect.getsource(BrakeHoldTest.main_execution)
    assert source.index("release_brake_for_positioning") < source.index("await_operator")


def test_the_sequence_zeroes_after_the_operator_and_before_moving():
    source = inspect.getsource(BrakeHoldTest.main_execution)
    assert source.index("await_operator") < source.index("get_pos_estimate")
    assert source.index("get_pos_estimate") < source.index("move_to")


def test_the_sequence_takes_the_load_back_in_place_before_every_move():
    """Both handovers back to the controller follow something that may have left
    the axis away from the setpoint - the operator's hand-positioning, and then a
    brake that may have slipped. A plain release_brake would lunge for the stale
    setpoint."""
    source = inspect.getsource(BrakeHoldTest.main_execution)
    assert source.count("release_brake_in_place") == 2
    for move in ("origin + self.TOP_POSITION", "origin + self.BOTTOM_POSITION"):
        assert source.index("release_brake_in_place") < source.index(move)


def test_the_sequence_holds_at_the_top_and_returns_to_the_bottom():
    source = inspect.getsource(BrakeHoldTest.main_execution)
    top = source.index("origin + self.TOP_POSITION")
    held = source.index("hold_on_brake")
    bottom = source.index("origin + self.BOTTOM_POSITION")
    assert top < held < bottom


def test_evaluation_starts_against_both_streams():
    """The rulebook spans two devices; one stream evaluates half of it and reports
    a clean pass for the rest."""
    source = inspect.getsource(BrakeHoldTest.main_execution)
    assert "self.testbed.telemetry" in source and "self.testbed.bus_telemetry" in source


def test_the_run_is_evaluated_against_the_rulebook():
    assert BRAKE_HOLD_TEST_NAME in TEST_NAMES
    assert BrakeHoldTest.TEST_NAME == BRAKE_HOLD_TEST_NAME
    assert ZDRIVE_RULEBOOK in BrakeHoldTest.RULEBOOKS


def test_the_run_is_attributable_in_the_verdict():
    test = BrakeHoldTest(test_id="t", require_engine=False)
    metadata = test.result_metadata()
    # What this run actually lifted to, not the stand's limit - a stored run has
    # to say how high it went, since that is now a per-test choice.
    assert metadata["top_position_turns"] == BrakeHoldTest.TOP_POSITION
    assert metadata["hold_s"] == BrakeHoldTest.HOLD_S
    assert metadata["brake_holds"] == 0


# --- tuning -----------------------------------------------------------------


def test_tuning_writes_the_controller_but_not_the_motor_current_limits():
    """ZdriveTestbed writes the motor's soft/hard current limits in start(). Two
    writers for one setting is how a stand ends up running under limits nobody
    declared."""
    source = inspect.getsource(teststeps._apply_tuning_params)
    assert "set_controller_config_vel_limit(" in source
    assert "set_motor_config_current_soft_max" not in source
    assert "set_motor_config_current_hard_max" not in source


def test_the_overspeed_tolerance_is_tighter_than_the_board_default():
    """The board ships 2.0. On a gravity-loaded axis an overspeed is the load
    running away, and there is less stroke left by the time a wider tolerance
    notices."""
    assert teststeps.VELOCITY_LIMIT_TOLERANCE == 1.5
    assert teststeps.VELOCITY_LIMIT_TOLERANCE < 2.0


def test_tuning_is_applied_in_ram_only():
    """A run must not leave the board configured differently than it found it."""
    assert "save_configuration" not in _code_of(teststeps)


@pytest.mark.parametrize("channel", [
    "controller_config_vel_limit_tolerance",
])
def test_the_new_tuning_channel_is_declared(channel):
    """A setter whose channel is not declared is refused by the driver at
    connect."""
    from hardware.odrive.odrive_channels import COMMAND_CHANNELS, TELEMETRY_CHANNELS

    assert channel in TELEMETRY_CHANNELS
    assert f"set_{channel}" in COMMAND_CHANNELS


# --- teardown: getting the load down ----------------------------------------


def test_teardown_attempts_the_descent_before_the_base_teardown_shuts_things_off():
    """The base teardown engages the brake, disarms and drops the bus. Lowering
    has to happen while the bus is still up and the axis can still be armed."""
    source = inspect.getsource(BrakeHoldTest.post_test_teardown)
    assert source.index("lower_to_bottom_for_teardown") < source.index("super().post_test_teardown()")


def test_teardown_is_skipped_until_the_origin_is_known():
    """Before the operator has positioned the load, nothing has lifted it - it is
    still on its stop, and there is no origin to compute a target from."""
    test = BrakeHoldTest(test_id="t", require_engine=False)
    assert test._origin is None

    called = []
    test.teardown_step = lambda description, action: called.append(description)
    test.runner = None
    test.testbed = None
    test.post_test_teardown()
    assert called == [], "attempted to lower the load before an origin existed"


def test_teardown_lowers_relative_to_the_operator_s_origin():
    """The device is never zeroed, so the bottom is origin + BOTTOM_POSITION and
    not absolute 0 - lowering to absolute zero would drive to an arbitrary place
    on the stroke."""
    test = BrakeHoldTest(test_id="t", require_engine=False)
    test._origin = 12.5
    targets = []
    test.teardown_step = lambda description, action: targets.append(description)
    test.runner = None
    test.testbed = None
    test.post_test_teardown()
    assert targets == ["lower the load to the bottom of the stroke"]

    source = inspect.getsource(BrakeHoldTest.post_test_teardown)
    assert "self._origin + self.BOTTOM_POSITION" in source


def test_the_descent_is_given_ten_seconds_and_then_abandoned():
    """An attempt, not a guarantee: the caller's next move is to switch
    everything off, and a stand nobody is watching is not improved by waiting."""
    assert teststeps.TEARDOWN_DESCENT_TIMEOUT_S == 10.0
    stroke_s = abs(teststeps.TOP_OF_STROKE) / teststeps.VELOCITY_LIMIT
    assert teststeps.TEARDOWN_DESCENT_TIMEOUT_S > stroke_s, (
        "a healthy full-stroke descent must fit inside the attempt"
    )


def test_the_descent_never_polls_the_abort_checks():
    """It runs after a fatal bound, so check_should_continue() would raise on its
    first tick - and a raise here leaves the load suspended, which is the outcome
    the whole path exists to avoid. It is called through teardown_step(), which
    logs rather than raises, so it needs no handling of its own."""
    code = _code_of(teststeps.lower_to_bottom_for_teardown)
    assert "check_should_continue" not in code
    assert "wait_for" not in code, "wait_for polls the abort checks"
    assert "raise" not in code


def test_the_descent_leaves_the_shutdown_to_the_base_teardown():
    """It does not re-engage the brake or drop the bus itself: ZdriveTestbed.stop()
    does both, whatever happened, and duplicating that here is what made this a
    loop with four exits instead of a command and a wait."""
    code = _code_of(teststeps.lower_to_bottom_for_teardown)
    assert "power_brake_bus(False)" not in code
    assert "power_motor_bus" not in code
    source = inspect.getsource(BrakeHoldTest.post_test_teardown)
    assert "super().post_test_teardown()" in source


def test_the_descent_confirms_the_axis_armed_before_releasing_the_brake():
    """Releasing on the strength of having asked would hand a gravity load to a
    controller that may have declined. One reading, not a loop."""
    code = _code_of(teststeps.lower_to_bottom_for_teardown)
    armed = code.index("CLOSED_LOOP_CONTROL")
    checked = code.index("get_axis_armed_status")
    released = code.index("power_brake_bus(True)")
    assert armed < checked < released


def test_the_descent_parks_the_setpoint_before_arming():
    """After a hold the axis may have crept, so arming to the stale setpoint would
    lunge for it - upward, on this stroke."""
    code = _code_of(teststeps.lower_to_bottom_for_teardown)
    parked = code.index("set_position(held_at)")
    armed = code.index("CLOSED_LOOP_CONTROL")
    commanded = code.index("set_position(target)")
    assert parked < armed < commanded


def test_the_descent_does_nothing_when_the_load_is_already_down():
    """A normal run ends at the bottom, so teardown should not re-arm and re-drive
    a load that is already resting on its stop."""
    code = _code_of(teststeps.lower_to_bottom_for_teardown)
    assert "TEARDOWN_POSITION_TOLERANCE" in code
    assert "nothing to lower" in code


def test_the_stand_shutdown_keeps_the_brake_first_ordering():
    """ZdriveTestbed.stop() is what ends every run, descent or not: the brake
    grabs, then the axis is disarmed, then the bus goes."""
    from testbeds.zdrive_testbed.zdrive_testbed import ZdriveTestbed

    source = inspect.getsource(ZdriveTestbed.stop)
    assert source.index("engage the brake") < source.index("disarm the ODrive axis")
    assert source.index("disarm the ODrive axis") < source.index("drop the 48 V motor bus")
