"""Concrete xdeploy test cases: ManualTest."""
from __future__ import annotations

from testcases.teststeps.operator import await_operator

from ..rulebooks.xdeploy_rulebook import MANUAL_TEST_NAME, MIN_BUS_VOLTAGE_V
from .base_xdeploy_test import BaseXdeployTest


class ManualTest(BaseXdeployTest):
    """No test sequence of its own - keeps the xdeploy drivers and their endpoints alive under live Rulebook evaluation, for an operator to command directly."""

    TEST_NAME = MANUAL_TEST_NAME

    def main_execution(self) -> None:
        # Asked before the runner starts: nothing on this stand can energize the
        # bus, and undervoltage_bound would end the run on its first frame - see
        # xdeploy_rulebook.
        await_operator(
            self,
            "switch the bench supply on and confirm the drive's DC bus is up "
            f"(this run ends immediately below {MIN_BUS_VOLTAGE_V:.1f} V). "
            "The axis is gravity-loaded and has NO BRAKE: whatever the load is resting on is "
            "the only thing holding it, and disarming the axis anywhere else lets it descend "
            "with nothing to catch it.",
        )

        # Both streams, so the bus bound and the thermocouple bounds are all live
        # during an operator's session - see BaseXdeployTest's docstring for why
        # passing fewer would silently evaluate part of the rulebook.
        self.runner.start(
            self.testbed.telemetry,
            self.testbed.tc_daq_telemetry,
        )
        self.wait_for(float("inf"))
