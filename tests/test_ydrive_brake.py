"""The ydrive stand's rails, and the brake sequencing around a cycle's dwell.

The brake is spring-applied - powering its rail releases it - so every ordering
here inverts a safety behaviour if it is wrong. These run against fakes rather
than the stand: no subprocess, no instrument.
"""
from __future__ import annotations

import inspect

import pytest

from hardware.cpx400dp.rails import MAX_CURRENT_A, MAX_VOLTAGE_V, POWER_ENVELOPE_W, deliverable_current_a
from protocol.wire import DEVICE_CPX400DP, DEVICE_ODRIVE, TELEMETRY_ENDPOINTS
from testbeds.ydrive_testbed.ydrive_testbed import BRAKE_BUS, MOTOR_BUS, RAILS, YdriveTestbed
from testbeds.ydrive_testbed.ydrive_testbed import BRAKE_SETTLE_S
from testcases.ydrive.teststeps.teststeps import cycle_position, engage_brake, release_brake


# --- the stand ---------------------------------------------------------------


def test_the_supply_is_one_of_this_testbed_s_devices():
    """So the engine records it into the run directory - a test's declared
    devices are validated against the endpoints the engine subscribes to."""
    assert YdriveTestbed.DEVICES == (DEVICE_ODRIVE, DEVICE_CPX400DP)
    for device in YdriveTestbed.DEVICES:
        assert device in TELEMETRY_ENDPOINTS


def test_the_two_rails_are_on_different_outputs():
    assert {rail.output for rail in RAILS} == {1, 2}
    assert BRAKE_BUS.output == 1 and MOTOR_BUS.output == 2


def test_rail_voltages_and_limits_are_inside_the_instrument_s_ratings():
    for rail in RAILS:
        assert 0 < rail.voltage_v <= MAX_VOLTAGE_V
        assert 0 < rail.current_limit_a <= MAX_CURRENT_A


def test_the_brake_rail_gets_real_current_limiting():
    assert BRAKE_BUS.is_within_envelope
    assert BRAKE_BUS.power_w == pytest.approx(120.0)


def test_the_motor_rail_s_limit_is_above_what_the_supply_can_deliver():
    """Which is why start() warns, and why a bound on this rail has to watch
    in_power_limit_2 rather than current_2. If the limit is ever lowered inside
    the envelope, this test is the place that should change with it."""
    assert not MOTOR_BUS.is_within_envelope
    assert deliverable_current_a(MOTOR_BUS.voltage_v) == pytest.approx(8.75)
    assert MOTOR_BUS.power_w > POWER_ENVELOPE_W


def test_supply_accessors_raise_before_start():
    testbed = YdriveTestbed()
    for name in ("supply", "supply_telemetry"):
        with pytest.raises(RuntimeError, match="before start"):
            getattr(testbed, name)


def test_teardown_engages_the_brake_before_disarming_and_dropping_the_bus():
    """The brake is spring-applied, so dropping its rail first is what holds the
    load before the drive is shut down. Source ordering is easy to disturb while
    editing and invisible in review."""
    source = inspect.getsource(YdriveTestbed.stop)
    brake = source.index("engage the brake")
    disarm = source.index("disarm the ODrive axis")
    bus = source.index("drop the 48 V motor bus")
    assert brake < disarm < bus


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
        self.armed = True  # cycle_position is entered with the axis armed
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

    def get_pos_estimate(self):
        return self.position

    def get_vel_estimate(self):
        return 0.0

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
        if name == "brake_engaged":
            self.testbed.calls.append(f"state:brake_engaged={value}")

    def wait_for(self, seconds):
        self.testbed.calls.append(f"wait:{seconds}")

    def check_fatal_violation(self):
        pass

    def check_stop_requested(self):
        pass

    def check_recording_alive(self):
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
    calls = [c for c in _run_cycle() if not c.startswith("state:")]
    assert calls == [
        "move:10.0", "brake:engage", SETTLE, "axis:IDLE", DWELL,
        "axis:CLOSED_LOOP_CONTROL", "brake:release", SETTLE,
        "move:0.0", "brake:engage", SETTLE, "axis:IDLE", DWELL,
        "axis:CLOSED_LOOP_CONTROL", "brake:release", SETTLE,
    ]


def test_every_settle_wait_is_polled_rather_than_blind():
    """The fix this test exists for: a plain time.sleep() around a brake
    transition would be the one wait in a cycle that ignores a fatal bound, a
    stop request and a lost recorder. Routing it through test_case.wait_for()
    means every tick checks all three. Four per cycle - two transitions, each
    side of the dwell."""
    assert [c for c in _run_cycle() if c == SETTLE].__len__() == 4


def test_the_brake_is_not_released_if_the_axis_never_arms():
    """Requesting CLOSED_LOOP_CONTROL only writes requested_state; the ODrive can
    decline it, and a latched error is enough. Releasing on the strength of having
    asked would drop the load onto a controller that never took it."""
    testbed = FakeSupplyTestbed(arms=False)
    testbed.armed = False
    case = FakeTestCase(testbed)
    with pytest.raises(RuntimeError, match="did not arm"):
        release_brake(case, arm_timeout_s=0.05)
    assert "brake:release" not in testbed.calls, "the brake was released without the axis armed"


def test_a_failure_to_arm_reports_the_axis_state_and_decoded_errors():
    """So the log says why it would not arm, not just that it did not."""
    testbed = FakeSupplyTestbed(arms=False)
    testbed.armed = False
    case = FakeTestCase(testbed)
    with pytest.raises(RuntimeError) as excinfo:
        release_brake(case, arm_timeout_s=0.05)
    message = str(excinfo.value)
    assert "axis_current_state=IDLE" in message
    assert "active_errors=0 (none)" in message
    assert "procedure_result=SUCCESS" in message


def test_engaging_raises_if_the_axis_will_not_idle():
    """The controller would be left driving against an engaged brake. The brake is
    holding by then, so raising is the safe outcome."""
    testbed = FakeSupplyTestbed(idles=False)
    case = FakeTestCase(testbed)
    with pytest.raises(RuntimeError, match="did not idle"):
        engage_brake(case, arm_timeout_s=0.05)
    assert "brake:engage" in testbed.calls, "the brake must still be engaged when this raises"


def test_the_motor_never_drives_against_the_brake():
    """The invariant these helpers exist to keep. Note it is about *driving*, not
    arming: arming while the brake still holds is required, because the
    controller has to take hold before the brake lets go. What must never happen
    is a move commanded while the brake is engaged."""
    braked = False
    for call in _run_cycle():
        if call == "brake:engage":
            braked = True
        elif call == "brake:release":
            braked = False
        elif call.startswith("move:"):
            assert not braked, f"{call} was commanded with the brake engaged"


def test_the_axis_is_idle_for_the_whole_dwell():
    """So nothing is holding position against a locked output while it waits."""
    armed = True  # cycle_position is entered with the axis armed
    for call in _run_cycle():
        if call == "axis:CLOSED_LOOP_CONTROL":
            armed = True
        elif call == "axis:IDLE":
            armed = False
        elif call == DWELL:
            assert not armed, "the dwell ran with the axis still armed"


def test_the_brake_is_never_released_while_the_axis_is_idle():
    """That would leave the load held by neither."""
    armed = True
    for call in _run_cycle():
        if call == "axis:CLOSED_LOOP_CONTROL":
            armed = True
        elif call == "axis:IDLE":
            armed = False
        elif call == "brake:release":
            assert armed, "the brake was released with the axis idle - load unheld"


def test_engaging_brakes_first_then_idles():
    """Idling first would leave the load held by nothing for the brake's settle
    time."""
    testbed = FakeSupplyTestbed()
    case = FakeTestCase(testbed)
    engage_brake(case)
    assert [c for c in testbed.calls if not c.startswith("state:")] == [
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
    assert [c for c in testbed.calls if not c.startswith("state:")] == [
        "axis:CLOSED_LOOP_CONTROL", "brake:release", SETTLE,
    ]
    assert testbed.armed is True
    assert case.state["brake_engaged"] is False


def test_the_testbeds_do_not_expose_an_unsequenced_brake_helper():
    """power_brake_bus() moves the rail alone. Naming it engage_brake() on the
    testbed too would give a step author holding `testbed` an easy way to skip
    the axis coupling entirely."""
    from testbeds.zdrive_testbed.zdrive_testbed import ZdriveTestbed

    for cls in (YdriveTestbed, ZdriveTestbed):
        assert hasattr(cls, "power_brake_bus")
        assert not hasattr(cls, "engage_brake"), f"{cls.__name__} exposes an unsequenced engage_brake"
        assert not hasattr(cls, "release_brake"), f"{cls.__name__} exposes an unsequenced release_brake"


def test_the_brake_helpers_are_not_steps():
    """A @step publishes itself as current_step; these are sub-actions, and would
    bury whichever step actually called them."""
    for helper in (engage_brake, release_brake):
        assert not hasattr(helper, "__wrapped__"), f"{helper.__name__} is decorated with @step"


def test_the_brake_is_engaged_only_after_the_axis_has_arrived():
    """move_to() blocks until arrived and settled, so engaging cannot brake a
    moving axis."""
    calls = _run_cycle()
    assert calls.index("move:10.0") < calls.index("brake:engage")


def test_the_brake_is_released_before_the_next_move_is_commanded():
    """Commanding a move into a brake that has not let go drives the axis into a
    mechanical stop."""
    calls = [c for c in _run_cycle() if not c.startswith("state:")]
    first_release = calls.index("brake:release")
    assert calls[first_release + 1] == SETTLE, "the brake needs time to let go first"
    assert calls[first_release + 2] == "move:0.0"


def test_the_dwell_happens_with_the_brake_engaged():
    calls = [c for c in _run_cycle() if not c.startswith("state:")]
    dwell = calls.index(DWELL)
    assert calls[dwell - 3:dwell] == ["brake:engage", SETTLE, "axis:IDLE"]
    assert calls[dwell + 1:dwell + 3] == ["axis:CLOSED_LOOP_CONTROL", "brake:release"]


def test_the_brake_state_channel_follows_the_brake():
    calls = _run_cycle()
    engaged = calls.index("state:brake_engaged=True")
    assert "brake:engage" in calls[:engaged]
    assert "state:brake_engaged=False" in calls


def test_the_brake_is_released_even_if_the_dwell_is_cut_short():
    """wait_for() raises on a fatal bound. Teardown moves the axis back to 0, so
    a brake left engaged would be driven against."""
    testbed = FakeSupplyTestbed()
    case = FakeTestCase(testbed)

    def boom(seconds):
        # Only the dwell itself, not the brake settle waits that now also go
        # through wait_for().
        testbed.calls.append(f"wait:{seconds}")
        if seconds == DWELL_S:
            raise RuntimeError("fatal bound")

    case.wait_for = boom
    with pytest.raises(RuntimeError, match="fatal bound"):
        cycle_position(case, low_position=0.0, high_position=10.0, dwell_s=DWELL_S)
    assert [c for c in testbed.calls if c.startswith("brake:")] == ["brake:engage", "brake:release"]
    assert testbed.armed is True, "the axis must be re-armed on the way out"
    assert case.state["brake_engaged"] is False


def test_brake_during_dwell_can_be_turned_off():
    """For a stand whose brake isn't wired to the supply, or to compare runs
    with and without it."""
    calls = _run_cycle(brake_during_dwell=False)
    assert not any(c.startswith("brake:") for c in calls)
    assert not any(c.startswith("axis:") for c in calls), "the axis state must be left alone too"
    assert [c for c in calls if not c.startswith("state:")] == [
        "move:10.0", DWELL, "move:0.0", DWELL,
    ]


# --- the power-up handover ---------------------------------------------------


def test_setup_powers_the_bus_but_leaves_the_brake_engaged():
    """Releasing at setup would leave the load held by nothing: the axis is still
    IDLE then. The handover goes controller-first."""
    from testcases.ydrive.testcases.base_ydrive_test import BaseYdriveTest

    source = inspect.getsource(BaseYdriveTest.pre_test_setup)
    assert "power_motor_bus(True)" in source
    assert "release_brake" not in source


def test_the_cycling_test_releases_the_brake_before_its_first_move():
    from testcases.ydrive.testcases.testcases import EnduranceCycleTest

    source = inspect.getsource(EnduranceCycleTest.main_execution)
    released = source.index("release_brake(self)")
    first_move = source.index("move_to(self, self.LOW_POSITION)")
    assert released < first_move, "the brake must be released before the first move"


# --- teardown must not drive into an engaged brake ---------------------------


class FakeTeardownTestbed(FakeSupplyTestbed):
    """Adds the brake-state read the teardown gate uses."""

    def __init__(self, brake_released: bool, releasable: bool = True):
        super().__init__()
        self._released = brake_released
        self._releasable = releasable

    def brake_is_released(self):
        return self._released

    def power_brake_bus(self, enabled):
        super().power_brake_bus(enabled)
        if self._releasable:
            self._released = enabled


def _teardown_with(testbed):
    """Drive EnduranceCycleTest._return_to_zero against a fake, borrowing
    FakeTestCase's TestCase surface."""
    from testcases.ydrive.testcases.testcases import EnduranceCycleTest

    case = FakeTestCase(testbed)
    case._return_to_zero = EnduranceCycleTest._return_to_zero.__get__(case, type(case))
    case._return_to_zero()
    return testbed.calls


def test_teardown_moves_to_zero_when_the_brake_is_already_released():
    calls = _teardown_with(FakeTeardownTestbed(brake_released=True))
    assert "move:0.0" in calls
    assert not any(c.startswith("brake:") for c in calls), "no need to touch a released brake"


def test_teardown_releases_an_engaged_brake_before_moving():
    calls = _teardown_with(FakeTeardownTestbed(brake_released=False))
    assert calls.index("brake:release") < calls.index("move:0.0")


def test_teardown_skips_the_move_if_the_brake_cannot_be_released():
    """Losing the park at 0 is cheap; driving the axis into a held brake is not.
    The testbed's own teardown re-engages the brake and drops the bus either
    way."""
    calls = _teardown_with(FakeTeardownTestbed(brake_released=False, releasable=False))
    assert "move:0.0" not in calls, "a move was commanded with the brake still engaged"
