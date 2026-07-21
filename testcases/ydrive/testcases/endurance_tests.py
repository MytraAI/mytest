"""EnduranceCycleTest: cycles ydrive indefinitely between two position
setpoints (no fixed duration or cycle count - runs until a fatal
Rulebook violation stops it, or the process is stopped externally),
tracking cumulative distance traveled as total_distance_m state.

Moves to LOW_POSITION once before cycling begins, so distance
accounting starts from a known reference point. Distance itself is
computed analytically from LOW_POSITION/HIGH_POSITION via
METERS_PER_TURN, not measured telemetry - assumes each cycle_position()
call actually reaches its target.

post_test_teardown() moves back to position 0 before the base class's
own teardown idles the axis - wrapped in _teardown_step() like every
other teardown action, so a fatal violation already in flight (move_to
checks it at entry via @step) or a stuck axis just logs and moves on to
idling rather than blocking teardown.
"""
from __future__ import annotations

import logging
from typing import Optional

from .base_ydrive_test import BaseYdriveTest
from ..teststeps.teststeps import cycle_position, move_to, set_tuning_params

logger = logging.getLogger(__name__)

METERS_PER_TURN = 0.084


class EnduranceCycleTest(BaseYdriveTest):
    """Cycles ydrive indefinitely between two position setpoints, tracking cumulative distance traveled."""

    TEST_NAME = "endurance_cycle_test"

    LOW_POSITION = 0.0
    HIGH_POSITION = 10.0

    def __init__(self, test_id: Optional[str] = None, use_mock: bool = False):
        super().__init__(test_id, use_mock)
        self.total_distance_m = 0.0

    def main_execution(self) -> None:
        self.runner.start(self.testbed.telemetry)
        set_tuning_params(self)
        self.testbed.command.set_control_mode("POSITION_CONTROL")
        self.testbed.command.set_axis_state("CLOSED_LOOP_CONTROL")
        move_to(self, self.LOW_POSITION)

        distance_per_cycle_m = 2 * abs(self.HIGH_POSITION - self.LOW_POSITION) * METERS_PER_TURN
        self.set_state("total_distance_m", self.total_distance_m)

        while True:
            cycle_position(self, low_position=self.LOW_POSITION, high_position=self.HIGH_POSITION)
            self.total_distance_m += distance_per_cycle_m
            self.set_state("total_distance_m", self.total_distance_m)
            logger.info("test %s: total distance traveled: %.3f m", self.test_id, self.total_distance_m)

    def post_test_teardown(self) -> None:
        if self.testbed is not None:
            self.teardown_step("move to position 0", lambda: move_to(self, 0.0))
        super().post_test_teardown()
