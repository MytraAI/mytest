"""teststeps.py currently defines a single test step: cycle_position.

cycle_position cycles the DUT's commanded position between
high_position and low_position for a total of duration_s. Closed-loop:
after commanding a new target, it blocks on test_case.dut.get_position()
until the DUT is actually within position_tolerance of that target
(not just after a fixed timeout), then holds there for dwell_s before
flipping to the other target. position_target is published as
test-case state purely for visibility (e.g. in telemetry/CSV output) -
no Rulebook bound references it today, since Bound has no way to
compare a channel against another live channel's value (see the
Rulebook this test uses for that reasoning).

Not a class/mixin - a plain @step function, called directly from a
test's main_execution() with the test case instance passed in, typed
as BaseExampleDutTest. test_case.dut is additionally bound to a
locally-typed `dut` variable up front, purely so DUT calls read as
dut.foo() instead of test_case.dut.foo(), and so a type checker doesn't
need test_case.dut's Optional-ness re-checked at every call site
(BaseExampleDutTest.dut is Optional, since it's unset until
pre_test_setup runs).

Rulebook evaluation runs independently, in LiveRulebookRunner's own
background thread (started/stopped by BaseExampleDutTest). On a fatal
bound's violation, that thread raises FatalBoundViolation and stops
evaluating (see LiveRulebookRunner.evaluate()) - this step's own loop
is not stopped by that, and keeps running for its full duration
regardless (see the architecture doc's open-decisions note on the
resulting risk).
"""
from __future__ import annotations

from testcases.example_dut.dut.example_dut import ExampleDut
from testcases.example_dut.testcases.base_example_dut_test import BaseExampleDutTest
from testcases.utils import Stopwatch
from testcases.step import step

DEFAULT_POSITION_TOLERANCE = 3.0


@step
def cycle_position(
    test_case: BaseExampleDutTest,
    high_position: float,
    low_position: float,
    duration_s: float,
    dwell_s: float,
    position_tolerance: float = DEFAULT_POSITION_TOLERANCE,
) -> None:
    dut: ExampleDut = test_case.dut
    clock = Stopwatch(duration_s=duration_s)
    current_target = high_position
    dut.set_position(current_target)
    test_case.set_state("position_target", current_target)

    while True:
        # Closed-loop: block on live position readings until we've actually arrived.
        while abs(dut.get_position() - current_target) > position_tolerance:
            if clock.expired:
                return

        # Confirmed arrival - hold here for dwell_s before flipping.
        dwell_clock = Stopwatch(duration_s=dwell_s)
        for _ in dwell_clock:
            if clock.expired:
                return

        current_target = low_position if current_target == high_position else high_position
        dut.set_position(current_target)
        test_case.set_state("position_target", current_target)
