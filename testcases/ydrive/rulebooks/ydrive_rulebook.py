"""Evaluation Rulebook for ydrive: two fatal safety-net bounds on
board-level DC bus channels, checked regardless of what a given test's
main_execution actually does:

- overcurrent_bound: board_ibus > 30A for 5s continuous (persistence_s
  debounces a brief spike, e.g. during a fast direction reversal).
  NOTE: since the CPX400DP took over this stand's DC bus, this bound can
  no longer fire. The supply cannot source 30 A at any voltage (20 A
  absolute, and only 8.75 A at the 48 V this rail runs at, from its
  420 W envelope), so an overdraw makes the output go *unregulated* and
  the bus voltage sag long before board_ibus reaches 30 A. It is left in
  place because it costs nothing and stays correct if the bus is ever fed
  by something bigger - but the channel that actually reports the limit
  being hit is the supply's own in_power_limit_2, and undervoltage_bound
  below is what now catches the sag. Adding a bound on in_power_limit_2
  is an open action (see AI/Mytest.md); it needs a decision about whether
  hitting the envelope should be fatal or merely recorded.
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

ENDURANCE_CYCLE_TEST_NAME = "endurance_cycle_test"
MANUAL_TEST_NAME = "manual_test"

TEST_NAMES = [ENDURANCE_CYCLE_TEST_NAME, MANUAL_TEST_NAME]

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
