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

from protocol.wire import DEVICE_ODRIVE
from testbeds.ydrive_testbed.ydrive_testbed import (
    METERS_PER_TURN,
    Motion,
    YdriveTestbed,
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
    BRAKE_TRIGGER_VELOCITY_LIMIT,
    move_to,
)


class FakeStand:
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


class DistanceCase(CycleBrakeEnduranceTest):
    """derived_channels() with the run's plumbing left out, standing in for its own testbed.

    TestCase.__init__ is skipped deliberately: the publisher only exists once a run is open,
    and what is under test is what this test makes of one ODrive frame."""

    def __init__(self, brake_interval_m=None):
        self.testbed = YdriveTestbed
        self.brake_cycles = 0
        self.distance_at_last_correction_m = 0.0
        self._brake_interval_m = brake_interval_m or self.BRAKE_INTERVAL_M
        self._distance_at_run_start_m = None  # seeded by main_execution, not __init__

    def derive(self, turns_traveled):
        """The derivation against one ODrive frame, which is all it ever sees."""
        return self.derived_channels({DEVICE_ODRIVE: {"turns_traveled": turns_traveled}})


# --- move_to reports where it stopped, not where it was aimed ----------------

CYCLE_TOLERANCES = dict(
    position_tolerance=CycleBrakeEnduranceTest.CYCLE_POSITION_TOLERANCE,
    velocity_tolerance=CycleBrakeEnduranceTest.CYCLE_VELOCITY_TOLERANCE,
)
"""The gates the cycling actually runs under, read off the class rather than
re-typed - otherwise tightening either constant leaves these tests passing while
exercising a regime the test no longer runs in."""


def test_move_to_reports_where_arrival_was_accepted_not_the_target():
    """Under a 3 turns/s gate the accepted frame is still short of the target, and it is
    what the load did. Distance travelled does not come from here - the driver counts the
    path - so this is the whole of what move_to owes a caller."""
    axis = FakeStand([(60.0, 18.0), (105.0, 12.0), (109.6, 2.5)])

    assert move_to(FakeTestCase(axis), 110.0, **CYCLE_TOLERANCES) == pytest.approx(109.6)


def test_move_to_will_not_accept_a_frame_still_at_cruise():
    """The position gate passes from 10 turns out, so the velocity gate is the one
    that decides. A frame inside the position tolerance but still at speed is a load
    passing through the target, not one that has arrived."""
    axis = FakeStand([(105.0, 18.0), (108.0, 18.0), (109.7, 1.0)])

    assert move_to(FakeTestCase(axis), 110.0, **CYCLE_TOLERANCES) == pytest.approx(109.7)


# --- distance comes from the driver's counter -------------------------------


def test_nothing_is_derived_until_the_run_knows_where_its_cycling_started():
    """runner.start() is called before setup, so the sampler is live while a person is still
    pushing the load to the marker. Publishing then would book that push as mileage."""
    case = DistanceCase()

    assert case.derive(500.0) == {}, "no baseline yet, so nothing true to say"

    case._distance_at_run_start_m = 12.0
    assert case.derive(500.0)["total_distance_m"] == pytest.approx(
        500.0 * METERS_PER_TURN - 12.0
    )


def test_the_run_starts_from_where_the_drivers_counter_already_stood():
    """The driver counts from its own connect: the operator's hand move to the marker at
    setup is not this run's mileage."""
    case = DistanceCase()
    case._distance_at_run_start_m = 40.0 * METERS_PER_TURN

    derived = case.derive(62.5)

    assert derived["total_distance_m"] == pytest.approx(22.5 * METERS_PER_TURN)


def test_the_interval_channel_is_derived_rather_than_counted():
    """A maintained counter would be a second copy of total_distance_m with its own
    chance to drift out of step with it."""
    case = DistanceCase(brake_interval_m=1000.0)
    case._distance_at_run_start_m = 0.0
    case.brake_cycles = 1

    expected = 20000.0 * METERS_PER_TURN - case._brake_interval_m
    assert case.derive(20000.0)["distance_since_brake_m"] == pytest.approx(expected, abs=0.1)


def test_an_interval_that_overran_gives_the_metres_back_to_the_next_one():
    """A brake event fires at the first cycle boundary past the multiple it is owed
    at, so an interval overruns by up to a cycle. Because the trigger is a multiple
    of the total rather than a countdown from the last event, those metres land in
    the next interval instead of being discarded - discarding them is what would
    make the average interval drift a cycle high every time."""
    case = DistanceCase(brake_interval_m=1000.0)
    case._distance_at_run_start_m = 0.0
    overran = 12100.0 * METERS_PER_TURN  # one cycle past the first multiple

    derived = case.derive(12100.0)
    assert derived["total_distance_m"] == pytest.approx(overran, abs=0.1)
    assert derived["distance_since_brake_m"] == pytest.approx(overran, abs=0.1)

    case.brake_cycles = 1  # the event happens

    assert (case.brake_cycles + 1) * case._brake_interval_m == 2000.0
    assert case.derive(12100.0)["distance_since_brake_m"] == pytest.approx(
        overran - case._brake_interval_m, abs=0.1
    )


# --- the velocity schedule --------------------------------------------------


def test_the_run_up_ceiling_has_to_be_raised_and_is_enough_when_it_is():
    """The trigger sits above the cycling ceiling, so a run-down under the duty
    tuning would clamp below it and never fire the brake - and below the raised one,
    so the run-up can reach it."""
    trigger = CycleBrakeEnduranceTest(require_engine=False).trigger_speed_turns_s

    assert trigger > CycleBrakeEnduranceTest.CYCLE_VELOCITY_LIMIT
    assert trigger < CycleBrakeEnduranceTest.BRAKE_RUN_VELOCITY_LIMIT


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
    # Stored runs are keyed by TEST_NAME, so reusing one would merge two populations.
    assert CycleBrakeEnduranceTest.TEST_NAME != BrakeEnduranceTest.TEST_NAME


# --- teardown stops the load before the brake is asked to --------------------


def test_the_setpoint_is_parked_where_the_axis_is_not_where_it_was_going():
    """Commanding the old target would finish the stroke instead of stopping."""
    axis = FakeStand([(42.0, 12.0), (42.5, 0.0)])
    settle_load_under_controller(axis)

    assert axis.calls == ["move:42.0"]


def test_a_load_already_at_rest_is_left_alone():
    axis = FakeStand([(42.0, 0.0)])
    settle_load_under_controller(axis)

    assert axis.calls == [], "nothing to stop, so nothing commanded"


def test_a_load_that_will_not_stop_is_handed_to_the_brake_rather_than_raising():
    """An attempt, not a guarantee: stop() engages the brake next, which is where
    the load would have ended up without this at all. Raising here would mask
    whatever ended the run, and stop() must reach the 48 V bus either way."""
    axis = FakeStand([(42.0, 12.0)] * 200)

    settle_load_under_controller(axis, settle_s=0.05)


def test_a_disarmed_axis_is_not_waited_on():
    """Nothing is driving the load, so there is nothing for the controller to do -
    the brake is the only thing left that can stop it. This is also the state
    ManualTest leaves behind, where an operator may have idled the axis anywhere.

    The frame count is the assertion: waiting out settle_s would consume the whole
    scripted list, so returning early is the only way to leave frames behind."""
    axis = FakeStand([(42.0, 12.0)] * 200, armed=False)

    settle_load_under_controller(axis, settle_s=60.0)

    assert len(axis._frames) > 190, "it waited on an axis that was not driving"


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
    axis = FakeStand([(0.0, 25.0), (5.0, 25.0)] + [(9.0, 25.0)] * 4)

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
    axis = FakeStand([(0.0, 1.0), (109.9, 1.0), (110.0, 1.0)])

    with pytest.raises(RuntimeError, match="without ever reaching"):
        brake_from_speed(
            FakeTestCase(axis), target=110.0, trigger_speed=21.0,
            on_engaged=lambda: counted.append(1),
        )

    assert counted == [], "nothing braked, so nothing to count"


def test_the_power_envelope_is_recorded_but_no_longer_bounded():
    """It was bounded to find out how often the stand leaves the 420 W envelope.
    Two 1800 lb runs answered ~45-49 brief excursions an hour - the longest 43 ms -
    so every healthy run recorded FAIL for a direction reversal. The channel is
    still in the cpx400dp stream; it just no longer decides pass/fail."""
    from testcases.ydrive.rulebooks.ydrive_rulebook import YDRIVE_RULEBOOK
    from hardware.cpx400dp.cpx400dp_channels import TELEMETRY_CHANNELS

    assert not any(b.channel == "in_power_limit_2" for b in YDRIVE_RULEBOOK.bounds)
    assert "in_power_limit_2" in TELEMETRY_CHANNELS, "still recorded, just not judged"


def test_the_bus_is_bounded_from_both_directions():
    """The rail rises on regen - it peaked at 50.36 V against a 48 V setpoint -
    and nothing bounded that direction before."""
    from testcases.ydrive.rulebooks.ydrive_rulebook import (
        MAX_BUS_VOLTAGE_V, MIN_BUS_VOLTAGE_V, YDRIVE_RULEBOOK,
    )

    vbus = [b for b in YDRIVE_RULEBOOK.bounds if b.channel == "board_vbus_voltage"]
    assert {b.name for b in vbus} == {"undervoltage_bound", "overvoltage_bound"}
    assert all(b.fatal for b in vbus)
    assert MIN_BUS_VOLTAGE_V < MAX_BUS_VOLTAGE_V


def test_the_bus_ceiling_fires_before_the_drive_faults_on_its_own():
    """config.dc_bus_overvoltage_trip_level is 64.0 V on this board. A bound above
    it could only ever report the aftermath of a fault the ODrive already took."""
    from testcases.ydrive.rulebooks.ydrive_rulebook import MAX_BUS_VOLTAGE_V

    assert MAX_BUS_VOLTAGE_V < 64.0


def test_the_bus_floor_sits_where_the_stand_runs_not_at_the_drives_trip():
    """10.5 V was the drive's own dc_bus_undervoltage_trip_level, so the old bound
    could only fire at the instant the ODrive faulted by itself. The measured floor
    with the bus up was 37.14 V."""
    from testcases.ydrive.rulebooks.ydrive_rulebook import MIN_BUS_VOLTAGE_V

    assert MIN_BUS_VOLTAGE_V > 10.5
    assert MIN_BUS_VOLTAGE_V < 37.14, "must sit under the lowest reading measured"


def test_the_cycle_time_bound_is_fatal_and_undebounced():
    """One number per completed cycle, held until the next - not a sampled signal
    that can spike, so debouncing would mean waiting for a second slow cycle to
    agree with the first."""
    from testcases.ydrive.rulebooks.ydrive_rulebook import MAX_CYCLE_TIME_S, YDRIVE_RULEBOOK

    bound = next(b for b in YDRIVE_RULEBOOK.bounds if b.channel == "cycle_time_s")

    assert bound.upper == MAX_CYCLE_TIME_S == 34.0
    assert bound.lower is None, "a fast cycle is not a fault"
    assert bound.fatal is True
    assert bound.persistence_s is None


def test_the_cycle_time_ceiling_clears_the_slowest_cycle_measured():
    """308 cycles over 2.5 h at 1800 lb ran 27.92-28.59 s. A ceiling near that
    band would abort healthy runs; this one sits ~19% above the slowest."""
    from testcases.ydrive.rulebooks.ydrive_rulebook import MAX_CYCLE_TIME_S

    slowest_measured = 28.59
    assert MAX_CYCLE_TIME_S > slowest_measured
    assert MAX_CYCLE_TIME_S / slowest_measured > 1.15


def test_cycle_time_is_seeded_numeric_so_a_run_can_start():
    """A numeric bound on a channel carrying None is unevaluable, and unevaluable
    stops a run - so None here would abort every ydrive run on its first frame,
    exactly as a None stopping_distance_m once did."""
    from testcases.ydrive.channels import DEFAULT_STATE
    from testcases.ydrive.rulebooks.ydrive_rulebook import MAX_CYCLE_TIME_S

    assert DEFAULT_STATE["cycle_time_s"] == 0.0
    assert isinstance(DEFAULT_STATE["cycle_time_s"], float)
    assert DEFAULT_STATE["cycle_time_s"] < MAX_CYCLE_TIME_S, "the seed must not trip it"


def test_the_cycle_clock_excludes_the_brake_event():
    """A brake event adds ~12 s and runs OUTSIDE the cycling loop. Timing it into
    a cycle would trip a 34 s ceiling on a perfectly healthy run."""
    import inspect

    from testcases.ydrive.testcases.testcases import CycleBrakeEnduranceTest

    source = inspect.getsource(CycleBrakeEnduranceTest.main_execution)
    started = source.index("cycle_clock = Stopwatch()")
    published = source.index('set_state("cycle_time_s"')
    braked = source.index("brake_from_speed(")

    assert started < published < braked, "the clock must open and close inside the cycling loop"


def test_cycle_time_is_published_after_the_cycle_closes():
    """So the value is the time of a cycle that actually completed. A cycle that
    hangs never publishes one - that is move_to()'s arrival timeout to catch."""
    import inspect

    from testcases.ydrive.testcases.testcases import CycleBrakeEnduranceTest

    source = inspect.getsource(CycleBrakeEnduranceTest.main_execution)
    last_leg = source.index("cycle_leg(self, self.START_POSITION)",
                            source.index("cycle_clock = Stopwatch()"))
    published = source.index('set_state("cycle_time_s"')

    assert last_leg < published
