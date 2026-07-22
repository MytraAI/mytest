"""Evaluation Rulebook for ydrive: two fatal safety-net bounds on
board-level DC bus channels, checked regardless of what a given test's
main_execution actually does:

- overcurrent_bound: board_ibus > 30A for 5s continuous (persistence_s
  debounces a brief spike, e.g. during a fast direction reversal).
- undervoltage_bound: board_vbus_voltage < 10.5V, no persistence -
  trusted instantaneously rather than debounced.

TEST_NAMES lists every concrete ydrive TestCase.TEST_NAME that starts
a runner against this Rulebook (today, EnduranceCycleTest and
ManualTest) - add a new test's TEST_NAME here when it should be
checked against these same safety bounds too. Lives here rather than
on the TestCase to avoid a circular import (see example_dut's
rulebooks for the same pattern).
"""
from __future__ import annotations

from testcases.asimov.rulebook import Bound, Rulebook

TEST_NAMES = ["endurance_cycle_test", "manual_test"]

YDRIVE_RULEBOOK = Rulebook(
    name="ydrive_rulebook",
    test_names=TEST_NAMES,
    bounds=[
        Bound(
            channel="board_ibus",
            upper=30.0,
            name="overcurrent_bound",
            fatal=True,
            persistence_s=5.0,
        ),
        Bound(
            channel="board_vbus_voltage",
            lower=10.5,
            name="undervoltage_bound",
            fatal=True,
        ),
    ],
)
