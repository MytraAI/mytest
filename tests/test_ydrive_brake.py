"""The ydrive stand's rails, and the brake sequencing around a cycle's dwell.

The brake is magnet-applied: engaged whenever its rail is unpowered, released by
powering it. Every ordering here inverts a safety behaviour if it is wrong. These run against fakes rather
than the stand: no subprocess, no instrument.
"""
from __future__ import annotations

import inspect

import pytest

from testbeds.ydrive_testbed.ydrive_testbed import BRAKE_SETTLE_S
from testcases.ydrive.teststeps.teststeps import (
    DEFAULT_ARM_TIMEOUT_S,
    cycle_position,
    engage_brake,
    release_brake,
)


# --- the brake around a dwell ------------------------------------------------


class FakeSupplyTestbed:
    """Records the order of rail and axis-state calls, and models arming well
    enough that the brake helpers' confirmation waits can complete.

    `arms` False makes every arming request silently fail, which is what a
    latched ODrive error does - the case release_brake() must not release into."""

    def __init__(self, arms: bool = True, idles: bool = True):
        self.calls = []
        self.command = self
        self.position = 0.0
        self.armed = arms  # a stand that cannot arm is not armed to begin with
        self._arms = arms
        self._idles = idles

    # ODrive side
    def set_position(self, target):
        self.calls.append(f"move:{target}")
        self.position = target

    def set_axis_state(self, state):
        self.calls.append(f"axis:{state}")
        if state == "CLOSED_LOOP_CONTROL" and self._arms:
            self.armed = True
        elif state == "IDLE" and self._idles:
            self.armed = False

    def get_channels(self):
        return {
            "axis_is_armed": self.armed,
            "axis_current_state": 8 if self.armed else 1,
            "active_errors": 0,
            "axis_procedure_result": 0,
            "pos_estimate": self.position,
            "vel_estimate": 0.0,
        }

    def get_axis_armed_status(self):
        return bool(self.get_channels()["axis_is_armed"])

    # supply side
    def power_brake_bus(self, enabled):
        self.calls.append("brake:release" if enabled else "brake:engage")


class FakeTestCase:
    """The surface @step and the steps require of a TestCase."""

    test_id = "test-brake"

    def __init__(self, testbed):
        self.testbed = testbed
        self.state = {}

    def set_state(self, name, value):
        self.state[name] = value

    def wait_for(self, seconds):
        self.testbed.calls.append(f"wait:{seconds}")

    def check_should_continue(self):
        pass


DWELL_S = 2.0
DWELL = f"wait:{DWELL_S}"
SETTLE = f"wait:{BRAKE_SETTLE_S}"


def _run_cycle(**kwargs):
    testbed = FakeSupplyTestbed()
    case = FakeTestCase(testbed)
    cycle_position(case, low_position=0.0, high_position=10.0, dwell_s=DWELL_S, **kwargs)
    return testbed.calls


def test_the_brake_holds_each_dwell_and_is_released_before_the_next_move():
    calls = _run_cycle()
    assert calls == [
        "move:10.0",
        "brake:engage",              # brake grabs before the controller lets go
        SETTLE,                      # polled, not a blind sleep - see wait_for()
        "axis:IDLE",                 # nothing drives against an engaged brake
        DWELL,                       # held by the brake, axis idle throughout
        "axis:CLOSED_LOOP_CONTROL",  # controller takes hold before the brake lets go
        "brake:release",
        SETTLE,                      # the brake needs time to let go before a move
        "move:0.0",
        "brake:engage", SETTLE, "axis:IDLE", DWELL,
        "axis:CLOSED_LOOP_CONTROL", "brake:release", SETTLE,
    ]


def test_the_brake_is_not_released_if_the_axis_never_arms():
    """Requesting CLOSED_LOOP_CONTROL only writes requested_state; the ODrive can
    decline it, and a latched error is enough. Releasing on the strength of having
    asked would drop the load onto a controller that never took it."""
    testbed = FakeSupplyTestbed(arms=False)
    case = FakeTestCase(testbed)
    with pytest.raises(RuntimeError, match="did not arm"):
        release_brake(case, arm_timeout_s=0.05)
    assert "brake:release" not in testbed.calls, "the brake was released without the axis armed"


def test_a_failure_to_arm_reports_the_axis_state_and_decoded_errors():
    """So the log says why it would not arm, not just that it did not."""
    testbed = FakeSupplyTestbed(arms=False)
    case = FakeTestCase(testbed)
    with pytest.raises(RuntimeError) as excinfo:
        release_brake(case, arm_timeout_s=0.05)
    message = str(excinfo.value)
    for expected in ("axis_current_state", "IDLE", "active_errors", "none", "SUCCESS"):
        assert expected in message, f"{expected!r} missing from: {message}"


def test_engaging_raises_if_the_axis_will_not_idle():
    """The controller would be left driving against an engaged brake. The brake is
    holding by then, so raising is the safe outcome."""
    testbed = FakeSupplyTestbed(idles=False)
    case = FakeTestCase(testbed)
    with pytest.raises(RuntimeError, match="did not idle"):
        engage_brake(case, arm_timeout_s=0.05)
    assert "brake:engage" in testbed.calls, "the brake must still be engaged when this raises"


def test_engaging_brakes_first_then_idles():
    """Idling first would leave the load held by nothing for the brake's settle
    time."""
    testbed = FakeSupplyTestbed()
    case = FakeTestCase(testbed)
    engage_brake(case)
    assert testbed.calls == [
        "brake:engage", SETTLE, "axis:IDLE",
    ]
    assert testbed.armed is False
    assert case.state["brake_engaged"] is True


def test_releasing_arms_first_then_releases():
    """The controller takes hold before the brake lets go."""
    testbed = FakeSupplyTestbed()
    case = FakeTestCase(testbed)
    testbed.armed = False
    release_brake(case)
    assert testbed.calls == [
        "axis:CLOSED_LOOP_CONTROL", "brake:release", SETTLE,
    ]
    assert testbed.armed is True
    assert case.state["brake_engaged"] is False


def test_the_brake_helpers_are_not_steps():
    """A @step publishes itself as current_step; these are sub-actions of one, and
    would bury whichever step actually called them."""
    for helper in (engage_brake, release_brake):
        assert not hasattr(helper, "__wrapped__"), f"{helper.__name__} is decorated with @step"


def test_an_aborted_dwell_leaves_the_load_braked_and_the_axis_idle():
    """wait_for() raises on a fatal bound, a stop request or a lost recorder. On
    any of those the load stays where engage_brake() put it - held by the brake,
    axis idle - rather than being handed back to a controller at the one moment
    the reason for stopping is unknown. Teardown commands no motion, so nothing
    downstream needs it released."""
    testbed = FakeSupplyTestbed()
    case = FakeTestCase(testbed)

    def boom(seconds):
        testbed.calls.append(f"wait:{seconds}")
        if seconds == DWELL_S:
            raise RuntimeError("fatal bound")

    case.wait_for = boom
    with pytest.raises(RuntimeError, match="fatal bound"):
        cycle_position(case, low_position=0.0, high_position=10.0, dwell_s=DWELL_S)

    assert [c for c in testbed.calls if c.startswith("brake:")] == ["brake:engage"], (
        "the brake must stay engaged when the dwell aborts"
    )
    assert testbed.armed is False, "the axis must be left idle, not re-armed"
    assert case.state["brake_engaged"] is True


def test_brake_during_dwell_can_be_turned_off():
    """For a stand whose brake isn't wired to the supply, or to compare runs
    with and without it."""
    calls = _run_cycle(brake_during_dwell=False)
    assert not any(c.startswith("brake:") for c in calls)
    assert not any(c.startswith("axis:") for c in calls), "the axis state must be left alone too"
    assert [c for c in calls if not c.startswith("state:")] == [
        "move:10.0", DWELL, "move:0.0", DWELL,
    ]


# --- teardown commands no motion at all ---------------------------------------


class FakeRunner:
    """Records its stop into the same sequence as the testbed's, so the order the
    two are torn down in is visible."""

    def __init__(self, calls):
        self._calls = calls

    def stop(self):
        self._calls.append("runner:stop")


class FakeTeardownCase(FakeTestCase):
    """Enough of TestCase for BaseYdriveTest.post_test_teardown to run: a
    teardown_step that mirrors the real one's log-and-continue behaviour, and a
    record of which steps were taken."""

    def __init__(self, testbed, runner=None):
        super().__init__(testbed)
        self.runner = runner
        self.steps = []

    def teardown_step(self, label, action):
        self.steps.append(label)
        try:
            action()
        except Exception:  # the real one logs and continues
            pass


def _run_teardown(runner=True):
    from testcases.ydrive.testcases.base_ydrive_test import BaseYdriveTest

    testbed = FakeSupplyTestbed()
    testbed.stop = lambda: testbed.calls.append("testbed:stop")
    case = FakeTeardownCase(testbed, FakeRunner(testbed.calls) if runner else None)
    BaseYdriveTest.post_test_teardown(case)
    return case, testbed


def test_teardown_stops_the_runner_before_the_testbed():
    """The runner's thread relies on telemetry still flowing to notice the stop
    signal promptly, and stopping the testbed kills the driver producing it. Order
    is the whole content of this teardown, so it is what gets asserted."""
    case, testbed = _run_teardown()
    assert case.steps == ["stop rulebook runner", "stop testbed"]
    assert testbed.calls == ["runner:stop", "testbed:stop"]


def test_teardown_commands_no_motion():
    """Teardown runs after a fatal violation, an arrival timeout, or a failure
    part-way through a brake transition - the states where the reason for stopping
    is least known. Driving the real teardown against a fake is what shows it
    commands nothing, rather than grepping for names a future move might not
    use."""
    _, testbed = _run_teardown()
    assert not any(c.startswith("move:") or c.startswith("axis:") for c in testbed.calls), (
        f"teardown commanded the axis: {testbed.calls}"
    )
    assert not any(c.startswith("brake:") for c in testbed.calls), (
        "teardown must leave the brake to the testbed's own stop()"
    )


def test_teardown_tolerates_a_run_that_never_built_a_runner():
    """pre_test_setup constructs the runner after starting the testbed, so a setup
    that failed in between leaves runner None - and teardown still has to run."""
    case, testbed = _run_teardown(runner=False)
    assert case.steps == ["stop testbed"]
    assert testbed.calls == ["testbed:stop"]


def test_nothing_is_energized_unless_a_test_asks():
    """The default has to fail safe. Defaulting to True would mean every new ydrive
    test brought up a 48 V rail unless its author knew to opt out - and would make
    ManualTest, which hands the stand to an operator, energize it by inheritance."""
    from testcases.ydrive.testcases.base_ydrive_test import BaseYdriveTest
    from testcases.ydrive.testcases.testcases import EnduranceCycleTest, ManualTest

    assert BaseYdriveTest.POWER_MOTOR_BUS_AT_SETUP is False, "the default must be off"
    assert EnduranceCycleTest.POWER_MOTOR_BUS_AT_SETUP is True, "a test that drives needs the bus"
    assert ManualTest.POWER_MOTOR_BUS_AT_SETUP is False


def test_the_arm_timeout_stays_below_the_telemetry_staleness_deadline():
    """The arming wait polls through telemetry, so at or above the client's own
    staleness deadline a silent stream raises TelemetryTimeout first and the
    arming timeout - the one that names the axis state and decoded errors - never
    fires."""
    from hardware.clients.telemetry_client import TelemetryClient
    staleness_s = inspect.signature(TelemetryClient.__init__).parameters["timeout_s"].default
    assert DEFAULT_ARM_TIMEOUT_S < staleness_s, (
        f"arm timeout {DEFAULT_ARM_TIMEOUT_S}s must be under the {staleness_s}s staleness deadline"
    )
