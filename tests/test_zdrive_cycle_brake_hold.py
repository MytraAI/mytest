"""CycleBrakeHoldTest: the brake hold, repeated, with the stand's temperatures deciding
when it may lift again.

Two things this test asserts that no other zdrive test does: that a hold which slipped
measurably ends the run, and that a stand too hot to lift waits at the bottom instead of
lifting anyway. Everything about the second is arithmetic against limits declared
elsewhere, which is the point - a second copy of a limit is a limit that drifts.
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
