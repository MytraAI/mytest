"""Concrete zdrive test cases.

ManualTest: no sequence of its own. Starts live evaluation and blocks on
wait_for(inf), so only a fatal bound ends it, keeping the drivers and their
endpoints alive for an operator. Leaves the axis IDLE and unconfigured, the motor
bus off and the brake engaged: control mode, tuning, arming and energizing a bus
are the operator's to choose, and teardown commands no motion because it cannot
know where they left the axis.

BrakeHoldTest: asks the operator which DUT, ticket and load this run is, then
lifts the load, pauses under the controller for a person to check the rig, holds
it on the brake alone, and brings it back down - recording how far it slipped
while the brake was the only thing holding it.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..rulebooks.zdrive_rulebook import BRAKE_HOLD_TEST_NAME, MANUAL_TEST_NAME
from ..teststeps.teststeps import (
    BOTTOM_OF_STROKE,
    RunDetail,
    await_operator,
    hold_on_brake,
    lower_to_bottom_for_teardown,
    move_to,
    prepare_for_operation,
    prompt_for_SN_ER_load,
    release_brake_for_positioning,
    release_brake_in_place,
    set_tuning_params,
)
from .base_zdrive_test import BaseZdriveTest

logger = logging.getLogger(__name__)


class ManualTest(BaseZdriveTest):
    """No test sequence of its own - keeps the zdrive driver processes and their command/telemetry endpoints alive, under live Rulebook evaluation, for an operator to command/view directly until stopped."""

    TEST_NAME = MANUAL_TEST_NAME

    def main_execution(self) -> None:
        # All three streams, so the bus, motor and thermocouple bounds are all
        # live during an operator's session - see BaseZdriveTest's docstring for
        # why passing fewer would silently evaluate part of the rulebook.
        self.runner.start(
            self.testbed.telemetry,
            self.testbed.bus_telemetry,
            self.testbed.tc_daq_telemetry,
        )
        self.wait_for(float("inf"))


class BrakeHoldTest(BaseZdriveTest):
    """Drives the load to the top of the stroke, holds it there on the brake alone for a dwell, then returns it to the bottom - recording how far it slipped while only the brake was holding it."""

    TEST_NAME = BRAKE_HOLD_TEST_NAME

    TOP_POSITION = -20.0
    """Where the hold happens, in turns from the bottom. Negative: up is negative
    on this drive.

    Not the top of the stroke. TOP_OF_STROKE is how far the stand can go; this is
    how far this test chooses to lift, and it sits well inside it."""

    BOTTOM_POSITION = BOTTOM_OF_STROKE
    """Where the run starts and ends. The load rests on its hard stop here, which
    is what makes the opening hand-positioning safe."""

    HOLD_S = 5.0
    """How long the brake holds the load at the top with the axis idle. The whole
    measurement: nothing but the brake opposes the load's weight for this long,
    and `brake_slip_turns` is what moved."""

    DUT_SERIAL_NUMBERS = ("ZDRIVE2IN",)
    """Every DUT this test can run on, and the only answers its serial prompt accepts:
    a stored run is matched to a DUT by this, and a typo attributes it to nothing."""

    RUN_DETAIL_FIELDS = (
        RunDetail("DUT SN", "dut_serial_number", DUT_SERIAL_NUMBERS),
        RunDetail("ER Ticket", "er_ticket"),
        RunDetail("Load (lb)", "load_lb"),
    )
    """What the operator is asked for before the run starts. The serial is picked from a
    list; the ticket and the load are free text."""

    def __init__(self, test_id: Optional[str] = None, use_mock: bool = False, require_engine: bool = True):
        super().__init__(test_id, use_mock, require_engine=require_engine)
        self.brake_holds = 0
        self.run_details: dict = {}
        """What the operator said this run is, once they have been asked. Empty
        until then, so a run that ends before the prompt still has a verdict."""
        self._origin: Optional[float] = None
        """Where the operator left the load, or None before they have. Held on the
        test case rather than only in main_execution because teardown needs it to
        know where the bottom is - see post_test_teardown()."""

    def result_metadata(self) -> dict:
        """What this run was, for the verdict."""
        return {
            **self.run_details,
            "brake_holds": self.brake_holds,
            "top_position_turns": self.TOP_POSITION,
            "hold_s": self.HOLD_S,
        }

    def main_execution(self) -> None:
        # Asked first, while nothing is energized and the brake is still holding:
        # it needs a person and does not need the stand, and a run nobody can
        # attribute to a DUT is not worth the hours it takes.
        self.run_details = prompt_for_SN_ER_load(self, self.RUN_DETAIL_FIELDS)

        # Bus up, latched faults cleared, control and input mode set - and the
        # axis still idle behind an engaged brake.
        prepare_for_operation(self)
        set_tuning_params(self)

        # Evaluation starts once the stand is in the state the bounds describe.
        # All three streams: the bus bounds are the supply's channels, the motor
        # bounds the ODrive's and the thermal bounds the DAQ's, and no device
        # publishes another's.
        self.runner.start(
            self.testbed.telemetry,
            self.testbed.bus_telemetry,
            self.testbed.tc_daq_telemetry,
        )

        # Hand the load to a person so they can set the origin. This is the one
        # moment the load is held by neither the brake nor the controller, and it
        # is safe only because the load bottoms out here - see
        # release_brake_for_positioning(). The gear is light, around 20 lb.
        release_brake_for_positioning(self)
        await_operator(
            self,
            "move the drive to the BOTTOM of the stroke, where the load rests on its stop "
            "(this becomes position 0), then acknowledge",
        )

        # Rezero in software: the origin is wherever the operator left the load,
        # and every target below is relative to it. The device is not zeroed -
        # there is no command for that in the declared channel set - so the
        # offset is published, which is what keeps a stored run's absolute
        # positions interpretable.
        origin = self.testbed.get_pos_estimate()
        self._origin = origin
        self.set_state("position_origin", origin)
        logger.info("test %s: position origin set at %.3f turns", self.test_id, origin)

        # Take the load back under control before anything moves. In place,
        # because the operator has moved the axis away from whatever the setpoint
        # was and arming to a stale one would lunge.
        release_brake_in_place(self)
        move_to(self, origin + self.TOP_POSITION)

        # Held at the top by the CONTROLLER, not the brake, for as long as the
        # operator takes. Full gravity current is flowing the whole time and the
        # axis is stationary, so there is no airflow over the motor - this is the
        # most thermally expensive part of the run, and its length is a person's
        # choice.
        await_operator(
            self,
            f"the load is held at {self.TOP_POSITION:+.0f} turns by the controller - check the "
            "rig, then acknowledge to hand it to the brake alone",
        )

        slip = hold_on_brake(self, self.HOLD_S)
        self.brake_holds += 1
        self.set_state("brake_holds", self.brake_holds)
        logger.info(
            "test %s: brake hold %d complete, slipped %+.3f turns",
            self.test_id, self.brake_holds, slip,
        )

        # In place again, deliberately: if the brake slipped, the axis is no
        # longer where the move left the setpoint.
        release_brake_in_place(self)
        move_to(self, origin + self.BOTTOM_POSITION)

    def post_test_teardown(self) -> None:
        """Try to put the load on the ground before shutting the stand down.

        HOWEVER THE RUN ENDED, INCLUDING ON A FATAL BOUND. Every other ending
        leaves the load wherever it got to, held by the brake - and on this stand
        the brake is the component under test, so a run that dies at the top
        leaves a suspended load depending on the one thing being measured, with
        nobody watching. Attempted for ten seconds and then abandoned; the base
        teardown below engages the brake and drops the bus either way.

        Skipped entirely before the origin is known, because nothing has lifted
        the load yet: the run has not got past the operator's positioning, so the
        load is still on its stop and there is nowhere to lower it to."""
        if self._origin is not None:
            self.teardown_step(
                "lower the load to the bottom of the stroke",
                lambda: lower_to_bottom_for_teardown(self, self._origin + self.BOTTOM_POSITION),
            )
        super().post_test_teardown()
