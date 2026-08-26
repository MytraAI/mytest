"""CycleBrakeHoldTest: the brake hold, repeated, with the stand's temperatures deciding
when it may lift again.

Three things this test asserts that no other zdrive test does: that a hold which slipped
measurably ends the run, that a stand too hot to lift waits at the bottom instead of
lifting anyway, and that the brake is handed the load on one cycle in HOLD_INTERVAL_CYCLES
rather than on all of them. Everything about the second is arithmetic against limits
declared elsewhere, which is the point - a second copy of a limit is a limit that drifts.
"""
from __future__ import annotations

import pytest

from testbeds.zdrive_testbed.zdrive_testbed import METERS_PER_TURN, turns_to_metres
from testcases.registry import REGISTERED_TESTS
from testcases.zdrive.channels import DEFAULT_STATE
from testcases.zdrive.rulebooks.zdrive_rulebook import (
    CYCLE_BRAKE_HOLD_TEST_NAME,
    MAX_BRAKE_SLIP_M,
    MAX_FET_TEMPERATURE_C,
    MAX_TEMPERATURE_C,
    TEST_NAMES,
    ZDRIVE_RULEBOOK,
)
from testcases.zdrive.teststeps import teststeps
from testcases.zdrive.teststeps.teststeps import (
    FET_WAIT_C,
    TC_HEADROOM_C,
    THERMAL_WAIT_S,
    temperatures_need_a_wait,
    wait_for_thermal_headroom,
)
from testcases.zdrive.testcases.testcases import CycleBrakeHoldTest
from protocol.wire import DEVICE_ODRIVE


class FakeStand:
    """A stand whose three temperatures and travel counter can be posed."""

    def __init__(self, fet=25.0, tcs=None, turns_traveled=0.0):
        self.fet = fet
        self.tcs = {1: 24.0, 2: 24.0} if tcs is None else tcs
        self._turns = turns_traveled

    def get_fet_temperature_c(self):
        return self.fet

    def get_tc_temperatures_c(self):
        return dict(self.tcs)

    def get_channels(self):
        return {"turns_traveled": self._turns}


class ThermalCase:
    """The thermal decision with the run's plumbing left out."""

    test_id = "test-cycle-hold"

    def __init__(self, stand):
        self.testbed = stand
        self.state = {}
        self.waited = []

    def set_state(self, name, value):
        self.state[name] = value

    def wait_for(self, seconds):
        self.waited.append(seconds)
        # Each wait is a chance for the stand to have cooled - a test poses that by
        # mutating the stand between calls, so nothing needs to happen here.

    def check_should_continue(self):
        pass


# --- the thermal decision: one place, three sensors --------------------------


def test_a_cool_stand_is_cleared_to_lift():
    case = ThermalCase(FakeStand(fet=30.0, tcs={1: 25.0, 2: 26.0}))

    assert temperatures_need_a_wait(case) is None
    assert case.state["fet_temperature_c"] == 30.0, "what it saw is recorded either way"


def test_a_hot_fet_stops_the_lift():
    """Below the board's own 83.96 C derate point, so the test backs off before the drive
    starts quietly reducing the current a lift gets."""
    case = ThermalCase(FakeStand(fet=FET_WAIT_C))

    objection = temperatures_need_a_wait(case)

    assert objection is not None and "FET" in objection
    assert FET_WAIT_C < MAX_FET_TEMPERATURE_C, "the wait must come before the fatal bound"


def test_a_thermocouple_near_its_own_fatal_bound_stops_the_lift():
    """Tracked against MAX_TEMPERATURE_C rather than restating it: move the bound and
    this moves with it."""
    case = ThermalCase(FakeStand(tcs={1: 24.0, 2: MAX_TEMPERATURE_C - TC_HEADROOM_C}))

    objection = temperatures_need_a_wait(case)

    assert objection is not None and "thermocouple 2" in objection


def test_the_hottest_thermocouple_is_the_one_reported():
    case = ThermalCase(FakeStand(tcs={1: 68.0, 2: 66.0}))

    assert "thermocouple 1" in temperatures_need_a_wait(case)


def test_a_thermocouple_that_cannot_be_read_is_not_compared_against_a_limit():
    """The DAQ streams eight channels and publishes None for one it cannot read. A
    channel going open is already fatal through its own bound, which is a better place to
    notice it than a flow-control check."""
    stand = FakeStand()
    stand.get_tc_temperatures_c = lambda: {1: 24.0}  # channel 2 absent, not None

    assert temperatures_need_a_wait(ThermalCase(stand)) is None


# --- waiting -----------------------------------------------------------------


def test_a_stand_that_cools_proceeds_and_reports_how_long_it_waited():
    stand = FakeStand(fet=75.0)
    case = ThermalCase(stand)

    def cool_after_two(seconds):
        case.waited.append(seconds)
        if len(case.waited) == 2:
            stand.fet = 40.0

    case.wait_for = cool_after_two

    assert wait_for_thermal_headroom(case) == 2
    assert case.waited == [THERMAL_WAIT_S, THERMAL_WAIT_S]
    assert case.state["thermal_waits"] == 2


def test_the_wait_count_is_published_while_it_is_still_waiting():
    """Unbounded is only survivable if it is visible: a stand that cannot cool produces a
    tenth of the cycles anyone expected and otherwise looks like a healthy run."""
    stand = FakeStand(fet=75.0)
    case = ThermalCase(stand)
    seen = []

    def record_then_cool(seconds):
        seen.append(case.state.get("thermal_waits"))
        if len(seen) == 3:
            stand.fet = 30.0

    case.wait_for = record_then_cool
    wait_for_thermal_headroom(case)

    assert seen == [1, 2, 3], "the count rises as it waits, not only at the end"


def test_a_cool_stand_never_waits():
    case = ThermalCase(FakeStand(fet=30.0))

    assert wait_for_thermal_headroom(case) == 0
    assert case.waited == []


# --- the bounds --------------------------------------------------------------


def test_a_measurable_slip_ends_the_run():
    """A brake-has-let-go trip. Measured slip over 73 holds was one encoder count, so
    nothing short of the brake releasing reaches this."""
    bound = next(b for b in ZDRIVE_RULEBOOK.bounds if b.name == "brake_slip_bound")

    assert bound.fatal
    assert bound.upper == MAX_BRAKE_SLIP_M == 0.010
    assert bound.evaluate({"brake_slip_m": 0.000001}) is False, "one encoder count"
    assert bound.evaluate({"brake_slip_m": 0.009}) is False
    assert bound.evaluate({"brake_slip_m": 0.011}) is True


def test_the_slip_bound_ignores_a_load_that_rose():
    """Slip is signed the way the stroke is - a descending load slips positive. A negative
    reading is noise or the fixture being lifted, not the brake giving way."""
    bound = next(b for b in ZDRIVE_RULEBOOK.bounds if b.name == "brake_slip_bound")

    assert bound.lower is None
    assert bound.evaluate({"brake_slip_m": -0.5}) is False


def test_the_fet_bound_is_fatal_debounced_and_above_the_wait_threshold():
    bound = next(b for b in ZDRIVE_RULEBOOK.bounds if b.name == "fet_overtemperature_bound")

    assert bound.fatal
    assert bound.upper == MAX_FET_TEMPERATURE_C == 80.0
    assert bound.persistence_s, "one thermistor per frame; a single reading must not end a run"
    assert bound.evaluate({"motor_fet_thermistor_temperature": 70.0}) is False
    assert bound.evaluate({"motor_fet_thermistor_temperature": 85.0}) is True


def test_a_fresh_run_can_start_without_tripping_anything():
    """Every seeded value has to satisfy every bound, or the run cannot open."""
    for bound in ZDRIVE_RULEBOOK.bounds:
        assert bound.evaluate(DEFAULT_STATE) is not True, f"{bound.name} fires on a fresh run"


# --- distance ----------------------------------------------------------------


def _case():
    test = CycleBrakeHoldTest(require_engine=False)
    test.testbed = FakeStand()
    return test


def test_nothing_is_derived_until_the_run_knows_where_its_cycling_started():
    """establish_origin_at_bottom has a person hand-move 1000 lb onto its stop, and the
    driver's counter is running throughout. That is not this run's travel."""
    test = _case()

    assert test.derived_channels({DEVICE_ODRIVE: {"turns_traveled": 500.0}}) == {}

    test._distance_at_run_start_m = turns_to_metres(500.0)
    derived = test.derived_channels({DEVICE_ODRIVE: {"turns_traveled": 600.0}})
    assert derived["total_distance_m"] == pytest.approx(100.0 * METERS_PER_TURN)


def test_the_derivation_reads_the_odrive_and_says_so():
    assert CycleBrakeHoldTest.DERIVED_FROM_DEVICES == (DEVICE_ODRIVE,)


def test_a_missing_odrive_frame_derives_nothing_rather_than_raising():
    test = _case()
    test._distance_at_run_start_m = 0.0

    assert test.derived_channels({}) == {}


# --- geometry and wiring -----------------------------------------------------


def test_the_lift_stays_inside_the_stroke():
    """Every target is relative to an origin a person sets by hand, so the margin to the
    stroke limit is only as good as that."""
    assert CycleBrakeHoldTest.TOP_POSITION == -50.0
    assert abs(CycleBrakeHoldTest.TOP_POSITION) < abs(teststeps.TOP_OF_STROKE)


def test_the_cycle_times_are_what_was_asked_for():
    assert CycleBrakeHoldTest.HOLD_S == 2.0
    assert CycleBrakeHoldTest.DWELL_S == 2.0
    assert THERMAL_WAIT_S == 60.0


def test_the_test_is_registered_and_in_the_rulebook():
    assert CycleBrakeHoldTest.TEST_NAME == CYCLE_BRAKE_HOLD_TEST_NAME
    assert CYCLE_BRAKE_HOLD_TEST_NAME in TEST_NAMES
    assert "zdrive.cycle_brake_hold" in REGISTERED_TESTS


# --- the hold schedule -------------------------------------------------------


def _holds_within(test, cycles=30):
    """Which of the first `cycles` cycles hand the load to the brake."""
    return [cycle for cycle in range(1, cycles + 1) if test.hold_is_due(cycle)]


def test_the_brake_takes_the_load_once_every_ten_cycles():
    """The nine between are a lift and a lower under the controller, with the brake
    released throughout - so the drive accumulates duty ten times faster than the brake
    accumulates loaded holds."""
    assert CycleBrakeHoldTest.HOLD_INTERVAL_CYCLES == 10
    assert _holds_within(_case()) == [10, 20, 30]


def test_every_hold_is_earned_by_a_full_interval_of_duty():
    """Holds land on the multiples rather than one cycle in from them, so each measures a
    brake that has just done nine lifts. Nothing before cycle 10 holds, which is also why
    a run ending inside its first ten cycles records no slip."""
    test = _case()

    assert not any(test.hold_is_due(cycle) for cycle in range(1, 10))
    assert test.hold_is_due(10)


def test_an_interval_of_one_holds_on_every_cycle():
    """The schedule is a generalisation rather than a mode: an interval of 1 is the
    every-cycle sequence, with no branch of its own to keep working."""
    test = CycleBrakeHoldTest(require_engine=False, hold_interval_cycles=1)

    assert _holds_within(test, cycles=5) == [1, 2, 3, 4, 5]


def test_the_interval_can_be_overridden_without_editing_the_class():
    """As BrakeEnduranceTest takes a slower trigger speed - a shakedown wanting holds
    sooner should not need a source edit."""
    test = CycleBrakeHoldTest(require_engine=False, hold_interval_cycles=3)

    assert _holds_within(test, cycles=10) == [3, 6, 9]


@pytest.mark.parametrize("interval", [0, -1])
def test_an_interval_below_one_is_refused_before_the_run_starts(interval):
    """A zero would raise on the first modulo instead: partway through a cycle, after the
    operator has answered the prompt and gone, with the load already lifted."""
    with pytest.raises(ValueError):
        CycleBrakeHoldTest(require_engine=False, hold_interval_cycles=interval)


@pytest.mark.parametrize("interval", [True, 10.0, "10"])
def test_an_interval_that_is_not_a_whole_number_of_cycles_is_refused(interval):
    """True is an int to isinstance and would quietly mean an interval of 1 - every cycle
    holding, on a run launched to do the opposite. A float clears the modulo and is then
    recorded through an integer format."""
    with pytest.raises(TypeError):
        CycleBrakeHoldTest(require_engine=False, hold_interval_cycles=interval)


def test_the_next_hold_is_reported_off_the_counter_that_decides_it():
    """What the log claims and what the loop does come from lift_cycles alone. Derived
    from the holds instead, a path that ever declined a due hold would leave every later
    line naming a cycle already past, with nothing disagreeing."""
    test = _case()

    for completed in range(0, 30):
        test.lift_cycles = completed
        owed = test.next_hold_number()
        falls_on = owed * CycleBrakeHoldTest.HOLD_INTERVAL_CYCLES

        assert falls_on > completed, "a hold cannot be owed on a cycle already run"
        assert test.hold_is_due(falls_on), "the cycle it names must be one that holds"
        assert not any(
            test.hold_is_due(c) for c in range(completed + 1, falls_on)
        ), "and must be the FIRST such cycle"


def test_the_cycle_count_is_of_finished_cycles():
    """lift_cycles and total_distance_m are read together, so they have to describe the
    same work: a run that dies on the way up must not report a cycle whose travel the
    distance beside it only partly contains. The schedule therefore asks about the cycle
    ABOUT to run, which is one past the last completed."""
    import inspect

    source = inspect.getsource(CycleBrakeHoldTest.main_execution)

    assert "cycle = self.lift_cycles + 1" in source
    assert source.index("wait_for_thermal_headroom") < source.index(
        "self.lift_cycles = cycle"
    ), "the count advances only once the cycle is back down and read"


def test_the_drive_s_duty_and_the_brake_s_wear_are_separate_counts():
    """Neither recovers the other: the holds do not say how far the drive worked, and
    the cycles do not say how often the brake carried the load."""
    assert "lift_cycles" in DEFAULT_STATE
    assert "loaded_brake_holds" in DEFAULT_STATE


def test_a_stored_run_carries_the_schedule_it_ran_under():
    """A count of holds cannot be compared across runs without it - the same 41 holds is
    41 cycles of duty at an interval of 1 and 410 at an interval of 10."""
    test = _case()
    test.lift_cycles = 410
    test.loaded_brake_holds = 41

    metadata = test.result_metadata()

    assert metadata["hold_interval_cycles"] == 10
    assert metadata["lift_cycles"] == 410
    assert metadata["loaded_brake_holds"] == 41


# --- what the review turned up -----------------------------------------------


def test_the_wait_is_a_named_step_so_a_still_stand_is_not_reported_as_moving():
    """A cycle can sit at the bottom for minutes. Without this, current_step still reads
    move_to and an operator watching the dashboard sees the stand described as moving."""
    stand = FakeStand(fet=30.0)
    case = ThermalCase(stand)

    wait_for_thermal_headroom(case)

    assert case.state["current_step"] == "wait_for_thermal_headroom"


def test_the_wait_count_is_published_once_per_outcome():
    """One write per path. It was written twice per iteration, which published the same
    number to the same channel back to back for no reason."""
    stand = FakeStand(fet=30.0)
    case = ThermalCase(stand)
    writes = []
    real = case.set_state
    case.set_state = lambda name, value: (writes.append(name), real(name, value))[1]

    wait_for_thermal_headroom(case)

    assert writes.count("thermal_waits") == 1


def test_the_distance_baseline_goes_through_the_testbed_not_a_channel_name():
    """Which ODrive channel carries the travel count is the driver's business. A test
    reaching into get_channels() by name is a module knowing a fact about a device it
    does not own."""
    import inspect

    source = inspect.getsource(CycleBrakeHoldTest.main_execution)

    assert "get_distance_travelled_m()" in source
    assert "turns_traveled" not in source


def test_the_dwell_is_measured_rather_than_computed_from_the_wait_count():
    """Each temperature check blocks for a frame from two devices, so DWELL_S plus
    waits x THERMAL_WAIT_S is close and not true - and this channel exists to say what
    actually happened."""
    import inspect

    source = inspect.getsource(CycleBrakeHoldTest.main_execution)

    assert "time.monotonic()" in source
    assert "waits * THERMAL_WAIT_S" not in source


def test_only_loaded_holds_are_counted_and_the_name_says_so():
    """The brake engages at the bottom of every cycle, where the load is already on its
    stop and it holds nothing, and at the top only on the cycles that measure it. Only
    the second is wear."""
    assert "loaded_brake_holds" in DEFAULT_STATE
    assert "brake_holds" not in DEFAULT_STATE
