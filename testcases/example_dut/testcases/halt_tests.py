"""halt_tests.py currently defines a single test case: CycleDutPositionTest.

CycleDutPositionTest continuously cycles the DUT's commanded position
between 0 and 130, evaluating live performance against
CYCLE_DUT_POSITION_RULEBOOK for the full cycle duration (10 minutes by
default). Closed-loop: each cycle waits for the DUT to actually arrive
within POSITION_TOLERANCE of the target (see teststeps.py) before
dwelling there, rather than assuming arrival after a fixed timeout.

Gains (POSITION_GAIN/VELOCITY_GAIN/VELOCITY_INTEGRATOR) are tuned so a
130-unit step settles smoothly in ~15s with no overshoot. See
../rulebooks/cycle_dut_position_rulebook.py for the reasoning and the
bounds tuned against this specific response.

The actual cycling sequence is the cycle_position step (see
../teststeps/teststeps.py), a plain function, not a method. This class
just supplies its gains/positions/durations and calls it directly from
main_execution.

cycle_duration_s/dwell_s are constructor overrides (falling back to the
class defaults of 600s/30s), purely so a shortened run can be used for
verification without waiting out the full 10 minutes. The class
defaults are the real test's actual parameters.
"""
from __future__ import annotations

from typing import Optional

from .base_example_dut_test import BaseExampleDutTest
from ..rulebooks.cycle_dut_position_rulebook import CYCLE_DUT_POSITION_RULEBOOK, TEST_NAMES
from ..teststeps.teststeps import cycle_position


class CycleDutPositionTest(BaseExampleDutTest):
    """Cycles the DUT's position between 0 and 130 for a fixed duration, evaluating performance live."""

    TEST_NAME = TEST_NAMES[0]
    RULEBOOKS = [CYCLE_DUT_POSITION_RULEBOOK]

    POSITION_LOW = 0.0
    POSITION_HIGH = 130.0
    DWELL_S = 30.0
    CYCLE_DURATION_S = 600.0  # 10 minutes
    POSITION_TOLERANCE = 3.0  # matches the settling margin these gains were tuned against

    POSITION_GAIN = 0.3
    VELOCITY_GAIN = 1.0
    VELOCITY_INTEGRATOR = 0.0

    def __init__(
        self,
        test_id: Optional[str] = None,
        cycle_duration_s: Optional[float] = None,
        dwell_s: Optional[float] = None,
    ):
        super().__init__(test_id)
        self._cycle_duration_s = cycle_duration_s if cycle_duration_s is not None else self.CYCLE_DURATION_S
        self._dwell_s = dwell_s if dwell_s is not None else self.DWELL_S

    def main_execution(self) -> None:
        self.dut.set_gains(
            position_gain=self.POSITION_GAIN,
            velocity_gain=self.VELOCITY_GAIN,
            velocity_integrator=self.VELOCITY_INTEGRATOR,
        )
        cycle_position(
            self,
            high_position=self.POSITION_HIGH,
            low_position=self.POSITION_LOW,
            duration_s=self._cycle_duration_s,
            dwell_s=self._dwell_s,
            position_tolerance=self.POSITION_TOLERANCE,
        )
