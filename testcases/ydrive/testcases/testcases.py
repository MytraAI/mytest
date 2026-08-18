"""Concrete ydrive test cases.

EnduranceCycleTest: cycles ydrive indefinitely between two position
setpoints (no fixed duration or cycle count - runs until a fatal
Rulebook violation stops it, or the process is stopped externally),
tracking cumulative distance traveled as total_distance_m state.

Moves to LOW_POSITION once before cycling begins, so distance
accounting starts from a known reference point. Distance itself is
computed analytically from LOW_POSITION/HIGH_POSITION via
METERS_PER_TURN, not measured telemetry - assumes each cycle_position()
call actually reaches its target.

Teardown does not move the axis. It stops where the last cycle left it,
and the base class's teardown engages the brake, idles the axis and
drops the bus. Parking at a particular position would mean commanding
motion during teardown - which is when the axis is least trustworthy,
since teardown also runs after a fatal violation or a failure part-way
through a brake transition. Nothing needs it: main_execution moves to
LOW_POSITION before it starts cycling, so the next run establishes its
own reference wherever this one stopped.

ManualTest: no test sequence of its own. Starts live Rulebook
evaluation, then blocks indefinitely via wait_for(float("inf")) -
Stopwatch never expires on its own, so the only way this returns is
check_fatal_violation() raising on a fatal bound, at the same 10ms
polling cadence every other step already relies on. This keeps the
ODrive driver process and its command/telemetry endpoints alive for as
long as an operator (e.g. via a GUI) needs them, for manually viewing
and commanding hardware directly. Deliberately leaves the axis in
IDLE, unconfigured - control mode, tuning, and arming are the
operator's own choice to make in the moment, not this test's to assume.
It never calls prepare_for_operation(), so the 48 V motor bus stays off and
nothing about opening this test energizes the stand: no bus, no arming,
no brake change. Powering a rail is the operator's to decide, the same
way control mode and arming already are - the supply's own command
endpoint can be added in tools/manual_gui.py alongside the ODrive's,
which is how they energize it and release the brake by hand.
post_test_teardown() is inherited unchanged from BaseYdriveTest for the
same reason: it has no idea where the operator left the axis, so it
commands no motion.
"""
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
    await_operator,
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
    """Stops a moving load with the brake, over and over: run 110 -> 0, brake at speed, return to 110, rest on the brake, recording the speed it engaged at and how far the load then travelled."""

    TEST_NAME = BRAKE_ENDURANCE_TEST_NAME

    START_POSITION = 110.0
    """Where each braking run begins. The axis is driven here under position
    control between cycles - the only part of a cycle the controller decides."""

    BRAKE_TARGET_POSITION = 0.0
    """Where each braking run is aimed. The axis never arrives: the brake stops it
    somewhere short, and this only has to be far enough away that the load reaches
    the trigger speed first.

    Travel is 110 -> 0, so the overshoot after a brake event runs *toward* 0 and
    the stroke below it is the clearance a late stop eats into. A stop at the 2 m
    limit is 23.8 turns, so a run-up that reaches trigger speed near 24 turns from
    0 has nothing left - which is what stopping_distance_bound stops the run
    over, after the fact rather than before it."""

    TRIGGER_SPEED_M_S = 1.8
    """Load speed at which the motor is idled and the brake closes. In m/s
    because that is the number the test is specified in; converted through the
    stand's own METERS_PER_TURN, since the controller works in turns.

    IT IS A FLOOR, NOT THE SPEED THE BRAKE ACTUALLY SEES, and on this stand the
    difference is large. Measured: the axis goes from rest to the velocity limit
    inside one telemetry frame (0 -> 21.3 turns/s in 77 ms at 12.6 Hz), so the
    first sample this step can see is already past any lower trigger. A run asked
    for 0.5 m/s and every one of its 52 events engaged at 1.61-1.79 m/s - the
    speed the velocity *limit* allowed, not the one requested, and 12x the
    kinetic energy. At the default the two happen to agree, because
    OVER_ENERGY_VELOCITY_LIMIT is just above this trigger.

    So the engagement speed is set by the velocity limit today. Making the
    requested speed the one that happens means making it the cruise speed - see
    AI/Mytest.md.

    A cycle is: run down toward 0, brake at this speed, hand the load back,
    return to START_POSITION. The return is part of the cycle rather than
    something teardown does - teardown commands no motion at all here, inherited
    unchanged from BaseYdriveTest, so a run that ends leaves the load wherever
    its last complete action put it."""

    VELOCITY_LIMIT = OVER_ENERGY_VELOCITY_LIMIT
    """What this test tunes the controller to before it moves anything. Held as an
    attribute rather than passed inline because the return move's timeout is
    derived from it - see return_timeout_s."""

    DWELL_S = 60.0
    """How long each cycle rests at the start line before the next run-up, held on
    the brake with the axis idle.

    Nothing dissipates during it - a magnet-applied brake needs no power to hold,
    and an idled axis draws no current - so it is what a thermal reading recovers
    over, and it gives the brake a static-hold duty cycle alongside the dynamic
    stops. It also sets the cycle rate: a minute of dwell means about 55 brake
    events an hour rather than 2500.
    """

    MOVE_TIMEOUT_S = 45.0
    """How long a move to the start line may take.

    Flat rather than derived from the velocity limit, and generous on purpose: the
    stroke is 110 turns, which is 5 s at this test's raised limit and 18.5 s at a
    limit set to cruise at 0.5 m/s, so one number covers both and lowering the
    limit cannot break the return. move_to's own 10 s default fits neither, which
    is why this is passed explicitly.

    Still bounded, because a move that will not finish has to be caught: 45 s is
    roughly nine times the traverse it normally takes, and a stalled axis reports
    within it rather than being waited out for the length of a dwell."""

    def __init__(
        self,
        test_id: Optional[str] = None,
        use_mock: bool = False,
        require_engine: bool = True,
        trigger_speed_m_s: Optional[float] = None,
    ):
        """trigger_speed_m_s overrides TRIGGER_SPEED_M_S, purely so a slower
        shakedown run can be done without editing the class - the same reason
        CycleDutPositionTest takes a shortened duration. The class default is the
        real test's speed."""
        super().__init__(test_id, use_mock, require_engine=require_engine)
        self.brake_cycles = 0
        self._trigger_speed_m_s = (
            trigger_speed_m_s if trigger_speed_m_s is not None else self.TRIGGER_SPEED_M_S
        )

    @property
    def trigger_speed_turns_s(self) -> float:
        return self._trigger_speed_m_s / METERS_PER_TURN

    def main_execution(self) -> None:
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
