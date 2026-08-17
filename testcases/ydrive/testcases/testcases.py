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
It leaves the 48 V motor bus off, taking BaseYdriveTest's default, so
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

from ..rulebooks.ydrive_rulebook import ENDURANCE_CYCLE_TEST_NAME, MANUAL_TEST_NAME
from .base_ydrive_test import BaseYdriveTest
from ..teststeps.teststeps import cycle_position, move_to, release_brake, set_tuning_params

logger = logging.getLogger(__name__)

METERS_PER_TURN = 0.084


class EnduranceCycleTest(BaseYdriveTest):
    """Cycles ydrive indefinitely between two position setpoints, tracking cumulative distance traveled."""

    TEST_NAME = ENDURANCE_CYCLE_TEST_NAME

    LOW_POSITION = 0.0
    HIGH_POSITION = 10.0

    POWER_MOTOR_BUS_AT_SETUP = True
    """This test drives the axis, so it needs the 48 V bus up before
    main_execution can arm the ODrive."""

    def __init__(self, test_id: Optional[str] = None, use_mock: bool = False, require_engine: bool = True):
        super().__init__(test_id, use_mock, require_engine=require_engine)
        self.total_distance_m = 0.0

    def main_execution(self) -> None:
        self.runner.start(self.testbed.telemetry)
        set_tuning_params(self)
        self.testbed.command.set_control_mode("POSITION_CONTROL")
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


class ManualTest(BaseYdriveTest):
    """No test sequence of its own - keeps the ODrive driver process and command/telemetry endpoints alive, under live Rulebook evaluation, for an operator to command/view directly (e.g. via a GUI) until stopped."""

    TEST_NAME = MANUAL_TEST_NAME

    def main_execution(self) -> None:
        self.runner.start(self.testbed.telemetry)
        self.wait_for(float("inf"))
