"""Concrete zdrive test cases.

ManualTest: no sequence of its own. Starts live evaluation and blocks on
wait_for(inf), so only a fatal bound ends it, keeping the drivers and their
endpoints alive for an operator. Leaves the axis IDLE and unconfigured, the motor
bus off and the brake engaged: control mode, tuning, arming and energizing a bus
are the operator's to choose, and teardown commands no motion because it cannot
know where they left the axis.
"""
from __future__ import annotations

import logging

from ..rulebooks.zdrive_rulebook import MANUAL_TEST_NAME
from .base_zdrive_test import BaseZdriveTest

logger = logging.getLogger(__name__)


class ManualTest(BaseZdriveTest):
    """No test sequence of its own - keeps the zdrive driver processes and their command/telemetry endpoints alive, under live Rulebook evaluation, for an operator to command/view directly until stopped."""

    TEST_NAME = MANUAL_TEST_NAME

    def main_execution(self) -> None:
        # Both streams, so the bus bounds and the motor bounds are both live
        # during an operator's session - see BaseZdriveTest's docstring for why
        # passing one would silently evaluate half the rulebook.
        self.runner.start(self.testbed.telemetry, self.testbed.bus_telemetry)
        self.wait_for(float("inf"))
