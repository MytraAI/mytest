"""take_measurement: the pointwise half of asimov's judgement.

Four things this covers that the design turns on:

- a failed measurement is a *result*, not an interruption - it is recorded,
  returned and the sequence goes on, and only the run's verdict changes;
- a value that cannot be judged is the opposite - loud, immediate, and it
  ends the run, so MeasurementsResult never has to mean "we don't know";
- a name is measured once per run, which is what makes a measurement a named
  line item comparable across runs rather than a stream;
- a measurement and a Bound comparing the same number reach the same answer,
  because they are the same code (asimov/limits.py).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asimov.measurement import (
    MeasurementLog,
    UnmeasurableError,
    take_measurement,
    take_measurement_over_time,
)
from asimov.rulebook import Bound
from protocol.verdict import (
    BoundsResult,
    Lifecycle,
    Measurement,
    MeasurementsResult,
    Verdict,
)


class FakeTestCase:
    """The surface take_measurement actually touches: an id for logging, a
    log to record into, the published state it reads current_step from, and
    the poll it makes afterwards."""

    def __init__(self, current_step=None, stop_with=None):
        self.test_id = "t-1"
        self.measurements = MeasurementLog()
        self._current_step = current_step
        self._stop_with = stop_with
        self.continues = 0

    def state_snapshot(self):
        return {"current_step": self._current_step} if self._current_step else {}

    def check_should_continue(self):
        self.continues += 1
        if self._stop_with is not None:
            raise self._stop_with


# --- judging ------------------------------------------------------------------


def test_an_upper_limit_alone():
    case = FakeTestCase()
    assert take_measurement(case, "a", 2.0, upper=3.0).passed is True
    assert take_measurement(case, "b", 4.0, upper=3.0).passed is False


def test_a_lower_limit_alone():
    case = FakeTestCase()
    assert take_measurement(case, "a", 2.0, lower=1.0).passed is True
    assert take_measurement(case, "b", 0.5, lower=1.0).passed is False


def test_limits_that_are_set_are_anded():
    """Same rule as a Bound's: satisfied only if every limit set holds."""
    case = FakeTestCase()
    assert take_measurement(case, "in", 47.9, lower=47.0, upper=49.0).passed is True
    assert take_measurement(case, "under", 46.9, lower=47.0, upper=49.0).passed is False
    assert take_measurement(case, "over", 49.1, lower=47.0, upper=49.0).passed is False


def test_a_boundary_value_satisfies_its_limit():
    """Violated is strictly outside, so a value sitting exactly on a limit
    passes - a bound written at the number a stand is specified to reach
    should not fail the run that reaches it."""
    case = FakeTestCase()
    assert take_measurement(case, "on_upper", 3.0, upper=3.0).passed is True
    assert take_measurement(case, "on_lower", 1.0, lower=1.0).passed is True


def test_expected_carries_the_discrete_half_of_a_test():
    case = FakeTestCase()
    assert take_measurement(case, "mode", "POSITION_CONTROL",
                            expected="POSITION_CONTROL").passed is True
    assert take_measurement(case, "other", "VELOCITY_CONTROL",
                            expected="POSITION_CONTROL").passed is False


def test_a_bool_is_measurable_against_expected():
    """An armed flag is as much a measurement as a voltage, and False must be
    a real expectation rather than read as 'no limit set'."""
    case = FakeTestCase()
    assert take_measurement(case, "armed", True, expected=True).passed is True
    assert take_measurement(case, "idle", False, expected=False).passed is True
    assert take_measurement(case, "wrong", False, expected=True).passed is False


def test_a_measurement_that_could_not_fail_is_refused():
    """The likeliest cause is a limit that was meant to be there."""
    case = FakeTestCase()
    with pytest.raises(ValueError, match="no upper, lower or expected"):
        take_measurement(case, "nothing", 1.0)


# --- a value that cannot be judged --------------------------------------------


def test_none_against_a_numeric_limit_stops_the_run():
    """Deliberately harsher than a Bound, which tolerates the same condition
    for unevaluable_grace_s: a step has no second sample coming."""
    case = FakeTestCase()
    with pytest.raises(UnmeasurableError) as exc:
        take_measurement(case, "vbus", None, lower=47.0)
    assert "vbus" in str(exc.value)
    assert "no value" in str(exc.value)


def test_an_uncomparable_type_against_a_numeric_limit_stops_the_run():
    case = FakeTestCase()
    with pytest.raises(UnmeasurableError):
        take_measurement(case, "vbus", "not a number", upper=49.0)


def test_an_unjudgeable_value_is_not_recorded_as_anything():
    """It must not land as a pass, and there is no third state for it to land
    as - which is why MeasurementsResult.NOT_TAKEN can only ever mean zero."""
    case = FakeTestCase()
    with pytest.raises(UnmeasurableError):
        take_measurement(case, "vbus", None, lower=47.0)
    assert case.measurements.measurements == []
    assert case.measurements.result == MeasurementsResult.NOT_TAKEN


def test_expected_alone_accepts_any_type_including_none():
    """An equality check needs no ordering, so nothing here is unjudgeable -
    the same carve-out Bound makes."""
    case = FakeTestCase()
    assert take_measurement(case, "absent", None, expected="IDLE").passed is False


# --- a failure is a result, not an interruption -------------------------------


def test_a_failure_does_not_raise_and_is_returned():
    case = FakeTestCase()
    m = take_measurement(case, "stopping_distance", 3.44, upper=3.25, units="m")
    assert m.passed is False
    assert m.value == 3.44
    assert case.measurements.result == MeasurementsResult.FAIL


def test_a_step_can_stop_the_run_itself_at_the_call_site():
    """The sanctioned way to make a measurement fatal - no flag on the call,
    the reason visible where the decision is made."""
    case = FakeTestCase()

    with pytest.raises(RuntimeError, match="3.44"):
        m = take_measurement(case, "stopping_distance", 3.44, upper=3.25, units="m")
        if not m.passed:
            raise RuntimeError(f"the brake no longer stops the load: {m.value:.2f} m")


# --- a name identifies one measurement per run --------------------------------


def test_a_repeated_name_is_refused():
    case = FakeTestCase()
    take_measurement(case, "vbus", 47.9, lower=47.0)
    with pytest.raises(ValueError, match="already taken"):
        take_measurement(case, "vbus", 48.1, lower=47.0)


def test_a_repeated_name_is_refused_even_with_identical_limits():
    """The rule is one take per name, not one set of limits per name - which
    is what pushes a value measured every cycle into being aggregated by the
    step and measured once."""
    case = FakeTestCase()
    take_measurement(case, "d", 2.9, upper=3.25)
    with pytest.raises(ValueError):
        take_measurement(case, "d", 2.9, upper=3.25)


def test_the_refusal_names_the_earlier_value():
    case = FakeTestCase()
    take_measurement(case, "vbus", 47.9, lower=47.0)
    with pytest.raises(ValueError, match="47.9"):
        take_measurement(case, "vbus", 48.1, lower=47.0)


# --- what lands in the record -------------------------------------------------


def test_a_pass_is_recorded_in_full_not_tallied():
    """The value of a passing measurement is most of the point: it is what
    lets fifty runs show a DUT drifting toward a limit it has not crossed."""
    case = FakeTestCase(current_step="prepare_for_operation")
    take_measurement(case, "vbus_after_origin", 47.9, lower=47.04, upper=48.96, units="V")

    (m,) = case.measurements.measurements
    assert (m.name, m.value, m.passed) == ("vbus_after_origin", 47.9, True)
    assert (m.lower, m.upper, m.units) == (47.04, 48.96, "V")
    assert m.step == "prepare_for_operation"
    assert m.t > 0


def test_a_measurement_outside_a_step_has_no_step():
    case = FakeTestCase()
    take_measurement(case, "vbus", 47.9, lower=47.0)
    assert case.measurements.measurements[0].step is None


def test_measurements_keep_the_order_they_were_taken_in():
    case = FakeTestCase()
    for name in ("a", "b", "c"):
        take_measurement(case, name, 1.0, upper=2.0)
    assert [m.name for m in case.measurements.measurements] == ["a", "b", "c"]


def test_the_log_hands_out_a_copy():
    """So appending to what the verdict was given cannot alter the run's own
    record."""
    case = FakeTestCase()
    take_measurement(case, "a", 1.0, upper=2.0)
    case.measurements.measurements.append("junk")
    assert len(case.measurements.measurements) == 1


# --- the run-level result -----------------------------------------------------


def test_no_measurements_is_not_taken():
    assert MeasurementLog().result == MeasurementsResult.NOT_TAKEN


def test_all_passing_is_pass():
    case = FakeTestCase()
    take_measurement(case, "a", 1.0, upper=2.0)
    take_measurement(case, "b", 1.0, upper=2.0)
    assert case.measurements.result == MeasurementsResult.PASS


def test_one_failure_among_many_is_fail():
    case = FakeTestCase()
    take_measurement(case, "a", 1.0, upper=2.0)
    take_measurement(case, "b", 9.0, upper=2.0)
    take_measurement(case, "c", 1.0, upper=2.0)
    assert case.measurements.result == MeasurementsResult.FAIL


# --- polling ------------------------------------------------------------------


def test_the_run_is_polled_after_each_measurement():
    case = FakeTestCase()
    take_measurement(case, "a", 1.0, upper=2.0)
    assert case.continues == 1


def test_a_measurement_survives_the_poll_that_ends_the_run():
    """Recorded before the poll, deliberately: the reading was already taken,
    and a run ending on this tick should still carry it."""
    case = FakeTestCase(stop_with=KeyboardInterrupt("stop requested"))
    with pytest.raises(KeyboardInterrupt):
        take_measurement(case, "vbus", 47.9, lower=47.0)
    assert [m.name for m in case.measurements.measurements] == ["vbus"]


def test_an_unjudgeable_value_never_reaches_the_poll():
    case = FakeTestCase()
    with pytest.raises(UnmeasurableError):
        take_measurement(case, "vbus", None, lower=47.0)
    assert case.continues == 0


# --- one comparison, two mechanisms -------------------------------------------


@pytest.mark.parametrize("value", [-1.0, 0.0, 46.9, 47.0, 47.9, 49.0, 49.1, 100.0])
def test_a_measurement_and_a_bound_agree_on_the_same_number(value):
    """The reason compare() was extracted rather than copied: a stand's spot
    check and its continuous supervision must not be able to disagree about
    whether a reading is in limits."""
    case = FakeTestCase()
    bound = Bound(name="vbus", channel="vbus", lower=47.0, upper=49.0)

    measured = take_measurement(case, "vbus", value, lower=47.0, upper=49.0)
    assert measured.passed is not bound.evaluate({"vbus": value})


# --- the verdict --------------------------------------------------------------


def _verdict(**kwargs) -> Verdict:
    base = dict(
        test_id="t-1", test_name="demo", lifecycle=Lifecycle.COMPLETED,
        bounds_result=BoundsResult.PASS, started_at=0.0, ended_at=1.0,
    )
    base.update(kwargs)
    return Verdict(**base)


def test_failed_measurements_is_derived_from_the_list():
    verdict = _verdict(measurements=[
        Measurement(name="a", value=1.0, passed=True, upper=2.0),
        Measurement(name="b", value=9.0, passed=False, upper=2.0),
    ])
    assert [m.name for m in verdict.failed_measurements()] == ["b"]


def test_a_failed_measurement_shows_in_the_outcome():
    """The false pass this exists to prevent: bounds clean, run completed,
    and a measurement out of spec."""
    verdict = _verdict(measurements_result=MeasurementsResult.FAIL)
    assert verdict.outcome == "COMPLETED/FAIL"


def test_a_run_that_measured_nothing_reads_as_it_did_before():
    """NOT_TAKEN is deliberately unranked: it only ever means this run took
    none, which for a test that defines none is not a defect - and every test
    in this repo is one today."""
    verdict = _verdict(measurements_result=MeasurementsResult.NOT_TAKEN)
    assert verdict.outcome == "COMPLETED/PASS"


def test_unevaluated_bounds_still_win_over_passing_measurements():
    """Unlike NOT_TAKEN, NOT_EVALUATED means supervision that should have run
    never did, so it must not be hidden by a measurement that passed."""
    verdict = _verdict(
        bounds_result=BoundsResult.NOT_EVALUATED,
        measurements_result=MeasurementsResult.PASS,
    )
    assert verdict.outcome == "COMPLETED/NOT_EVALUATED"


def test_a_failure_beats_an_unevaluated_bound():
    verdict = _verdict(
        bounds_result=BoundsResult.NOT_EVALUATED,
        measurements_result=MeasurementsResult.FAIL,
    )
    assert verdict.outcome == "COMPLETED/FAIL"


def test_a_violated_bound_still_shows_with_measurements_passing():
    verdict = _verdict(
        bounds_result=BoundsResult.FAIL,
        measurements_result=MeasurementsResult.PASS,
    )
    assert verdict.outcome == "COMPLETED/FAIL"


def test_everything_clean_is_a_pass():
    verdict = _verdict(measurements_result=MeasurementsResult.PASS)
    assert verdict.outcome == "COMPLETED/PASS"


def test_a_run_with_neither_bounds_nor_measurements():
    verdict = _verdict(
        bounds_result=BoundsResult.NOT_EVALUATED,
        measurements_result=MeasurementsResult.NOT_TAKEN,
    )
    assert verdict.outcome == "COMPLETED/NOT_EVALUATED"


def test_measurements_survive_a_round_trip():
    verdict = _verdict(
        measurements_result=MeasurementsResult.FAIL,
        measurements=[Measurement(name="d", value=3.44, passed=False, upper=3.25,
                                  units="m", t=12.0, step="brake_from_speed")],
    )
    restored = Verdict.from_dict(verdict.to_dict())
    assert restored.measurements == verdict.measurements
    assert restored.measurements_result == MeasurementsResult.FAIL


def test_a_verdict_written_before_measurements_existed_still_reads():
    """Every stored run predates this field, and none of them took any."""
    old = {
        "test_id": "t-0", "test_name": "demo", "lifecycle": "COMPLETED",
        "bounds_result": "PASS", "started_at": 0.0, "ended_at": 1.0,
    }
    restored = Verdict.from_dict(old)
    assert restored.measurements == []
    assert restored.measurements_result == MeasurementsResult.NOT_TAKEN
    assert restored.outcome == "COMPLETED/PASS"


# --- the name is the record's key ---------------------------------------------


def test_an_empty_name_is_refused():
    """It identifies the measurement in this verdict and across every run that
    took one by the same name."""
    case = FakeTestCase()
    with pytest.raises(ValueError, match="non-empty name"):
        take_measurement(case, "", 1.0, upper=2.0)
    with pytest.raises(ValueError, match="non-empty name"):
        take_measurement(case, "   ", 1.0, upper=2.0)


def test_swapping_the_name_and_the_value_says_so():
    """The commonest way to get this wrong. Without the check the limits are
    compared against the name and the run dies complaining about a value
    nobody wrote."""
    case = FakeTestCase()
    with pytest.raises(ValueError, match="take_measurement\\(test_case, name, value"):
        take_measurement(case, 47.9, "vbus", lower=47.0)


def test_a_recorded_measurement_cannot_be_edited_through_the_list():
    """MeasurementLog hands out a shallow copy, so the entries themselves have
    to be what refuses the write."""
    import dataclasses

    case = FakeTestCase()
    take_measurement(case, "vbus", 47.9, lower=47.0)
    (m,) = case.measurements.measurements
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.passed = False


# --- take_measurement_over_time -----------------------------------------------
#
# The window is driven by a fake source with a scripted sequence of readings,
# and `seconds` is kept tiny so the tests run at Stopwatch's own tick rather
# than at any device's rate. What is under test is the arithmetic, the
# skipping, and what reaches the record - never the wall clock.


class Source:
    """A callable returning a scripted sequence, then repeating its last
    value - so a window can run past the script without ending it.

    `blocks_for` makes each read take that long, which is what a real
    telemetry read does: TelemetryClient.latest_frame() waits for a frame. It
    is the only way to pin down how many readings a window contains, since an
    instant source is fast enough to get several into even a microsecond."""

    def __init__(self, *values, blocks_for=0.0):
        self._values = list(values)
        self._blocks_for = blocks_for
        self.reads = 0

    def __call__(self):
        if self._blocks_for:
            time.sleep(self._blocks_for)
        value = self._values[min(self.reads, len(self._values) - 1)]
        self.reads += 1
        return value


def _window(case, name, source, statistic, seconds=0.02, **limits):
    return take_measurement_over_time(
        case, name, source, seconds=seconds, statistic=statistic, **limits
    )


def test_each_statistic_judges_the_window():
    case = FakeTestCase()
    readings = (1.0, 5.0, 9.0)

    assert _window(case, "a", Source(*readings), "min", upper=2.0).value == 1.0
    assert _window(case, "b", Source(*readings), "max", upper=99.0).value == 9.0
    assert _window(case, "c", Source(1.0, 3.0), "mean", upper=99.0).value == pytest.approx(
        2.0, abs=1.5
    )


def test_an_unknown_statistic_is_refused_before_anything_is_read():
    case = FakeTestCase()
    source = Source(1.0)
    with pytest.raises(ValueError, match="max, mean, min, stdev"):
        _window(case, "a", source, "average", upper=2.0)
    assert source.reads == 0


def test_a_window_must_have_a_positive_length():
    case = FakeTestCase()
    with pytest.raises(ValueError, match="positive window"):
        take_measurement_over_time(
            case, "a", Source(1.0), seconds=0.0, statistic="min", upper=2.0
        )


def test_a_window_needs_a_limit_like_any_other_measurement():
    case = FakeTestCase()
    with pytest.raises(ValueError, match="no upper, lower or expected"):
        _window(case, "a", Source(1.0), "min")


def test_a_window_shorter_than_one_reading_still_takes_one():
    """The clock is checked after a reading rather than before, so a window is
    never empty - strange, but honest, and `samples` says so."""
    case = FakeTestCase()
    m = _window(case, "a", Source(7.0, blocks_for=0.02), "min",
                seconds=0.001, upper=99.0)
    assert m.samples == 1
    assert m.value == 7.0
    assert m.seconds > 0.001  # one reading outlasted the whole window


# --- unusable readings are skipped, not fatal ---------------------------------


def test_a_dropped_reading_is_skipped_and_counted():
    """The opposite of take_measurement's rule, and for Bound's reason: one
    dropped frame in a window is not a lost sensor."""
    case = FakeTestCase()
    m = _window(case, "a", Source(4.0, None, 6.0), "max", upper=99.0)
    assert m.skipped >= 1
    assert m.samples >= 2
    assert m.value == 6.0


def test_a_non_numeric_reading_is_skipped_too():
    """Nothing can be averaged from a string."""
    case = FakeTestCase()
    m = _window(case, "a", Source(4.0, "FAULT", 6.0), "max", upper=99.0)
    assert m.skipped >= 1


def test_a_window_of_flags_can_be_asked_an_ordering_question():
    """Booleans order, so min is "armed for the whole window" and max is
    "armed at any point" - both real questions about a stand, and neither
    expressible as a point measurement after the fact."""
    case = FakeTestCase()

    throughout = _window(case, "armed_throughout", Source(True, True, False),
                         "min", expected=True)
    assert throughout.value is False
    assert throughout.passed is False

    ever = _window(case, "armed_at_any_point", Source(False, False, True),
                   "max", expected=True)
    assert ever.value is True
    assert ever.passed is True


def test_a_flag_stays_a_flag_in_the_record():
    """min over flags must answer True or False, not 1.0 - otherwise an
    expected= judges a boolean question by numeric coincidence and the stored
    record reads as a number that happens to be zero."""
    case = FakeTestCase()
    m = _window(case, "a", Source(True), "min", seconds=0.01, expected=True)
    assert m.value is True
    assert m.window_min is True and m.window_max is True


def test_an_average_of_flags_is_refused_as_a_different_measurement():
    """A mean over flags is a duty cycle - legitimate to want, and not what
    anyone writes statistic="mean" over an armed flag expecting."""
    case = FakeTestCase()
    with pytest.raises(ValueError, match="duty cycle"):
        _window(case, "a", Source(True), "mean", upper=99.0)
    with pytest.raises(ValueError, match="duty cycle"):
        _window(case, "b", Source(True), "stdev", upper=99.0)


def test_the_refusal_costs_one_reading_not_the_whole_window():
    """The framework cannot know a source's type without calling it, so the
    check lands on the first usable reading rather than after the window."""
    case = FakeTestCase()
    source = Source(True, blocks_for=0.005)
    with pytest.raises(ValueError, match="duty cycle"):
        _window(case, "a", source, "mean", seconds=30.0, upper=99.0)
    assert source.reads == 1


def test_a_duty_cycle_is_available_by_saying_so_at_the_call_site():
    """The sanctioned way to ask the refused question - numeric where it is
    written, rather than by inference inside the framework."""
    case = FakeTestCase()
    flags = Source(True, True, False, True)
    m = _window(case, "armed_duty_cycle", lambda: float(flags()),
                "mean", seconds=0.01, lower=0.5)
    assert isinstance(m.value, float)
    assert m.passed is True


def test_a_window_that_mixes_flags_and_numbers_is_refused():
    """Half a window of flags and half of volts has no statistic, and quietly
    dropping one kind would answer a question nobody asked."""
    case = FakeTestCase()
    with pytest.raises(UnmeasurableError, match="mixed boolean and numeric"):
        _window(case, "a", Source(True, 47.9, blocks_for=0.004),
                "min", seconds=0.03, expected=True)


def test_a_boolean_window_has_no_mean_or_spread():
    case = FakeTestCase()
    m = _window(case, "a", Source(True), "min", seconds=0.01, expected=True)
    assert m.window_mean is None
    assert m.window_stdev is None


def test_a_source_that_never_answers_is_fatal():
    case = FakeTestCase()
    with pytest.raises(UnmeasurableError, match="usable reading"):
        _window(case, "a", Source(None), "min", upper=99.0)


def test_stdev_needs_two_readings():
    """A spread over one reading is unknown, not zero."""
    case = FakeTestCase()
    with pytest.raises(UnmeasurableError, match="at least 2"):
        _window(case, "a", Source(4.0, blocks_for=0.02), "stdev",
                seconds=0.001, upper=99.0)


def test_nothing_is_recorded_when_the_window_cannot_be_judged():
    case = FakeTestCase()
    with pytest.raises(UnmeasurableError):
        _window(case, "a", Source(None), "min", upper=99.0)
    assert case.measurements.measurements == []


# --- what the window puts in the record ---------------------------------------


def test_the_whole_window_is_recorded_beside_the_judged_statistic():
    """'The mean passed, but what was the worst of it' is the question that
    always follows, and the other statistics were computed anyway."""
    case = FakeTestCase(current_step="brake_from_speed")
    m = _window(case, "vbus_under_load", Source(44.0, 46.0, 48.0), "min",
                lower=40.0, units="V")

    assert m.statistic == "min"
    assert m.value == m.window_min == 44.0
    assert m.window_max >= 46.0
    assert m.window_mean is not None
    assert m.window_stdev is not None
    assert m.seconds > 0
    assert m.samples >= 3
    assert m.skipped == 0
    assert (m.lower, m.units, m.step) == (40.0, "V", "brake_from_speed")
    assert m.passed is True


def test_the_recorded_seconds_are_what_elapsed_not_what_was_asked_for():
    case = FakeTestCase()
    m = _window(case, "a", Source(1.0), "min", seconds=0.02, upper=99.0)
    assert m.seconds >= 0.02
    assert m.seconds != 0.02  # the loop overshoots by up to one reading


def test_a_one_reading_window_has_no_spread():
    case = FakeTestCase()
    m = _window(case, "a", Source(3.0, blocks_for=0.02), "min",
                seconds=0.001, upper=99.0)
    assert m.samples == 1
    assert m.window_stdev is None
    assert m.window_min == m.window_max == m.window_mean == 3.0


def test_a_point_measurement_carries_no_window_fields():
    """The field that keeps a glance and a ten-second watch from reading
    alike."""
    case = FakeTestCase()
    take_measurement(case, "a", 1.0, upper=2.0)
    (m,) = case.measurements.measurements
    assert m.statistic is None
    assert (m.seconds, m.samples, m.skipped) == (None, None, None)
    assert (m.window_min, m.window_max, m.window_mean, m.window_stdev) == (None,) * 4


# --- a window is still a measurement ------------------------------------------


def test_a_failing_window_does_not_raise_and_fails_the_run():
    case = FakeTestCase()
    m = _window(case, "a", Source(9.0), "max", upper=2.0)
    assert m.passed is False
    assert case.measurements.result == MeasurementsResult.FAIL


def test_a_window_shares_the_name_rule_with_a_point_measurement():
    """One log, one namespace - a name taken by either is taken."""
    case = FakeTestCase()
    take_measurement(case, "vbus", 47.9, lower=47.0)
    with pytest.raises(ValueError, match="already taken"):
        _window(case, "vbus", Source(47.9), "mean", lower=47.0)


def test_a_window_refuses_an_unusable_name_before_reading_anything():
    case = FakeTestCase()
    source = Source(1.0)
    with pytest.raises(ValueError, match="non-empty name"):
        _window(case, "", source, "min", upper=2.0)
    assert source.reads == 0


# --- the window is not a hole in the run's supervision -------------------------


def test_the_run_is_polled_throughout_the_window():
    """The gap @step's boundary polls leave open in any long step."""
    case = FakeTestCase()
    m = _window(case, "a", Source(1.0), "min", seconds=0.05, upper=99.0)
    assert case.continues > m.samples  # every tick, plus the one after recording


def test_an_interrupted_window_records_nothing():
    """Three seconds of a window specified as ten is not the measurement that
    was asked for, and there is no third state to file it under."""
    case = FakeTestCase(stop_with=KeyboardInterrupt("stop requested"))
    with pytest.raises(KeyboardInterrupt):
        _window(case, "a", Source(1.0), "min", seconds=10.0, upper=99.0)
    assert case.measurements.measurements == []
    assert case.measurements.result == MeasurementsResult.NOT_TAKEN


def test_an_interval_paces_a_source_that_does_not_block():
    """Without it, a callable reading cached state spins for the whole window
    and fills it with duplicates - a right mean and a confident stdev of 0."""
    case = FakeTestCase()
    unpaced = _window(case, "a", Source(1.0), "min", seconds=0.05, upper=99.0)
    paced = take_measurement_over_time(
        case, "b", Source(1.0), seconds=0.05, statistic="min",
        upper=99.0, interval_s=0.01,
    )
    assert paced.samples < unpaced.samples


# --- repeats: the record says when a window measured one value over and over --


def test_a_telemetry_shaped_source_shows_no_repeats():
    """A source that consumes a frame per call cannot repeat, because the
    second call blocks until a second frame exists. Every testbed accessor is
    one."""
    case = FakeTestCase()
    m = _window(case, "a", Source(1.0, 2.0, 3.0, 4.0, blocks_for=0.005),
                "max", seconds=0.02, upper=99.0)
    assert m.repeats == 0


def test_a_source_that_never_changes_is_visible_as_repeats():
    """The whole point: the mean comes out right and the stdev comes out a
    confident zero, so only this says the window measured one value."""
    case = FakeTestCase()
    m = _window(case, "a", Source(5.0), "mean", seconds=0.02, upper=99.0)
    assert m.repeats == m.samples - 1
    assert m.window_stdev == 0.0


def test_a_dropped_reading_between_two_equal_ones_is_still_a_repeat():
    """Compared against the last usable reading, not the last reading - a hole
    does not make the value after it novel."""
    case = FakeTestCase()
    m = _window(case, "a", Source(5.0, None, 5.0, blocks_for=0.004),
                "min", seconds=0.03, upper=99.0)
    assert m.skipped == 1
    assert m.samples >= 3
    # Every usable reading after the first is 5.0 again, including the one
    # straight after the hole - so none of them is counted as novel.
    assert m.repeats == m.samples - 1


def test_a_point_measurement_has_no_repeat_count():
    case = FakeTestCase()
    take_measurement(case, "a", 1.0, upper=2.0)
    assert case.measurements.measurements[0].repeats is None


def test_repeats_reach_the_verdict():
    from protocol.verdict import Verdict as _V
    case = FakeTestCase()
    _window(case, "a", Source(5.0), "mean", seconds=0.02, upper=99.0)
    verdict = _verdict(measurements=case.measurements.measurements)
    restored = _V.from_dict(verdict.to_dict())
    assert restored.measurements[0].repeats == case.measurements.measurements[0].repeats


def test_a_negative_interval_is_refused_before_the_window_runs():
    """Left to time.sleep() this raises partway through - after spending time,
    with nothing recorded, and with a message that never names the
    measurement."""
    case = FakeTestCase()
    source = Source(1.0)
    with pytest.raises(ValueError, match="non-negative interval_s"):
        take_measurement_over_time(case, "a", source, seconds=0.01,
                                   statistic="min", upper=9.0, interval_s=-1.0)
    assert source.reads == 0


def test_a_source_that_raises_mid_window_records_nothing():
    """Same rule as an interrupted window: a source that stopped answering
    partway through did not produce the window that was asked for."""
    case = FakeTestCase()

    def dies():
        if dies.reads >= 2:
            raise RuntimeError("stream died")
        dies.reads += 1
        return 1.0
    dies.reads = 0

    with pytest.raises(RuntimeError, match="stream died"):
        take_measurement_over_time(case, "a", dies, seconds=10.0,
                                   statistic="min", upper=9.0)
    assert case.measurements.measurements == []


# --- a NaN is not a value that passed -----------------------------------------
#
# Every comparison against a NaN is False, so without a check for it a NaN
# satisfies every limit it is given. Not hypothetical on this hardware:
# zdrive's pos_estimate reads NaN with every other channel looking healthy,
# which is why the testbed has _require_finite_position at all.


def test_a_nan_cannot_be_judged_against_a_numeric_limit():
    case = FakeTestCase()
    with pytest.raises(UnmeasurableError, match="finite"):
        take_measurement(case, "pos", float("nan"), upper=3.0, lower=1.0)
    assert case.measurements.measurements == []


def test_an_infinity_cannot_either():
    """It compares, but a limit is a question about a real quantity."""
    case = FakeTestCase()
    with pytest.raises(UnmeasurableError, match="finite"):
        take_measurement(case, "pos", float("inf"), upper=3.0)


def test_a_bound_and_a_measurement_agree_that_a_nan_is_unjudgeable():
    """The shared comparison means fixing one fixes both - a NaN on a bounded
    channel used to report a clean PASS while supervising nothing."""
    from asimov.rulebook import Bound, UnevaluableBoundError

    with pytest.raises(UnevaluableBoundError):
        Bound(name="uv", channel="vbus", lower=10.5).evaluate({"vbus": float("nan")})
    with pytest.raises(UnmeasurableError):
        take_measurement(FakeTestCase(), "vbus", float("nan"), lower=10.5)


def test_a_nan_is_still_judged_by_expected_alone():
    """An equality check needs no ordering, so it never raises - and a NaN is
    not equal to anything, including itself."""
    case = FakeTestCase()
    assert take_measurement(case, "a", float("nan"), expected=1.0).passed is False


def test_a_nan_in_a_window_is_skipped_like_a_dropped_reading():
    """Within a window there is another sample coming, so it is counted rather
    than fatal - and kept out of the samples, because one NaN makes every
    statistic over the window NaN."""
    case = FakeTestCase()
    m = _window(case, "a", Source(1.0, float("nan"), 2.0, blocks_for=0.004),
                "min", seconds=0.03, lower=0.5)
    assert m.skipped >= 1
    assert m.value == 1.0
    assert m.window_stdev is not None  # statistics.stdev would have raised on a NaN


def test_a_window_of_nothing_but_nan_is_fatal():
    case = FakeTestCase()
    with pytest.raises(UnmeasurableError, match="usable reading"):
        _window(case, "a", Source(float("nan")), "min", lower=0.5)


def test_a_flag_is_still_comparable():
    """math.isfinite(True) is True, so the bool carve-out compare() makes
    deliberately is untouched by the finite check."""
    case = FakeTestCase()
    assert take_measurement(case, "armed", True, expected=True).passed is True
