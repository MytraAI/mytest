"""The cycling brake endurance test: a brake event earned by a kilometre of travel.

Two things here are new rather than borrowed from the brake endurance test, and
both exist because this test accepts overshoot instead of waiting it out:
move_to() reporting the furthest position it reached rather than the one it
accepted arrival at, and the distance being measured between consecutive
turnarounds rather than derived from the setpoints. The third is not this test's at all: bringing a moving load to rest
before the brake rail drops belongs to the stand, so it lives on the testbed and
every ydrive test gets it.

These run against fakes - no subprocess, no instrument.
"""
from __future__ import annotations

import pytest

from testbeds.ydrive_testbed.ydrive_testbed import (
    METERS_PER_TURN,
    Motion,
    settle_load_under_controller,
)
from testcases.registry import REGISTERED_TESTS
from testcases.ydrive.channels import DEFAULT_STATE
from testcases.ydrive.rulebooks.ydrive_rulebook import TEST_NAMES
from testcases.ydrive.testcases.testcases import (
    BrakeEnduranceTest,
    CycleBrakeEnduranceTest,
)
from testcases.ydrive.teststeps.teststeps import (
    MAX_LOAD_VELOCITY_LIMIT,
    OVER_ENERGY_VELOCITY_LIMIT,
    move_to,
)


class FakeAxis:
    """An axis that walks a scripted list of (position, velocity) frames.

    Scripted rather than simulated because what is under test is which frame a
    loop accepts, not the dynamics that produced it - and the interesting frames
    are exactly the ones a plausible simulation would smooth over."""

    def __init__(self, frames, armed: bool = True):
        self.command = self
        self.calls = []
        self._frames = list(frames)
        self._armed = armed
        self.last = self._frames[0]

    def set_position(self, target):
        self.calls.append(f"move:{target}")

    def set_axis_state(self, state):
        self.calls.append(f"axis:{state}")

    def get_motion(self):
        if self._frames:
            self.last = self._frames.pop(0)
        return Motion(position=self.last[0], velocity=self.last[1], armed=self._armed)

    def get_pos_estimate(self):
        return self.last[0]

    def power_brake_bus(self, enabled):
        self.calls.append("brake:release" if enabled else "brake:engage")


class FakeTestCase:
    test_id = "test-cycle-brake-endurance"

    def __init__(self, testbed):
        self.testbed = testbed
        self.state = {}

    def set_state(self, name, value):
        self.state[name] = value

    def wait_for(self, seconds):
        self.testbed.calls.append(f"wait:{seconds}")

    def check_should_continue(self):
        pass


class Accumulator(CycleBrakeEnduranceTest):
    """The real distance bookkeeping, with the run's plumbing left out.

    TestCase.__init__ is skipped deliberately: set_state() goes through a state
    publisher that only exists once a run is open, and what is under test here is
    arithmetic over positions. `start_at` stands in for the origin, which
    main_execution seeds _last_position with before anything moves."""

    def __init__(self, start_at: float = 0.0, brake_interval_m=None):
        self.total_distance_m = 0.0
        self.brake_cycles = 0
        self._brake_interval_m = brake_interval_m or self.BRAKE_INTERVAL_M
        self._last_position = start_at
        self.state = {}

    def set_state(self, name, value):
        self.state[name] = value


# --- move_to reports where it stopped, not where it was aimed ----------------

CYCLE_TOLERANCES = dict(
    position_tolerance=CycleBrakeEnduranceTest.CYCLE_POSITION_TOLERANCE,
    velocity_tolerance=CycleBrakeEnduranceTest.CYCLE_VELOCITY_TOLERANCE,
)
"""The gates the cycling actually runs under, read off the class rather than
re-typed - otherwise tightening either constant leaves these tests passing while
exercising a regime the test no longer runs in."""


def test_move_to_reports_the_accepted_frame_when_nothing_overshoots():
    """With no overshoot the furthest point and the accepted point are one frame, so
    a caller that only wanted to know where the move ended is unaffected. Under a
    3 turns/s gate that frame is still short of the target, so returning the target
    would report travel that never happened."""
    axis = FakeAxis([(60.0, 18.0), (105.0, 12.0), (109.6, 2.5)])

    assert move_to(FakeTestCase(axis), 110.0, **CYCLE_TOLERANCES) == pytest.approx(109.6)


def test_move_to_will_not_accept_a_frame_still_at_cruise():
    """The position gate passes from 10 turns out, so the velocity gate is the one
    that decides. A frame inside the position tolerance but still at speed is a load
    passing through the target, not one that has arrived."""
    axis = FakeAxis([(105.0, 18.0), (108.0, 18.0), (109.7, 1.0)])

    assert move_to(FakeTestCase(axis), 110.0, **CYCLE_TOLERANCES) == pytest.approx(109.7)


# --- distance is measured, not derived --------------------------------------


def test_distance_is_the_gap_between_consecutive_resting_positions():
    case = Accumulator(start_at=110.6)
    case._travelled_to(-0.4)

    assert case.total_distance_m == pytest.approx(111.0 * METERS_PER_TURN)


def test_travel_is_counted_from_the_origin_the_load_starts_at():
    """main_execution seeds _last_position with the origin, because that is where
    release_brake_in_place() left the load - so the one move up to the start line is
    travel rather than a gap in the record."""
    case = Accumulator(start_at=4.0)
    case._travelled_to(114.0)

    assert case.total_distance_m == pytest.approx(110.0 * METERS_PER_TURN)


def test_overshoot_counts_as_travel_the_setpoints_cannot_see():
    """A cycle that overshoots both ends travels further than 2 x the stroke. The
    setpoint arithmetic EnduranceCycleTest uses would report the stroke."""
    case = Accumulator(start_at=110.0)
    case._travelled_to(-1.5)
    case._travelled_to(111.5)

    derived = 2 * CycleBrakeEnduranceTest.START_POSITION * METERS_PER_TURN
    assert case.total_distance_m == pytest.approx(224.5 * METERS_PER_TURN)
    assert case.total_distance_m > derived


def test_both_distance_channels_are_published():
    case = Accumulator()
    case._travelled_to(110.0)

    assert case.state["total_distance_m"] == pytest.approx(110.0 * METERS_PER_TURN)
    assert case.state["distance_since_brake_m"] == pytest.approx(110.0 * METERS_PER_TURN)


def test_the_interval_channel_is_derived_rather_than_counted():
    """A maintained counter would be a second copy of total_distance_m with its own
    chance to drift out of step with it."""
    case = Accumulator(brake_interval_m=1000.0)
    case._travelled_to(20000.0)
    case.brake_cycles = 1
    case._travelled_to(case._last_position)  # republish, no added travel

    expected = 20000.0 * METERS_PER_TURN - case._brake_interval_m
    assert case.state["distance_since_brake_m"] == pytest.approx(expected, abs=0.1)


def test_an_interval_that_overran_gives_the_metres_back_to_the_next_one():
    """A brake event fires at the first cycle boundary past the multiple it is owed
    at, so an interval overruns by up to a cycle. Because the trigger is a multiple
    of the total rather than a countdown from the last event, those metres land in
    the next interval instead of being discarded - discarding them is what would
    make the average interval drift a cycle high every time."""
    case = Accumulator(brake_interval_m=1000.0)
    overran = 12100.0 * METERS_PER_TURN  # one cycle past the first multiple
    case._travelled_to(12100.0)

    assert case.total_distance_m == pytest.approx(overran, abs=0.1)
    assert case.state["distance_since_brake_m"] == pytest.approx(overran, abs=0.1)

    case.brake_cycles = 1  # the event happens
    case._travelled_to(case._last_position)

    assert (case.brake_cycles + 1) * case._brake_interval_m == 2000.0
    assert case.state["distance_since_brake_m"] == pytest.approx(
        overran - case._brake_interval_m, abs=0.1
    )


# --- the velocity schedule --------------------------------------------------


def test_the_run_up_ceiling_has_to_be_raised_and_is_enough_when_it_is():
    """The trigger sits above the cycling ceiling, so a run-down under the duty
    tuning would clamp below it and never fire the brake - and below the raised one,
    so the run-up can reach it."""
    trigger = CycleBrakeEnduranceTest(require_engine=False).trigger_speed_turns_s

    assert trigger > CycleBrakeEnduranceTest.CYCLE_VELOCITY_LIMIT
    assert trigger < CycleBrakeEnduranceTest.VELOCITY_LIMIT


# --- wiring -----------------------------------------------------------------


def test_the_distance_channels_are_seeded():
    """The engine fixes a file's header from the union of its first frames and drops
    channels that appear later. Neither of these is published until the stand is open
    and the load has moved, which is well past that."""
    assert DEFAULT_STATE["total_distance_m"] == 0.0
    assert DEFAULT_STATE["distance_since_brake_m"] == 0.0


def test_the_test_name_is_in_the_rulebook_and_the_registry():
    """A TEST_NAME the rulebook does not list is a run with no bounds evaluated, and
    one the registry does not list cannot be started by name."""
    assert CycleBrakeEnduranceTest.TEST_NAME in TEST_NAMES
    assert "ydrive.cycle_brake_endurance" in REGISTERED_TESTS


def test_it_does_not_share_a_test_name_with_the_test_it_was_built_from():
    """Stored runs are keyed by TEST_NAME. Reusing brake_endurance_test would merge
    two different tests into one population."""
    assert CycleBrakeEnduranceTest.TEST_NAME != BrakeEnduranceTest.TEST_NAME


# --- teardown stops the load before the brake is asked to --------------------


def test_the_setpoint_is_parked_where_the_axis_is_not_where_it_was_going():
    """Commanding the old target would finish the stroke instead of stopping."""
    axis = FakeAxis([(42.0, 12.0), (42.5, 0.0)])
    settle_load_under_controller(axis)

    assert axis.calls == ["move:42.0"]


def test_a_load_already_at_rest_is_left_alone():
    axis = FakeAxis([(42.0, 0.0)])
    settle_load_under_controller(axis)

    assert axis.calls == [], "nothing to stop, so nothing commanded"


def test_a_load_that_will_not_stop_is_handed_to_the_brake_rather_than_raising():
    """An attempt, not a guarantee: stop() engages the brake next, which is where
    the load would have ended up without this at all. Raising here would mask
    whatever ended the run, and stop() must reach the 48 V bus either way."""
    axis = FakeAxis([(42.0, 12.0)] * 200)

    settle_load_under_controller(axis, settle_s=0.05)


def test_a_disarmed_axis_is_not_waited_on():
    """Nothing is driving the load, so there is nothing for the controller to do -
    the brake is the only thing left that can stop it. This is also the state
    ManualTest leaves behind, where an operator may have idled the axis anywhere.

    The frame count is the assertion: waiting out settle_s would consume the whole
    scripted list, so returning early is the only way to leave frames behind."""
    axis = FakeAxis([(42.0, 12.0)] * 200, armed=False)

    settle_load_under_controller(axis, settle_s=60.0)

    assert len(axis._frames) > 190, "it waited on an axis that was not driving"


def test_move_to_reports_the_peak_when_arrival_is_accepted_on_the_pullback():
    """The case the stand actually produces, and the one an accepted-position
    return gets wrong.

    Measured at 1800 lb the load runs 17.3 turns past each end - wider than
    CYCLE_POSITION_TOLERANCE - so the peak cannot satisfy arrival and the load is
    accepted on the way back, near the target. Returning that accepted position
    drops the whole out-and-back excursion from the distance: a cycling block
    covering 156.2 m of track counted 105.2 m, 67% of it."""
    axis = FakeAxis([
        (60.0, 18.0),     # cruising out
        (127.3, 5.0),     # past the target by 17.3 turns - outside the position gate
        (127.3, 0.0),     # the peak: still 17.3 out, so still not arrived
        (118.0, -8.0),    # controller pulling it back
        (112.0, -2.5),    # inside both gates at last - arrival accepted here
    ])

    reached = move_to(FakeTestCase(axis), 110.0, **CYCLE_TOLERANCES)

    assert reached == pytest.approx(127.3), "the excursion past the end was dropped"


def test_a_downward_move_reports_its_lowest_point():
    """Direction is taken from the first frame of the move, not assumed, so the
    same rule holds on the leg back down."""
    axis = FakeAxis([(110.0, -18.0), (-7.4, -5.0), (-7.4, 0.0), (2.0, 8.0), (8.0, 2.0)])

    assert move_to(FakeTestCase(axis), 0.0, **CYCLE_TOLERANCES) == pytest.approx(-7.4)


def test_too_short_an_interval_is_refused_rather_than_run():
    """A brake event has to start from the top of the stroke. The cycling loop is
    checked between whole cycles, so an interval shorter than one cycle plus a
    run-down can be satisfied before the loop runs at all - and then the run-down
    starts from wherever the last brake left the load, part-way down. From there the
    load either stops past 0 into the clearance the brake needs, or never reaches the
    trigger speed and fails in a way that reads as a tuning problem.

    Refused at construction, because the constructor advertises this override for
    shakedowns and metres is exactly the range that trips it."""
    with pytest.raises(ValueError, match="shorter than one cycle"):
        CycleBrakeEnduranceTest(require_engine=False, brake_interval_m=10.0)


def test_the_default_interval_clears_the_floor_by_a_wide_margin():
    assert CycleBrakeEnduranceTest.BRAKE_INTERVAL_M > 10 * CycleBrakeEnduranceTest.MIN_BRAKE_INTERVAL_M


# --- the brake event is counted where it happens ----------------------------


def test_a_brake_event_is_counted_at_engagement_not_on_the_way_out():
    """@step re-checks for a fatal bound as a step returns, and the stopping distance
    brake_from_speed publishes is itself able to trip one - so a counter driven off
    the return value drops exactly the event most worth counting.

    Counting at engagement means the event survives whatever the load does next."""
    from testcases.ydrive.teststeps.teststeps import brake_from_speed

    counted = []
    # The brake fires, and then the load never comes to rest - the worst stop there
    # is, and the one whose count a return-value counter would lose.
    axis = FakeAxis([(0.0, 25.0), (5.0, 25.0)] + [(9.0, 25.0)] * 4)

    with pytest.raises(TimeoutError, match="still moving"):
        brake_from_speed(
            FakeTestCase(axis), target=110.0, trigger_speed=21.0, stop_timeout_s=0.0,
            on_engaged=lambda: counted.append(1),
        )

    assert counted == [1], "the brake fired, so the event happened"


def test_a_run_up_that_never_reaches_the_trigger_counts_no_event():
    """The other edge: this counts events the brake performed, not attempts."""
    from testcases.ydrive.teststeps.teststeps import brake_from_speed

    counted = []
    axis = FakeAxis([(0.0, 1.0), (109.9, 1.0), (110.0, 1.0)])

    with pytest.raises(RuntimeError, match="without ever reaching"):
        brake_from_speed(
            FakeTestCase(axis), target=110.0, trigger_speed=21.0,
            on_engaged=lambda: counted.append(1),
        )

    assert counted == [], "nothing braked, so nothing to count"


def test_the_power_envelope_is_watched_and_only_recorded():
    """The channel that actually reports the limit overcurrent_bound cannot reach.
    Recorded rather than fatal until there is data on how often the stand hits it."""
    from testcases.ydrive.rulebooks.ydrive_rulebook import YDRIVE_RULEBOOK

    bound = next(b for b in YDRIVE_RULEBOOK.bounds if b.channel == "in_power_limit_2")

    assert bound.expected is False
    assert bound.fatal is False, "not fatal until the stand says how often it happens"
