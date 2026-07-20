"""Evaluation Rulebook for CycleDutPositionTest.

Uses the DUT's own channels (position, velocity, current).

There's no bound tracking position against the moving position_target
setpoint today. That would need comparing a channel against another
live channel's value, which Bound doesn't support - deliberately
dropped rather than kept as the only consumer of that capability (see
the module this Rulebook's test case uses for the fuller reasoning).
Revisit with a proper derived-channel/dynamic-reference mechanism if a
real test needs that again.

Bounds are tuned against the mock DUT's simple first-order response
with POSITION_GAIN=0.3/VELOCITY_GAIN=1.0/VELOCITY_INTEGRATOR=0.0 (see
CycleDutPositionTest): a 130-unit step settles in ~15s with no
overshoot, peak velocity ~28, peak current ~19.

- position_overshoot_bound: fatal safety net. Should never trip under
  normal operation (no overshoot expected from these gains).
- current_fatal_bound: fatal safety net, well above the ~19 peak
  observed in normal operation.
- current_transient_bound / velocity_transient_bound: non-fatal.
  Expected to trip briefly on every position change and clear once the
  DUT settles - a real, repeated performance signal, not a fault.

TEST_NAME lives here, not on the TestCase, for the same reason as
previous rulebooks: CycleDutPositionTest needs to import this Rulebook
for its own live evaluation, so this module can't import the test case
back without a circular import.
"""
from __future__ import annotations

from testcases.asimov.rulebook import Bound, Rulebook

TEST_NAME = "cycle_dut_position_test"

CYCLE_DUT_POSITION_RULEBOOK = Rulebook(
    name="cycle_dut_position_rulebook",
    test_name=TEST_NAME,
    bounds=[
        Bound(
            channel="position",
            upper=140.0,
            name="position_overshoot_bound",
            fatal=True,
        ),
        Bound(
            channel="current",
            upper=35.0,
            name="current_fatal_bound",
            fatal=True,
        ),
        Bound(
            channel="current",
            upper=15.0,
            name="current_transient_bound",
            fatal=False,
        ),
        Bound(
            channel="velocity",
            upper=25.0,
            name="velocity_transient_bound",
            fatal=False,
        ),
    ],
)
