"""Concrete ydrive test cases.

EnduranceCycleTest: cycles indefinitely between two positions, tracking
cumulative distance as total_distance_m. Distance is computed from the
setpoints via METERS_PER_TURN, not measured, so it assumes each cycle
reaches its target. Teardown moves nothing: it stops where the last cycle
left it, and main_execution re-establishes its own reference next run.

BrakeEnduranceTest: stops a moving load with the brake over and over -
run 110 -> 0, brake at speed, hold, return, rest - recording the speed it
engaged at and how far the load ended up.

ManualTest: no sequence of its own. Starts live evaluation and blocks on
wait_for(inf), so only a fatal bound ends it, keeping the drivers and
their endpoints alive for an operator (e.g. tools/manual_gui.py). Leaves
the axis IDLE and unconfigured and the bus off: control mode, tuning,
arming and powering a rail are the operator's to choose, and teardown
commands no motion because it cannot know where they left the axis."""
from __future__ import annotations

import logging
from typing import Optional

from testbeds.ydrive_testbed.ydrive_testbed import METERS_PER_TURN

from ..rulebooks.ydrive_rulebook import (
    BRAKE_ENDURANCE_TEST_NAME,
    ENDURANCE_CYCLE_TEST_NAME,
    MANUAL_TEST_NAME,
)
from .base_ydrive_test import BaseYdriveTest
from ..teststeps.teststeps import (
    OVER_ENERGY_VELOCITY_LIMIT,
    RunDetail,
    await_operator,
    await_operator_details,
    brake_from_speed,
    cycle_position,
    dwell_braked,
    move_to,
    prepare_for_operation,
    release_brake,
    release_brake_in_place,
    set_tuning_params,
)

logger = logging.getLogger(__name__)


class EnduranceCycleTest(BaseYdriveTest):
    """Cycles ydrive indefinitely between two position setpoints, tracking cumulative distance traveled."""

    TEST_NAME = ENDURANCE_CYCLE_TEST_NAME

    LOW_POSITION = 0.0
    HIGH_POSITION = 10.0

    def __init__(self, test_id: Optional[str] = None, use_mock: bool = False, require_engine: bool = True):
        super().__init__(test_id, use_mock, require_engine=require_engine)
        self.total_distance_m = 0.0

    def main_execution(self) -> None:
        # Bus up, latched faults cleared, control mode and tuning set - and the
        # axis still idle behind an engaged brake.
        prepare_for_operation(self)
        # Evaluation starts once the stand is in the state the bounds describe.
        # YDRIVE_RULEBOOK's undervoltage_bound is fatal and undebounced, so a
        # runner started over a de-energized bus fails the run on its first
        # frame - the bus reading volts is the correct answer to a question
        # nobody should be asking yet.
        #
        # Both streams: YDRIVE_RULEBOOK's bus bounds are on ODrive channels and
        # its temperature bounds on the DAQ's, and neither device publishes the
        # other's. A bound whose channel is absent from a frame returns no
        # result, so each stream evaluates the bounds it carries.
        self.runner.start(self.testbed.telemetry, self.testbed.tc_daq_telemetry)
        # Arms the axis and then releases the brake, in that order -
        # pre_test_setup left the brake engaged and the axis idle on purpose.
        release_brake(self)
        move_to(self, self.LOW_POSITION)

        distance_per_cycle_m = 2 * abs(self.HIGH_POSITION - self.LOW_POSITION) * METERS_PER_TURN
        self.set_state("total_distance_m", self.total_distance_m)

        while True:
            cycle_position(self, low_position=self.LOW_POSITION, high_position=self.HIGH_POSITION)
            self.total_distance_m += distance_per_cycle_m
            self.set_state("total_distance_m", self.total_distance_m)
            logger.info("test %s: total distance traveled: %.3f m", self.test_id, self.total_distance_m)


class BrakeEnduranceTest(BaseYdriveTest):
    """Stops a moving load with the brake, over and over: run 110 -> 0, brake at speed, hold it there, return to 110, rest on the brake, recording the speed it engaged at and how far the load then travelled."""

    TEST_NAME = BRAKE_ENDURANCE_TEST_NAME

    START_POSITION = 110.0
    """Where each braking run begins. The axis is driven here under position control
    between cycles - the only part of a cycle the controller decides."""

    BRAKE_TARGET_POSITION = 0.0
    """Where each braking run is aimed; the brake stops the axis short of it. Travel is
    110 -> 0, so a late stop's overshoot eats into the clearance below 0."""

    TRIGGER_SPEED_M_S = 1.75
    """A floor, not the speed the brake sees - unloaded, the axis crosses it within one
    frame and engages at the velocity limit. Set below the loaded stand's peak."""

    VELOCITY_LIMIT = OVER_ENERGY_VELOCITY_LIMIT
    """What this test tunes the controller to before anything moves. An attribute
    because the move timeout is checked against it."""

    DUT_SERIAL_NUMBERS = ("YDRIVE1", "YDRIVE2", "ZDRIVE2IN")
    """Every DUT this test can run on, and the only answers its serial prompt accepts:
    a stored run is matched to a DUT by this, and a typo attributes it to nothing."""

    RUN_DETAIL_FIELDS = (
        RunDetail("DUT SN", "dut_serial_number", DUT_SERIAL_NUMBERS),
        RunDetail("ER Ticket", "er_ticket"),
        RunDetail("Load (lb)", "load_lb"),
    )
    """What the operator is asked for before the run starts. The serial is picked from a
    list; the ticket and the load are free text."""

    POST_BRAKE_DWELL_S = 5.0
    """How long the brake holds what it stopped before the distance is taken, so creep
    counts against that distance. Nothing drives, so movement across it is slip."""

    DWELL_S = 600.0
    """How long each cycle rests at the start line, on the brake with the axis idle -
    nothing dissipates. Ten minutes is ~6 events an hour, each from a cold brake."""

    MOVE_TIMEOUT_S = 45.0
    """How long a move to the start line may take: 110 turns is 5 s at this ceiling and
    18.5 s at a 0.5 m/s cruise. Bounded, so a stalled axis reports inside a dwell."""

    def __init__(
        self,
        test_id: Optional[str] = None,
        use_mock: bool = False,
        require_engine: bool = True,
        trigger_speed_m_s: Optional[float] = None,
    ):
        """trigger_speed_m_s overrides TRIGGER_SPEED_M_S, so a slower shakedown needs no
        edit to the class - as CycleDutPositionTest takes a shortened duration."""
        super().__init__(test_id, use_mock, require_engine=require_engine)
        self.brake_cycles = 0
        self.run_details: dict = {}
        self._trigger_speed_m_s = (
            trigger_speed_m_s if trigger_speed_m_s is not None else self.TRIGGER_SPEED_M_S
        )

    @property
    def trigger_speed_turns_s(self) -> float:
        return self._trigger_speed_m_s / METERS_PER_TURN

    def result_metadata(self) -> dict:
        """What this run was, for the verdict.

        The same answers the operator typed, in the one record a reporting database
        ingests - the state channels carry them per frame, which is right for reading
        a run back and wrong for finding every run against a ticket."""
        return {
            **self.run_details,
            "brake_cycles": self.brake_cycles,
            "trigger_speed_m_s": self._trigger_speed_m_s,
        }

    def main_execution(self) -> None:
        # Asked first, while nothing is energized: it needs a person and does not
        # need the stand, and a run nobody can attribute to a DUT is not worth the
        # hours it takes.
        self.run_details = await_operator_details(self, self.RUN_DETAIL_FIELDS)

        prepare_for_operation(self)
        # The trigger speed is above what the normal tuning allows, so the
        # ceiling is raised before anything moves - otherwise the axis clamps
        # below the trigger and the run-up never fires the brake.
        set_tuning_params(self, velocity_limit=self.VELOCITY_LIMIT)
        self.runner.start(self.testbed.telemetry, self.testbed.tc_daq_telemetry)

        # Released and then idled, so the load is free for a person to push. Safe
        # on this stand because the axis is not gravity-loaded; on one that was,
        # this is the state where the load falls.
        release_brake(self)
        self.testbed.command.set_axis_state("IDLE")
        await_operator(
            self,
            "move the load by hand to the end of the stroke it should brake TOWARD "
            "(this becomes position 0), then acknowledge",
        )

        # Rezero in software: the origin is wherever the operator left the load,
        # and every target below is relative to it. The device is not zeroed -
        # there is no command for that in the declared channel set - so the
        # offset is published, which is what keeps a stored run's absolute
        # positions interpretable.
        origin = self.testbed.get_pos_estimate()
        self.set_state("position_origin", origin)
        logger.info("test %s: position origin set at %.3f turns", self.test_id, origin)

        # Up to the start line once. The operator left the load at 0 with the axis
        # idle, so the controller takes it back in place before anything moves.
        release_brake_in_place(self)
        move_to(self, origin + self.START_POSITION, arrival_timeout_s=self.MOVE_TIMEOUT_S)

        while True:
            # Run down toward 0 and let the brake stop it, wherever that is.
            brake_from_speed(
                self,
                target=origin + self.BRAKE_TARGET_POSITION,
                trigger_speed=self.trigger_speed_turns_s,
                rest_s=self.POST_BRAKE_DWELL_S,
            )

            self.brake_cycles += 1
            self.set_state("brake_cycles", self.brake_cycles)
            logger.info("test %s: brake cycle %d complete", self.test_id, self.brake_cycles)


            # Then hand the load back, return it to the start line, and rest
            # there on the brake before the next run-up.
            #
            # After the brake event, deliberately: a bad stop publishes
            # stopping_distance_m, stopping_distance_bound fires on it, and this
            # step's entry check raises before anything drives the load. So the
            # one run that ends with a brake that could not stop the load in 2 m
            # is also the one that never moves it afterwards.
            release_brake_in_place(self)
            move_to(self, origin + self.START_POSITION, arrival_timeout_s=self.MOVE_TIMEOUT_S)
            dwell_braked(self, self.DWELL_S)


class ManualTest(BaseYdriveTest):
    """No test sequence of its own - keeps the ODrive driver process and command/telemetry endpoints alive, under live Rulebook evaluation, for an operator to command/view directly (e.g. via a GUI) until stopped."""

    TEST_NAME = MANUAL_TEST_NAME

    def main_execution(self) -> None:
        # Both streams, so the thermal bounds cover an operator's session too -
        # see EnduranceCycleTest.
        self.runner.start(self.testbed.telemetry, self.testbed.tc_daq_telemetry)
        self.wait_for(float("inf"))
