"""The brake endurance test: stopping a moving load, over and over.

Every ordering here is the opposite of the one the dwell brake uses, and each
inversion is what keeps the motor out of a closing brake and the controller off a
stale setpoint. These run against fakes - no subprocess, no instrument.
"""
from __future__ import annotations

import pytest

from testbeds.ydrive_testbed.ydrive_testbed import BRAKE_SETTLE_S, METERS_PER_TURN, Motion
from testcases.ydrive.channels import DEFAULT_STATE
from testcases.ydrive.rulebooks.ydrive_rulebook import (
    MAX_STOPPING_DISTANCE_M,
    YDRIVE_RULEBOOK,
)
from testcases.ydrive.teststeps.teststeps import (
    BRAKE_TRIGGER_VELOCITY_LIMIT,
    await_operator,
    brake_from_speed,
    release_brake_in_place,
)

TRIGGER_TURNS_S = 1.8 / METERS_PER_TURN  # 21.43


class FakeBrakeTestbed:
    """A load that accelerates to a speed, then coasts to a stop over a distance
    once the brake is commanded.

    `stopping_turns` is how far it travels after the brake command, which is the
    number the test under test is measuring - None for a brake that never stops
    it at all."""

    def __init__(self, stopping_turns=10.0, reaches: float = 25.0):
        self.calls = []
        self.command = self
        self.position = 0.0
        self.velocity = 0.0
        self.armed = True
        self.braked = False
        self._reaches = reaches
        self._stopping_turns = stopping_turns

    # ODrive side
    def set_position(self, target):
        self.calls.append(f"move:{target}")

    def set_axis_state(self, state):
        self.calls.append(f"axis:{state}")
        self.armed = state == "CLOSED_LOOP_CONTROL"

    def get_motion(self):
        channels = self._channels()
        return Motion(
            position=channels["pos_estimate"],
            velocity=channels["vel_estimate"],
            armed=self.armed,
        )

    def _channels(self):
        if self.braked and self._stopping_turns is None:
            # A brake that does not stop the load: still moving, still travelling.
            self.position += 1.0
            return {"pos_estimate": self.position, "vel_estimate": self._reaches}
        if self.braked:
            # Decelerating: cover the stopping distance, then read as stopped.
            remaining = self._braked_at + self._stopping_turns - self.position
            if remaining > 0:
                self.position += min(remaining, self._stopping_turns / 2)
                return {"pos_estimate": self.position, "vel_estimate": self._reaches / 2}
            return {"pos_estimate": self.position, "vel_estimate": 0.0}
        # Accelerating toward the trigger.
        self.velocity = min(self._reaches, self.velocity + self._reaches / 2)
        self.position += 1.0
        return {"pos_estimate": self.position, "vel_estimate": self.velocity}

    def get_pos_estimate(self):
        return self.position

    def get_axis_armed_status(self):
        return self.armed

    # supply side
    def power_brake_bus(self, enabled):
        self.calls.append("brake:release" if enabled else "brake:engage")
        self.braked = not enabled
        if self.braked:
            self._braked_at = self.position


class FakeTestCase:
    test_id = "test-brake-endurance"

    def __init__(self, testbed):
        self.testbed = testbed
        self.state = {}
        self.acked = True

    def set_state(self, name, value):
        self.state[name] = value

    def wait_for(self, seconds):
        self.testbed.calls.append(f"wait:{seconds}")

    def check_should_continue(self):
        pass

    def operator_ack_path(self, tmp=None):
        return self._ack_path


# --- stopping a moving load -------------------------------------------------


def _brake_once(**kwargs):
    testbed = FakeBrakeTestbed(**kwargs)
    case = FakeTestCase(testbed)
    brake_from_speed(case, target=110.0, trigger_speed=TRIGGER_TURNS_S)
    return case, testbed


def test_the_motor_is_idled_before_the_brake_closes():
    """The inverse of engage_brake(), and it has to be: the load is moving, so the
    brake closes on a coasting axis. The other order would have the motor driving
    into a closing brake."""
    _, testbed = _brake_once()

    assert testbed.calls.index("axis:IDLE") < testbed.calls.index("brake:engage")


def test_the_axis_is_never_commanded_to_stop():
    """What stops the load is the brake. A commanded decelerating move would be
    measuring the controller instead."""
    _, testbed = _brake_once()

    moves = [c for c in testbed.calls if c.startswith("move:")]
    assert moves == ["move:110.0"], f"the axis was commanded after the run-up: {moves}"


def test_the_speed_and_distance_of_each_event_are_recorded_in_metres():
    case, _ = _brake_once(stopping_turns=10.0, reaches=TRIGGER_TURNS_S)

    assert case.state["brake_speed_m_s"] == pytest.approx(1.8, abs=0.01)
    assert case.state["stopping_distance_m"] == pytest.approx(10.0 * METERS_PER_TURN, abs=0.01)


def test_a_load_that_never_reaches_the_trigger_speed_fails_on_arrival_not_a_clock():
    """Observed on the loaded stand: it drove the whole 8.75 m stroke, peaked 2%
    under the trigger, then decelerated into the target - and the old time bound
    reported the speed at the moment the timer expired rather than the peak, which
    is the number that says whether the requested speed is achievable at all."""
    testbed = FakeBrakeTestbed(reaches=TRIGGER_TURNS_S / 4)
    with pytest.raises(RuntimeError) as excinfo:
        brake_from_speed(FakeTestCase(testbed), target=110.0, trigger_speed=TRIGGER_TURNS_S)

    message = str(excinfo.value)
    assert "arrived at 110.0 turns" in message
    assert "peaked at 5.36 turns/s" in message, f"the peak is what matters: {message}"
    assert "brake:engage" not in testbed.calls, "no brake event on a run-up that failed"


def test_a_load_that_never_stops_is_an_error():
    """A brake that does not stop the load is a failure, not something to keep
    waiting on."""
    testbed = FakeBrakeTestbed(stopping_turns=None)
    with pytest.raises(TimeoutError, match="still moving"):
        brake_from_speed(
            FakeTestCase(testbed),
            target=110.0,
            trigger_speed=TRIGGER_TURNS_S,
            stop_timeout_s=0.05,
        )


# --- handing the load back --------------------------------------------------


def test_the_setpoint_is_parked_where_the_axis_is_before_arming():
    """After a brake stop the last commanded target is the far end of the stroke.
    Arming on it would have the controller lunge for it the instant the brake let
    go."""
    testbed = FakeBrakeTestbed()
    testbed.position = 41.5
    case = FakeTestCase(testbed)

    release_brake_in_place(case)

    assert testbed.calls[:2] == ["move:41.5", "axis:CLOSED_LOOP_CONTROL"]
    assert testbed.calls.index("move:41.5") < testbed.calls.index("brake:release")


# --- the limits this test runs under ----------------------------------------


def test_the_velocity_ceiling_is_above_the_trigger_speed():
    """Below it, the controller clamps and the brake never fires - the run would
    wait out its trigger timeout on a healthy stand."""
    assert BRAKE_TRIGGER_VELOCITY_LIMIT > TRIGGER_TURNS_S


def test_a_bad_stop_aborts_through_the_rulebook_rather_than_an_exception():
    """So the value that ended the run lands in the verdict's timeline."""
    bound = next(b for b in YDRIVE_RULEBOOK.bounds if b.channel == "stopping_distance_m")

    assert bound.upper == MAX_STOPPING_DISTANCE_M == 3.25
    assert bound.fatal is True
    assert bound.persistence_s is None, "one number per event - debouncing waits for a second one"
    assert bound.evaluate({"stopping_distance_m": 3.5}) is True
    assert bound.evaluate({"stopping_distance_m": 3.0}) is False
    assert bound.evaluate({"stopping_distance_m": 1.5}) is False


def test_the_seeded_distance_is_a_number_so_a_run_can_start():
    """A live run caught this: seeded None, the bound was unevaluable on the first
    frame - and unevaluable stops a run - so every run died before anything moved.
    The channel has to carry a number from frame 1."""
    assert DEFAULT_STATE["stopping_distance_m"] == 0.0
    assert DEFAULT_STATE["brake_speed_m_s"] == 0.0
    bound = next(b for b in YDRIVE_RULEBOOK.bounds if b.channel == "stopping_distance_m")

    assert bound.evaluate(DEFAULT_STATE) is False, "a fresh run must be able to start"


def test_the_brake_settle_wait_is_part_of_the_stopping_distance():
    """Distance is measured from the brake command, so the coast before the brake
    bites counts against the budget - understating it by starting from first
    deceleration would flatter the brake."""
    assert BRAKE_SETTLE_S * 1.8 < MAX_STOPPING_DISTANCE_M


def test_the_move_timeout_covers_the_stroke_at_any_limit_this_test_would_use():
    """move_to's 10 s default fits the raised limit only: 110 turns at a limit set
    to cruise at 0.5 m/s needs 18.5 s and would fail a run for a move that was
    working. One flat number covers both, and still catches a stalled axis."""
    from testcases.ydrive.testcases.testcases import BrakeEnduranceTest
    from testcases.ydrive.teststeps.teststeps import DEFAULT_ARRIVAL_TIMEOUT_S

    test = BrakeEnduranceTest(require_engine=False)
    at_raised_limit = test.START_POSITION / test.BRAKE_RUN_VELOCITY_LIMIT
    at_cruise_for_half_a_metre_per_second = test.START_POSITION / 5.95

    assert at_cruise_for_half_a_metre_per_second > DEFAULT_ARRIVAL_TIMEOUT_S, (
        "this test is checking nothing if the default already covered it"
    )
    assert test.MOVE_TIMEOUT_S > at_cruise_for_half_a_metre_per_second
    assert test.MOVE_TIMEOUT_S < test.START_LINE_DWELL_S, (
        "a stalled move should report well inside a dwell, not be waited out for longer"
    )
    assert at_raised_limit < test.MOVE_TIMEOUT_S


# --- resting between events -------------------------------------------------


def test_the_dwell_is_held_by_the_brake_with_the_axis_idle():
    """The state that dissipates nothing: a magnet-applied brake needs no power to
    hold, and an idled axis draws no current - so a thermal reading recovers over
    it. A minute of the controller holding position would heat the motor through
    the very interval that is supposed to be cooling."""
    from testcases.ydrive.teststeps.teststeps import dwell_braked

    testbed = FakeBrakeTestbed()
    case = FakeTestCase(testbed)

    dwell_braked(case, 60.0)

    assert testbed.calls == [
        "brake:engage",     # the brake takes the load before the controller lets go
        f"wait:{BRAKE_SETTLE_S}",
        "axis:IDLE",        # nothing drives against an engaged brake
        "wait:60.0",        # the rest itself, dissipating nothing
        "axis:CLOSED_LOOP_CONTROL",  # controller takes hold before the brake lets go
        "brake:release",
        f"wait:{BRAKE_SETTLE_S}",
    ]


def test_an_aborted_dwell_leaves_the_load_on_the_brake():
    """wait_for() raises on a fatal bound, a stop request or a lost recorder, and
    handing the load back at the one moment the reason for stopping is unknown is
    the wrong reflex."""
    from testcases.ydrive.teststeps.teststeps import dwell_braked

    testbed = FakeBrakeTestbed()
    case = FakeTestCase(testbed)

    def boom(seconds):
        testbed.calls.append(f"wait:{seconds}")
        if seconds == 60.0:
            raise RuntimeError("fatal bound")

    case.wait_for = boom
    with pytest.raises(RuntimeError, match="fatal bound"):
        dwell_braked(case, 60.0)

    assert [c for c in testbed.calls if c.startswith("brake:")] == ["brake:engage"]
    assert testbed.armed is False, "the axis must be left idle, not re-armed"


def test_the_dwell_sets_the_cycle_rate():
    """Worth stating in a test because nothing else in the test constrains how often
    the brake is used, and the dwell is the whole of it."""
    from testcases.ydrive.testcases.testcases import BrakeEnduranceTest

    test = BrakeEnduranceTest(require_engine=False)
    # The traverse the axis actually takes, not MOVE_TIMEOUT_S - that is a ceiling
    # for a stalled move, not how long a healthy one lasts.
    traverse_s = test.START_POSITION / test.BRAKE_RUN_VELOCITY_LIMIT
    cycle_s = test.START_LINE_DWELL_S + traverse_s + test.POST_BRAKE_DWELL_S
    assert test.START_LINE_DWELL_S > 0.8 * cycle_s, "the dwell should dominate the cycle"
    events_per_hour = 3600 / cycle_s
    assert 10 < events_per_hour < 13, (
        f"{events_per_hour:.0f} events an hour is not a five-minute dwell"
    )


# --- an axis that stops driving on its own ------------------------------------


class DisarmingTestbed(FakeBrakeTestbed):
    """A board that disarms itself part-way through a move, as the real one does on
    a current limit violation - by dropping to IDLE with no exception anywhere."""

    def __init__(self, disarm_after: int = 3, **kwargs):
        super().__init__(**kwargs)
        self._reads = 0
        self._disarm_after = disarm_after

    def _channels(self):
        self._reads += 1
        if self._reads > self._disarm_after:
            self.armed = False
        return super()._channels()

    def describe_errors(self):
        return {"disarm_reason": "CURRENT_LIMIT_VIOLATION", "axis_current_state": "IDLE"}


def test_a_move_stops_the_moment_the_axis_disarms():
    """Observed on the stand: an axis disarmed on CURRENT_LIMIT_VIOLATION and the
    load coasted for the full 45 s move timeout - brake released, controller idle,
    held by neither. One frame is what that should cost."""
    from testcases.ydrive.teststeps.teststeps import move_to

    testbed = DisarmingTestbed()
    with pytest.raises(RuntimeError) as excinfo:
        move_to(FakeTestCase(testbed), 500.0, arrival_timeout_s=45.0)

    message = str(excinfo.value)
    assert "stopped driving" in message
    assert "CURRENT_LIMIT_VIOLATION" in message, "the board's own reason is what to report"


def test_a_run_up_stops_the_moment_the_axis_disarms():
    """Same for the acceleration to the trigger speed, which otherwise waits out
    its own timeout while the load coasts."""
    from testcases.ydrive.teststeps.teststeps import brake_from_speed

    # Disarms before the trigger speed is reached, which is when the axis is
    # working hardest and so when the real board latched its violation.
    testbed = DisarmingTestbed(reaches=TRIGGER_TURNS_S, disarm_after=1)
    with pytest.raises(RuntimeError, match="stopped driving"):
        brake_from_speed(FakeTestCase(testbed), target=0.0, trigger_speed=TRIGGER_TURNS_S)

    assert "brake:engage" not in testbed.calls, "no brake event should be recorded"


def test_the_brake_keeps_the_load_before_the_distance_is_taken():
    """The rest is inside the event, and it touches neither the rail nor the axis -
    they were left where they should be when the brake closed."""
    _, testbed = _brake_once()

    after_brake = testbed.calls[testbed.calls.index("brake:engage"):]
    assert "wait:5.0" in after_brake, f"the brake did not keep the load: {after_brake}"
    assert [c for c in after_brake if c.startswith(("brake:", "axis:"))] == ["brake:engage"], (
        "the rail or the axis was touched while the brake held the load"
    )


def test_creep_while_the_brake_holds_counts_against_the_stopping_distance():
    """A brake that stops the load and then lets it creep has not stopped it in
    that distance, so the measurement ends after the rest rather than at first
    standstill."""

    class CreepingCase(FakeTestCase):
        def wait_for(self, seconds):
            super().wait_for(seconds)
            if seconds == 5.0:
                self.testbed.position += 5.0  # 0.42 m of creep while held

    testbed = FakeBrakeTestbed(stopping_turns=10.0, reaches=TRIGGER_TURNS_S)
    case = CreepingCase(testbed)
    brake_from_speed(case, target=110.0, trigger_speed=TRIGGER_TURNS_S)

    assert case.state["stopping_distance_m"] == pytest.approx(15.0 * METERS_PER_TURN, abs=0.01), (
        "the creep is missing from the distance"
    )
