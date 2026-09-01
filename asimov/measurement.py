"""take_measurement: the pointwise counterpart to a Rulebook's Bound.

A Bound is ambient. It runs on the runner's own threads, against every
telemetry frame, whether or not the test is paying attention - supervision
the DUT lives under for the whole run.

A measurement is deliberate. A test step reads something at a moment it
chose, hands over the number, and says what the number had to be:

    from asimov.measurement import take_measurement

    take_measurement(test_case, "vbus_after_origin",
                     testbed.get_bus_voltage(),
                     lower=47.04, upper=48.96, units="V")

THE VALUE IS PASSED IN, not a channel name. Test code in this codebase does
not name raw channels - which ODrive channel carries the travel count is the
driver's business, and there is a test enforcing it (see
tests/test_zdrive_cycle_brake_hold.py). The consequence, which is the point:
anything a step can compute is measurable on the same footing as anything it
can read. A stopping distance, a duration, the difference between two
positions and a bus voltage are all just numbers with limits.

Any combination of `upper`, `lower` and `expected` may be given, ANDed
together, exactly as a Bound's are - the comparison is literally the same
code (asimov/limits.py). `expected` carries the discrete half of a functional
test: a control mode, an armed flag, a firmware string.

WHAT IT DOES WITH A FAILURE, and does not: it records it, returns it, and
carries on. The run's measurements_result becomes FAIL, so the verdict says
the run failed - but the sequence is not interrupted, because the rest of a
characterization run is still worth having and the framework does not get to
decide otherwise. A step that must stop says so itself, at the call site,
where the reason is visible:

    m = take_measurement(test_case, "stopping_distance_worst", worst, upper=3.25)
    if not m.passed:
        raise RuntimeError(f"the brake no longer stops the load: {m.value:.2f} m")

WHAT IT REFUSES is a value it cannot judge - a None, a type that will not
order against a numeric limit, or a NaN (every comparison against one is
False, so an unchecked NaN passes every limit it is given). That raises UnmeasurableError on the spot and
ends the run. Deliberately harsher than a Bound, which tolerates the same
condition for unevaluable_grace_s: a bound is fed thousands of frames and one
dropped sample is not a lost sensor, whereas a step handed the framework a
value it had already read, once, and there is no second sample coming.

A NAME IS MEASURED ONCE PER RUN. A second take of the same name raises. That
makes each measurement a named line item - the shape of a test report rather
than a stream - and it is what lets fifty runs be compared by name. Its cost
lands on loops: a value worth measuring every cycle is aggregated by the step
itself and measured once, which is a decision about what the run is claiming
("the worst stop in 800 cycles was 3.44 m") rather than 800 rows nobody reads.

TWO ENTRY POINTS, ONE MEASUREMENT. take_measurement_over_time() samples for a
while and judges one statistic of the window:

    take_measurement_over_time(test_case, "vbus_under_load",
                               lambda: testbed.get_bus_voltage(),
                               seconds=10.0, statistic="min",
                               lower=44.0, units="V")

Everything above still holds of it - one name per run, a failure recorded
rather than raised, the same limits, the same comparison. It takes a callable
only because a duration means reading repeatedly and a number already read
cannot be read again; it still never names a channel. The whole window is
recorded beside the statistic that was judged, because the others were
computed anyway and "the mean passed, but what was the worst of it" is the
question that always follows.

The one rule the two do NOT share is what an unusable reading means, and the
difference is the same one that separates a measurement from a Bound. Handed a
single unjudgeable value, take_measurement has no second sample coming and
stops the run. A window demonstrably does have one, so it skips that reading,
counts it, and records the count - the reasoning behind Bound's
unevaluable_grace_s, applied where it actually holds.
"""
from __future__ import annotations

import logging
import math
import statistics
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from protocol.verdict import Measurement, MeasurementsResult
from testcases.utils import Stopwatch

from .limits import Uncomparable, compare

logger = logging.getLogger(__name__)


class UnmeasurableError(Exception):
    """Raised when a value handed to take_measurement cannot be judged
    against its limits at all - a None, a type that will not order against a
    numeric limit, or a value that is not finite.

    Loud and immediate, not recorded and skipped: a measurement that was not
    judged is not a measurement that passed, and unlike a Bound there is no
    stream of further samples that might recover. It propagates out of
    main_execution, so teardown runs and the lifecycle is ERRORED - which is
    also why MeasurementsResult has no "unmeasurable" state to hide in."""

    def __init__(self, name: str, value: Any, reason: str):
        super().__init__(f"measurement {name} cannot be judged: value={value!r} - {reason}")
        self.name = name
        self.value = value
        self.reason = reason


class MeasurementLog:
    """Every measurement one run has taken, in order, and the run-level
    result over them.

    The measurement counterpart to LiveRulebookRunner: TestCase holds one and
    _author_verdict() reads `measurements` and `result` off it the way it
    reads the runner's summary(). Two differences, both deliberate:

    - It is built in TestCase.__init__ and is never None, unlike self.runner
      which each subclass constructs for itself. A test with no Rulebook at
      all can still measure.
    - It takes no lock. The runner's state is written from a thread per
      telemetry stream and read from the main thread; this is only ever
      touched from the test's own thread, by a step making a call.
    """

    def __init__(self) -> None:
        self._measurements: List[Measurement] = []
        self._by_name: Dict[str, Measurement] = {}
        """The same measurements keyed by name, so a repeated name is caught
        on the take rather than found in the record."""

    def take(self, measurement: Measurement) -> None:
        """Record one measurement, refusing a name already measured this run.

        Raises ValueError on a repeat - see this module's docstring for why a
        name is a once-per-run identity. The message names the earlier value
        so the author can see which of the two calls they meant."""
        earlier = self._by_name.get(measurement.name)
        if earlier is not None:
            raise ValueError(
                f"measurement {measurement.name!r} was already taken this run "
                f"(value={earlier.value!r}); a name identifies one measurement per run, so a "
                "value measured repeatedly should be aggregated by the step and taken once"
            )
        self._by_name[measurement.name] = measurement
        self._measurements.append(measurement)

    @property
    def measurements(self) -> List[Measurement]:
        """Every measurement taken, in order - a copy, so the verdict's list
        cannot be appended to through this."""
        return list(self._measurements)

    @property
    def result(self) -> str:
        """PASS, FAIL or NOT_TAKEN over the whole run - see
        MeasurementsResult. NOT_TAKEN only ever means none were taken; a value
        that could not be judged raised instead of landing here."""
        if not self._measurements:
            return MeasurementsResult.NOT_TAKEN
        if any(not m.passed for m in self._measurements):
            return MeasurementsResult.FAIL
        return MeasurementsResult.PASS


def take_measurement(
    test_case,
    name: str,
    value: Any,
    *,
    upper: Optional[float] = None,
    lower: Optional[float] = None,
    expected: Optional[Any] = None,
    units: str = "",
) -> Measurement:
    """Judge `value` against the limits given, record it on this run, and
    return it. See this module's docstring for the shape and the reasoning.

    `test_case` is deliberately unannotated. Importing TestCase here would be
    a genuine cycle - testcases/base.py imports this module to build its
    MeasurementLog - and the surface used is four attributes wide
    (`measurements`, `state_snapshot()`, `check_should_continue()`, `test_id`),
    which is what lets a unit test pass a stub. Same reason @step takes an
    unannotated `test_case`.

    Raises ValueError if `name` is not a non-empty string, if no limit is set
    at all - a measurement that cannot fail is not a measurement, and the
    likeliest cause is a limit that was meant to be there - or if `name` was
    already measured this run. Raises UnmeasurableError if `value` cannot be
    judged: a None, an unorderable type, or a non-finite number.

    Then calls test_case.check_should_continue(), so a run of measurements
    inside one long step notices a fatal bound, an operator stop or a lost
    recorder between them at the cadence @step already sets at its boundaries.
    DELIBERATELY AFTER RECORDING: the reading was already taken, and a run
    that ends on this poll should still carry the measurement that was made
    before it.
    """
    _require_name(name, "take_measurement(test_case, name, value, ...)")
    _require_limits(name, upper, lower, expected)

    try:
        out_of_limits = compare(value, upper=upper, lower=lower, expected=expected)
    except Uncomparable as exc:
        # The comparison is shared with Bound; what an unjudgeable value means
        # is not. Here it ends the run - see UnmeasurableError.
        raise UnmeasurableError(name, value, exc.reason) from exc

    return _record(
        test_case,
        Measurement(
            name=name,
            value=value,
            passed=not out_of_limits,
            upper=upper,
            lower=lower,
            expected=expected,
            units=units,
            t=time.time(),
            # Whatever @step last published, so a failure says where in the
            # sequence it happened. Read back from the run's own published
            # state rather than tracked separately, so it cannot disagree with
            # the current_step column in the recorded telemetry.
            step=_current_step(test_case),
        ),
    )


STATISTICS: Dict[str, Callable[[List[float]], float]] = {
    "min": min,
    "max": max,
    "mean": statistics.mean,
    "stdev": statistics.stdev,
}
"""What a window may be judged on.

Sample standard deviation rather than population: a window is a sample of an
ongoing process, not the whole of one. At the ~126 readings ten seconds of
ODrive telemetry gives, the two differ by 0.4%, so the choice cannot decide a
verdict - it is made on which question is being asked, not on the number.

One spelling each, deliberately. "average" is the same statistic as "mean" and
having both would put two names for one thing in every stored record.

Not all four apply to every window - see BOOLEAN_STATISTICS."""

BOOLEAN_STATISTICS = ("min", "max")
"""Which statistics a window of flags may be judged on.

Booleans order, so an ordering question over them is well defined and answers
in the same terms it was asked: min is "armed for the whole window", max is
"armed at any point", and each returns True or False for an `expected=` to
judge. Both are real questions about a stand, and neither is expressible as a
point measurement after the fact.

mean and stdev are not ordering questions - they turn flags into a number, and
that is a different measurement rather than a stricter one. A mean over flags
is a duty cycle, which is a legitimate thing to want and not what anyone writes
`statistic="mean"` over an armed flag expecting. An author who does want it
says so at the call site with `lambda: float(flag)`, which makes the
measurement numeric where it is written rather than by inference here."""

MIN_SAMPLES = {"stdev": 2}
"""How many usable readings a statistic needs before it means anything.

Only stdev has one: a spread computed from a single reading is not zero, it is
unknown, and `statistics.stdev` says so by raising. Everything else is
well-defined on one reading - the minimum of one number is that number."""


def take_measurement_over_time(
    test_case,
    name: str,
    read: Callable[[], Any],
    *,
    seconds: float,
    statistic: str,
    upper: Optional[float] = None,
    lower: Optional[float] = None,
    expected: Optional[Any] = None,
    units: str = "",
    interval_s: Optional[float] = None,
) -> Measurement:
    """Sample `read()` for `seconds`, judge one `statistic` of the window
    against the limits given, and record the whole window.

    The same measurement as take_measurement in every respect that matters -
    one name per run, a failure recorded rather than raised, the same limits -
    differing only in where the number comes from. Which is why it takes a
    CALLABLE rather than a value: a duration means reading repeatedly, and a
    number already read cannot be read again. It still never names a channel,
    so `lambda: testbed.get_bus_voltage()` measures a stand exactly as
    `lambda: worst_so_far` would measure a step's own bookkeeping.

    SAMPLED AS FAST AS `read()` RETURNS, which for a telemetry read is the
    device's own frame rate: TelemetryClient.latest_frame() blocks for a frame
    when none is queued and then drains to the newest. So a tight loop over a
    testbed accessor paces itself at the true rate, and CANNOT SEE THE SAME
    FRAME TWICE - the read consumes, so a second call blocks until a second
    frame exists. Every testbed accessor is such a source.

    A source that touches no frame is not: published state, a derived channel,
    a step's own local. Those return instantly, so the same loop spins for the
    whole window and fills it with copies of one value - and that does not fail
    loudly, it leaves the mean right and the standard deviation a confident
    zero. `interval_s` slows such a loop down; `repeats` in the record is what
    actually shows it happened, near zero on a telemetry window and roughly
    equal to `samples` on a window that measured one value over and over.

    What this does NOT guarantee is that no frame was missed. latest_frame()
    drains to the newest and discards what was queued behind it, so a loop
    slower than the device - one whose `read()` waits on two streams, say -
    skips frames, and a minimum can be discarded unseen. Closing that needs a
    transport that stops consuming on read; see AI/Mytest.md's open decisions.

    A WINDOW OF FLAGS may be judged on min or max, and only those. Booleans
    order, so "armed for the whole window" (min) and "armed at any point" (max)
    are well-defined questions that answer True or False for an `expected=` to
    judge - and neither is expressible as a point measurement after the fact. A
    mean or a stdev over flags is not a stricter version of that question, it
    is a different one: a duty cycle. Refused rather than computed, on the
    first usable reading so the mistake costs one frame instead of the whole
    window, and an author who does want a duty cycle writes
    `lambda: float(flag)` - which makes the measurement numeric where it is
    written rather than by inference here. A window that mixes flags and
    numbers has no statistic at all and raises.

    A READING THAT CANNOT BE USED IS SKIPPED, NOT FATAL - a None, or something
    no statistic can be computed from. Deliberately the opposite of
    take_measurement, and for the reason Bound has unevaluable_grace_s: a
    thermocouple once dropped one frame in 6852 and ended a twelve-minute run
    it had supervised perfectly either side. `skipped` is recorded beside
    `samples`, so the tolerance is visible rather than silent. If nothing
    usable remains - or fewer than two for stdev - that is a source which
    answered nothing, and it raises UnmeasurableError.

    check_should_continue() runs on every tick, so the window is not a hole in
    which a fatal bound, an operator stop or a lost recorder goes unnoticed -
    the gap @step's boundary polls leave open in any long step. IF ONE OF THEM
    RAISES, NOTHING IS RECORDED: three seconds of a window specified as ten is
    not the measurement that was asked for, and with only pass and fail
    available there is no honest verdict to give it. Nothing is lost that the
    telemetry CSV is not already recording for that period.

    The same is true of `read()` itself raising - a TelemetryTimeout on a stream
    that went silent, say. It propagates untouched and the window records
    nothing, for the same reason: a source that stopped answering partway
    through did not produce the window that was asked for.

    Raises ValueError for an unusable name, for `seconds` that is not
    positive, for an unknown `statistic`, or if no limit is set.
    """
    _require_name(name, "take_measurement_over_time(test_case, name, read, seconds=..., ...)")
    if statistic not in STATISTICS:
        raise ValueError(
            f"measurement {name!r} asks for statistic {statistic!r}; "
            f"choose one of {', '.join(sorted(STATISTICS))}"
        )
    if not seconds > 0:
        raise ValueError(f"measurement {name!r} needs a positive window, got seconds={seconds!r}")
    if interval_s is not None and interval_s < 0:
        # Checked here rather than left to time.sleep(), which would raise
        # partway through the window - after spending time, with nothing
        # recorded, and with a message that never names the measurement.
        raise ValueError(
            f"measurement {name!r} needs a non-negative interval_s, got {interval_s!r}"
        )
    _require_limits(name, upper, lower, expected)

    samples, skipped, repeats, elapsed, boolean = _sample_window(
        test_case, name, read, statistic, seconds, interval_s
    )

    needed = MIN_SAMPLES.get(statistic, 1)
    if len(samples) < needed:
        raise UnmeasurableError(
            name, None,
            f"{len(samples)} usable reading(s) in {elapsed:.2f}s ({skipped} unusable), and "
            f"{statistic} needs at least {needed}",
        )

    value = STATISTICS[statistic](samples)
    # Only the judged statistic can fail, so this cannot raise Uncomparable:
    # every statistic returns a number, and _sample_window keeps only numbers.
    out_of_limits = compare(value, upper=upper, lower=lower, expected=expected)

    return _record(
        test_case,
        Measurement(
            name=name,
            value=value,
            passed=not out_of_limits,
            upper=upper,
            lower=lower,
            expected=expected,
            units=units,
            t=time.time(),
            step=_current_step(test_case),
            statistic=statistic,
            seconds=elapsed,
            samples=len(samples),
            skipped=skipped,
            repeats=repeats,
            window_min=min(samples),
            window_max=max(samples),
            # None over a window of flags: an average of them is a duty cycle,
            # a different measurement from the one that was taken, and a spread
            # over one reading is unknown rather than zero.
            window_mean=None if boolean else statistics.mean(samples),
            window_stdev=(
                None if boolean or len(samples) < 2 else statistics.stdev(samples)
            ),
        ),
    )


def _sample_window(
    test_case,
    name: str,
    read: Callable[[], Any],
    statistic: str,
    seconds: float,
    interval_s: Optional[float],
) -> Tuple[List[Any], int, int, float, bool]:
    """Read until `seconds` have passed, returning the usable readings, how
    many were not, how many repeated the reading before them, how long it
    actually took, and whether the window turned out to be boolean.

    Polls check_should_continue() on every tick - see
    take_measurement_over_time for why an interruption here records nothing.
    The clock is checked after each reading rather than before, so a window
    always contains at least one: `seconds` smaller than the source's own
    interval gives a one-reading window rather than an empty one, which is a
    strange measurement but an honest one, and `samples` says so.

    A repeat is compared against the last USABLE reading, not the last reading:
    a dropped frame between two identical values does not make the second one
    novel."""
    samples: List[Any] = []
    skipped = 0
    repeats = 0
    boolean: Optional[bool] = None
    clock = Stopwatch(duration_s=seconds)
    while True:
        test_case.check_should_continue()
        value = read()
        # A non-finite reading is unusable for the same reason compare() refuses
        # one, and skippable for the same reason a None is: within a window
        # there is another sample coming. Kept out of the samples list rather
        # than judged later, because one NaN makes every statistic over the
        # window NaN - and statistics.stdev raises on it from inside the
        # stdlib, naming neither the measurement nor the channel.
        if not isinstance(value, (int, float)) or (  # bool included - an int subclass
            isinstance(value, float) and not math.isfinite(value)
        ):
            skipped += 1
        else:
            is_flag = isinstance(value, bool)
            if boolean is None:
                # The first usable reading decides what kind of window this is,
                # and it is the earliest moment the pairing can be checked at
                # all - the framework cannot know a source's type without
                # calling it. Checked here rather than after the window so a
                # mistake costs one reading instead of the whole duration.
                boolean = is_flag
                if is_flag and statistic not in BOOLEAN_STATISTICS:
                    raise ValueError(
                        f"measurement {name!r} asks for {statistic!r} over boolean readings, "
                        f"which is a duty cycle rather than an ordering question; use "
                        f"{' or '.join(BOOLEAN_STATISTICS)}, or read float(flag) if a duty "
                        "cycle is the measurement"
                    )
            elif is_flag != boolean:
                # Not skippable: half a window of flags and half of volts has
                # no statistic, and quietly dropping one kind would answer a
                # question nobody asked.
                raise UnmeasurableError(
                    name, value,
                    "the window mixed boolean and numeric readings, which have no statistic "
                    "in common",
                )
            # Flags stay flags: min over them must answer True or False, so an
            # `expected=` judges a boolean question in boolean terms and the
            # record does not read as a number that happens to be 0.
            value = value if is_flag else float(value)
            if samples and value == samples[-1]:
                repeats += 1
            samples.append(value)
        if clock.expired:
            return samples, skipped, repeats, clock.elapsed_s(), bool(boolean)
        if interval_s is not None:
            time.sleep(interval_s)


def _record(test_case, measurement: Measurement) -> Measurement:
    """Record a judged measurement on the run, log it, then poll.

    Shared by both entry points so the order cannot drift between them: the
    log refuses a duplicate name before anything is logged, and the poll comes
    last so a run that ends on it still carries the measurement."""
    test_case.measurements.take(measurement)

    log_fn = logger.info if measurement.passed else logger.warning
    log_fn(
        "test %s: measurement %s = %s%s [%s] - %s",
        test_case.test_id,
        measurement.name,
        _format_value(measurement.value),
        f" {measurement.units}" if measurement.units else "",
        _describe(measurement),
        "PASS" if measurement.passed else "FAIL",
    )

    test_case.check_should_continue()
    return measurement


def _require_name(name: Any, call_shape: str) -> None:
    """The name is this measurement's identity - in the verdict, and across
    every run that took one by the same name - so an empty or non-string one
    is not a cosmetic problem. Checked rather than assumed because the
    commonest way to get here is passing the value first: the limits would
    then be compared against the name, and the run would die complaining about
    a value nobody wrote."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"a measurement needs a non-empty name, got {name!r} - the call is {call_shape}")


def _require_limits(
    name: str, upper: Optional[float], lower: Optional[float], expected: Optional[Any]
) -> None:
    """A measurement that cannot fail is not a measurement, and the likeliest
    cause is a limit that was meant to be there."""
    if upper is None and lower is None and expected is None:
        raise ValueError(
            f"measurement {name!r} sets no upper, lower or expected value, so nothing could "
            "ever fail it"
        )


def _current_step(test_case) -> Optional[str]:
    """Whatever @step last published, or None outside one."""
    return test_case.state_snapshot().get("current_step")


def _format_value(value: Any) -> str:
    """3 decimals for numbers, plain str() otherwise - a measurement's value
    can be a discrete state as easily as a float. bool first, since it is an
    int subclass and 'True' reads better than '1.000'."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:.3f}"
    return str(value)


def _describe(measurement: Measurement) -> str:
    """The limits as one human-readable phrase for the log line, in the same
    style as Bound.label, prefixed by the window when there was one - so a log
    says whether a number was glanced at or watched."""
    parts = []
    if measurement.lower is not None:
        parts.append(f">={measurement.lower}")
    if measurement.upper is not None:
        parts.append(f"<={measurement.upper}")
    if measurement.expected is not None:
        parts.append(f"=={measurement.expected}")
    limits = " and ".join(parts)
    if measurement.statistic is None:
        return limits
    window = f"{measurement.statistic} of {measurement.samples} over {measurement.seconds:.1f}s"
    if measurement.skipped:
        window += f", {measurement.skipped} unusable"
    if measurement.repeats:
        # Only when there are any: a healthy telemetry window has none, and a
        # count of zero on every line is noise that hides the one that matters.
        window += f", {measurement.repeats} repeated"
    return f"{window}, {limits}"
