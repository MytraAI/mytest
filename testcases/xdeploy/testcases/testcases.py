"""Concrete xdeploy test cases: ManualTest and CycleTest."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from protocol.wire import DEVICE_ODRIVE
from testcases.teststeps.operator import await_operator, prompt_for_run_details

from ..rulebooks.xdeploy_rulebook import (
    CYCLE_TEST_NAME,
    MANUAL_TEST_NAME,
    MIN_BUS_VOLTAGE_V,
)
from ..teststeps.teststeps import (
    DEFAULT_DWELL_S,
    FULL_DEPLOY,
    FULL_RETRACT,
    clear_faults,
    cycle_position_forever,
    home_axis,
    park_for_teardown,
    move_to,
    prepare_to_cycle,
)
from .base_xdeploy_test import BaseXdeployTest

logger = logging.getLogger(__name__)


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
            "The axis is gravity-loaded and has NO BRAKE: with the load lifted, the "
            "controller is the only thing holding it, and disarming lets it run to the "
            "ground under its own weight.",
        )

        # Both streams, so the bus bound and the thermocouple bounds are all live
        # during an operator's session - see BaseXdeployTest's docstring for why
        # passing fewer would silently evaluate part of the rulebook.
        self.runner.start(
            self.testbed.telemetry,
            self.testbed.tc_daq_telemetry,
        )
        self.wait_for(float("inf"))


class CycleTest(BaseXdeployTest):
    """Homes the axis against its retract stop, then cycles the load between FULL_RETRACT and FULL_DEPLOY indefinitely, recording cycles, cycle time and travel."""

    TEST_NAME = CYCLE_TEST_NAME

    DERIVED_FROM_DEVICES = (DEVICE_ODRIVE,)

    def __init__(
        self,
        test_id: Optional[str] = None,
        use_mock: bool = False,
        require_engine: bool = True,
    ):
        super().__init__(test_id, use_mock, require_engine=require_engine)
        self.run_details: dict = {}
        """What the operator said this run is. Empty until they have been asked."""
        self.cycle_count = 0
        """Cycles COMPLETED - advanced only once back at FULL_RETRACT, so it
        describes the same work as the travel beside it."""
        self._homed = False
        """Whether homing found the stop. Until it has, no absolute target on
        this axis means anything, and teardown has nowhere to send the load."""
        self._turns_at_cycling_start: Optional[float] = None
        """What the driver's travel counter read when cycling began. The counter
        runs from the driver's connect, so neither homing nor the move out to
        full retract is counted as this run's work."""

    def result_metadata(self) -> dict:
        """What this run was, for the verdict."""
        return {
            **self.run_details,
            "cycle_count": self.cycle_count,
            "total_travel_turns": self.total_travel_turns,
            "full_retract_turns": FULL_RETRACT,
            "full_deploy_turns": FULL_DEPLOY,
            "dwell_s": DEFAULT_DWELL_S,
        }

    def derived_channels(self, latest: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """How far the drive has travelled since cycling began, sampled off the
        ODrive's own count of the path rather than the setpoints it was aimed at."""
        frame = latest.get(DEVICE_ODRIVE)
        if frame is None or self._turns_at_cycling_start is None:
            return {}
        return {
            "total_travel_turns": frame["turns_traveled"] - self._turns_at_cycling_start
        }

    @property
    def total_travel_turns(self) -> float:
        """This run's travel, read back from the state the derivation publishes so
        the verdict cannot disagree with the record."""
        return float(self.state_snapshot().get("total_travel_turns", 0.0))

    def main_execution(self) -> None:
        # Asked first, while nothing is energized: it needs a person and does not
        # need the stand, and a run nobody can attribute is not worth the hours.
        self.run_details = prompt_for_run_details(self, self.RUN_DETAIL_FIELDS)

        # Nothing on this stand can energize the bus, and undervoltage_bound is
        # fatal and undebounced - so this prompt is not a workaround for the
        # bound, it is the only way the condition the bound describes is met.
        await_operator(
            self,
            "switch the bench supply on and confirm the drive's DC bus is up "
            f"(this run ends immediately below {MIN_BUS_VOLTAGE_V:.1f} V). "
            "The run will then HOME ITSELF against the retract stop and start cycling - "
            "nobody needs to position the unit, but stand clear before acknowledging. "
            "The axis is gravity-loaded and has NO BRAKE: a fault disarms it and the load "
            "runs to the ground under its own weight.",
        )

        # Started before homing, so the creep into the hard stop is supervised by
        # the bus and thermal bounds too. Both streams: a bound whose channel is
        # absent from a frame returns no result, so one stream would silently
        # evaluate half the rulebook - see BaseXdeployTest.
        self.runner.start(
            self.testbed.telemetry,
            self.testbed.tc_daq_telemetry,
        )

        clear_faults(self)
        home_axis(self)
        self._homed = True

        # Armed here and left armed for the rest of the run. With no brake the
        # controller is the only thing that holds the load, so a disarm anywhere
        # below is a fault rather than a step.
        prepare_to_cycle(self)

        move_to(self, FULL_RETRACT)

        # Taken here, at the position every cycle starts and ends at, so neither
        # the homing creep nor this one-off move out from the stop is booked as
        # cycling travel. The driver's counter runs from its own connect, and it
        # already excludes the rezero itself.
        self._turns_at_cycling_start = self.testbed.get_channels()["turns_traveled"]

        cycle_position_forever(self)

    def post_test_teardown(self) -> None:
        """Put the load back on the ground before the stand is shut down, however
        the run ended. Skipped before homing, when no target means anything and
        nothing has been lifted."""
        if self._homed:
            self.teardown_step(
                "return the axis to full retract",
                lambda: park_for_teardown(self),
            )
        super().post_test_teardown()
