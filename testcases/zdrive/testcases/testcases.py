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
BrakeEnduranceTest: stops a MOVING load with the brake over and over - lift to the
top, hold on the brake, run back down and engage the brake at speed, return to the
bottom, rest - recording the speed it engaged at and how far the load then
travelled.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..rulebooks.zdrive_rulebook import (
    BRAKE_ENDURANCE_TEST_NAME,
    BRAKE_HOLD_TEST_NAME,
    MANUAL_TEST_NAME,
)
from ..teststeps.teststeps import (
    BOTTOM_OF_STROKE,
    RunDetail,
    await_operator,
    brake_from_speed,
    engage_brake,
    establish_origin_at_bottom,
    hold_on_brake,
    lower_to_bottom_for_teardown,
    move_to,
    prepare_for_operation,
    prompt_for_SN_ER_load,
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


class _LiftingZdriveTest(BaseZdriveTest):
    """Shared plumbing for the zdrive tests that lift the load off its stop.

    NOT A RUNNABLE TEST - no TEST_NAME and no main_execution of its own. What it
    holds is the part that is identical between them and must not drift: where the
    bottom is, which DUTs they accept, and the teardown that puts the load back on
    the ground.

    ManualTest deliberately does not inherit this. Its teardown commands no motion,
    because an operator may have left the axis anywhere and this teardown assumes
    the load is above a known origin."""

    BOTTOM_POSITION = BOTTOM_OF_STROKE
    """Where a run starts and ends. The load rests on its hard stop here, which is
    what makes the opening hand-positioning safe."""

    DUT_SERIAL_NUMBERS = ("ZDRIVE2IN",)
    """Every DUT these tests can run on, and the only answers their serial prompt
    accepts: a stored run is matched to a DUT by this, and a typo attributes it to
    nothing."""

    RUN_DETAIL_FIELDS = (
        RunDetail("DUT SN", "dut_serial_number", DUT_SERIAL_NUMBERS),
        RunDetail("ER Ticket", "er_ticket"),
        RunDetail("Load (lb)", "load_lb"),
    )
    """What the operator is asked for before a run starts. The serial is picked from
    a list; the ticket and the load are free text."""

    def __init__(self, test_id=None, use_mock: bool = False, require_engine: bool = True):
        super().__init__(test_id, use_mock, require_engine=require_engine)
        self.run_details: dict = {}
        """What the operator said this run is, once they have been asked. Empty until
        then, so a run that ends before the prompt still has a verdict."""
        self._origin: Optional[float] = None
        """Where the operator left the load, or None before they have. Held on the
        test case rather than only in main_execution because teardown needs it to
        know where the bottom is - see post_test_teardown()."""

    def post_test_teardown(self) -> None:
        """Try to put the load on the ground before shutting the stand down.

        HOWEVER THE RUN ENDED, INCLUDING ON A FATAL BOUND. Every other ending leaves
        the load wherever it got to, held by the brake - and on this stand the brake
        is the component under test, so a run that dies at the top leaves a suspended
        load depending on the one thing being measured, with nobody watching.
        Attempted for ten seconds and then abandoned; the base teardown below engages
        the brake and drops the bus either way.

        Skipped entirely before the origin is known, because nothing has lifted the
        load yet: the run has not got past the operator's positioning, so the load is
        still on its stop and there is nowhere to lower it to."""
        if self._origin is not None:
            self.teardown_step(
                "lower the load to the bottom of the stroke",
                lambda: lower_to_bottom_for_teardown(self, self._origin + self.BOTTOM_POSITION),
            )
        super().post_test_teardown()


class BrakeHoldTest(_LiftingZdriveTest):
    """Drives the load to the top of the stroke, holds it there on the brake alone for a dwell, then returns it to the bottom - recording how far it slipped while only the brake was holding it."""

    TEST_NAME = BRAKE_HOLD_TEST_NAME

    TOP_POSITION = -20.0
    """Where the hold happens, in turns from the bottom. Negative: up is negative
    on this drive.

    Not the top of the stroke. TOP_OF_STROKE is how far the stand can go; this is
    how far this test chooses to lift, and it sits well inside it."""

    HOLD_S = 5.0
    """How long the brake holds the load at the top with the axis idle. The whole
    measurement: nothing but the brake opposes the load's weight for this long,
    and `brake_slip_turns` is what moved."""

    def __init__(self, test_id: Optional[str] = None, use_mock: bool = False, require_engine: bool = True):
        super().__init__(test_id, use_mock, require_engine=require_engine)
        self.brake_holds = 0

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

        # The load is held by nothing while the operator works, which is safe only
        # at the bottom of the stroke - see establish_origin_at_bottom().
        origin = self._origin = establish_origin_at_bottom(self)

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

        slip = hold_on_brake(self, self.HOLD_S, origin)
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



class BrakeEnduranceTest(_LiftingZdriveTest):
    """Stops a moving load with the brake, over and over: lift to the top, hold it there on the brake, run back down and engage the brake at speed, return to the bottom and rest - recording the speed it engaged at and how far the load then travelled."""

    TEST_NAME = BRAKE_ENDURANCE_TEST_NAME

    TOP_POSITION = -50.0
    """Where each run-down begins, in turns from the bottom. Negative: up is
    negative on this drive.

    Deeper into the stroke than BrakeHoldTest's -20, because this test needs room
    for the load to reach the trigger speed and then be stopped, and it still
    leaves 5 turns below TOP_OF_STROKE."""

    TRIGGER_SPEED_TURNS_S = 25.0
    """How fast the load must be moving before the brake is commanded.

    A FLOOR, NOT THE SPEED THE BRAKE SEES - see brake_from_speed(). It must sit
    below RUNDOWN_VELOCITY_LIMIT, or the axis clamps under it and the brake never
    fires."""

    RUNDOWN_VELOCITY_LIMIT = 26.0
    """What the controller is tuned to for the run-down only, above
    TRIGGER_SPEED_TURNS_S so the trigger is reachable.

    The lift runs at the normal VELOCITY_LIMIT instead, deliberately: holding
    1000 lb already draws about 52 A of a 55 A soft limit, so there is almost no
    current left for the extra acceleration a raised ceiling would ask for on the
    way up. Down is where the headroom is - this axis is close to self-locking, so
    a descent draws almost nothing."""

    HOLD_S = 5.0
    """How long the brake holds the load at the top with the axis idle, before the
    run-down. A static hold, the same measurement BrakeHoldTest takes, taken here
    once per cycle so brake wear shows up in slip as well as in stopping
    distance."""

    DWELL_S = 300.0
    """How long each cycle rests at the bottom of the stroke, on the brake with the
    axis idle and the load on its hard stop. Nothing dissipates across it: the brake
    is magnet-applied so holding costs no coil power, and an idled axis draws no
    current.

    Five minutes makes the dwell the whole cycle - about 320 s of which the traverse
    is 19 - so roughly eleven events an hour and 270 a day. Long enough that what
    differs between events is closer to wear than to how hot the last stop left the
    brake, without ydrive's ten minutes.

    WHETHER IT REACHES AMBIENT IS UNMEASURED, and the first cycle of a run is the
    one to watch for it: measured at 1000 lb with a shorter rest, cycle 1 stopped
    36% longer than the cycles after it, which had settled to within 3% of each
    other. A dwell that truly returned the brake to ambient would make every cycle
    look like that first one."""

    MOVE_TIMEOUT_S = 10.0
    """How long the lift to the top may take, before a stalled axis is reported
    rather than waited on.

    Bounded well under the dwell so a stall surfaces inside a cycle rather than
    looking like one. THE LIFT IS CURRENT-LIMITED AT FULL LOAD, not velocity
    limited - holding 1000 lb already draws most of the soft limit - so what sets
    this is how fast the axis can accelerate the load, and raising the velocity
    ceiling does not move it."""

    def __init__(
        self,
        test_id: Optional[str] = None,
        use_mock: bool = False,
        require_engine: bool = True,
        trigger_speed_turns_s: Optional[float] = None,
    ):
        """trigger_speed_turns_s overrides TRIGGER_SPEED_TURNS_S, so a slower
        shakedown needs no edit to the class."""
        super().__init__(test_id, use_mock, require_engine=require_engine)
        self.brake_cycles = 0
        self._trigger_speed = (
            trigger_speed_turns_s if trigger_speed_turns_s is not None
            else self.TRIGGER_SPEED_TURNS_S
        )

    def result_metadata(self) -> dict:
        """What this run was, for the verdict.

        The same answers the operator typed, in the one record a reporting database
        ingests - the state channels carry them per frame, which is right for reading
        a run back and wrong for finding every run against a ticket."""
        return {
            **self.run_details,
            "brake_cycles": self.brake_cycles,
            "trigger_speed_turns_s": self._trigger_speed,
            "top_position_turns": self.TOP_POSITION,
            "hold_s": self.HOLD_S,
        }

    def main_execution(self) -> None:
        # Asked first, while nothing is energized and the brake is still holding:
        # it needs a person and does not need the stand, and a run nobody can
        # attribute to a DUT is not worth the hours it takes.
        self.run_details = prompt_for_SN_ER_load(self, self.RUN_DETAIL_FIELDS)

        prepare_for_operation(self)
        set_tuning_params(self)

        # All three streams: the bus bounds are the supply's channels, the motor
        # bounds the ODrive's and the thermal bounds the DAQ's, and no device
        # publishes another's. stopping_distance_mm is published state rather than
        # a device channel, and the runner merges state into what it evaluates.
        self.runner.start(
            self.testbed.telemetry,
            self.testbed.bus_telemetry,
            self.testbed.tc_daq_telemetry,
        )

        # The load is held by nothing while the operator works, which is safe only
        # at the bottom of the stroke - see establish_origin_at_bottom().
        origin = self._origin = establish_origin_at_bottom(self)

        while True:
            # Take the load back in place: the operator moved the axis away from
            # whatever the setpoint was on the first pass, and on later passes the
            # brake has been holding it and may have crept.
            release_brake_in_place(self)
            move_to(self, origin + self.TOP_POSITION, arrival_timeout_s=self.MOVE_TIMEOUT_S)

            # A static hold first, on the brake alone, so each cycle records slip
            # as well as a stopping distance.
            slip = hold_on_brake(self, self.HOLD_S, origin)

            # The ceiling goes up only now, for the run-down - see
            # RUNDOWN_VELOCITY_LIMIT for why the lift above does not get it.
            set_tuning_params(self, velocity_limit=self.RUNDOWN_VELOCITY_LIMIT)

            # In place again: the hold above is the step that measures the brake
            # letting go, so the load having moved is expected rather than a fault.
            release_brake_in_place(self)
            stopping_distance_mm = brake_from_speed(
                self,
                target=origin + self.BOTTOM_POSITION,
                trigger_speed=self._trigger_speed,
            )

            self.brake_cycles += 1
            self.set_state("brake_cycles", self.brake_cycles)
            logger.info(
                "test %s: brake cycle %d complete - slipped %+.3f turns at the top, "
                "stopped in %.1f mm",
                self.test_id, self.brake_cycles, slip, stopping_distance_mm,
            )

            # Finish the descent and rest at the bottom.
            #
            # AFTER the brake event, deliberately: a bad stop publishes
            # stopping_distance_mm, stopping_distance_bound fires on it, and this
            # step's entry check raises before anything drives the load. So the one
            # cycle that ends with a brake which could not stop the load in 250 mm
            # is also the one that never moves it afterwards.
            # Tuning restored BEFORE the descent rather than after it, so this
            # move runs under the normal ceiling instead of the run-down's. Written
            # while the axis is still idle behind the engaged brake, which is where
            # brake_from_speed left it.
            set_tuning_params(self)
            release_brake_in_place(self)
            move_to(self, origin + self.BOTTOM_POSITION)
            engage_brake(self)
            self.wait_for(self.DWELL_S)

