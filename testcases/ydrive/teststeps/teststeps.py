"""Test steps for ydrive.

move_to: commands a single target position and blocks (closed-loop)
until arrived and settled. Its own step - call it directly from a test
case, or via cycle_position below.

cycle_position: one low<->high position cycle (move_to, dwell, repeat
other direction), then returns - call it repeatedly from
main_execution() for a full cycling test. Assumes the axis is already
armed before this step runs.

set_tuning_params: sets the controller's velocity limit, position
filter bandwidth, position/velocity/velocity-integrator gains, and
spinout power thresholds in one call.
"""
from __future__ import annotations

from testbeds.ydrive_testbed.ydrive_testbed import YdriveTestbed
from testcases.step import step
from testcases.utils import Stopwatch
from testcases.ydrive.testcases.base_ydrive_test import BaseYdriveTest

DEFAULT_DWELL_S = 1.0
DEFAULT_POSITION_TOLERANCE = 0.05  # turns
DEFAULT_VELOCITY_TOLERANCE = 0.05  # turns/s
DEFAULT_ARRIVAL_TIMEOUT_S = 10.0

MAX_LOAD_VELOCITY_LIMIT = 18.3  # turns/s
MAX_LOAD_FILTER_BW = 10.0  # 1/s
MAX_LOAD_POSITION_GAIN = 0.5
MAX_LOAD_VELOCITY_GAIN = 1.2
MAX_LOAD_VELOCITY_INTEGRATOR = 0.2
MAX_LOAD_SPINOUT_MECHANICAL_THRESHOLD = 100.0  # W
MAX_LOAD_SPINOUT_ELECTRICAL_THRESHOLD = -100.0  # W


@step
def move_to(
    test_case: BaseYdriveTest,
    target: float,
    position_tolerance: float = DEFAULT_POSITION_TOLERANCE,
    velocity_tolerance: float = DEFAULT_VELOCITY_TOLERANCE,
    arrival_timeout_s: float = DEFAULT_ARRIVAL_TIMEOUT_S,
) -> None:
    testbed: YdriveTestbed = test_case.testbed
    testbed.command.set_position(target)
    test_case.set_state("position_target", target)
    deadline: Stopwatch = Stopwatch(duration_s=arrival_timeout_s)
    # Closed-loop: block on live readings until we've actually arrived AND
    # settled (not just passing through the target while still moving).
    # Each testbed.get_pos_estimate()/get_vel_estimate() call blocks on the
    # next telemetry frame, so this loop is naturally paced by the
    # telemetry stream rather than a hot spin.
    while True:
        test_case.check_fatal_violation()
        within_tolerance: bool = (
            abs(testbed.get_pos_estimate() - target) <= position_tolerance
            and abs(testbed.get_vel_estimate()) <= velocity_tolerance
        )
        if within_tolerance:
            return
        if deadline.expired:
            raise TimeoutError(
                f"test {test_case.test_id}: position didn't settle at {target} within {arrival_timeout_s}s"
            )


@step
def cycle_position(
    test_case: BaseYdriveTest,
    low_position: float,
    high_position: float,
    dwell_s: float = DEFAULT_DWELL_S,
    position_tolerance: float = DEFAULT_POSITION_TOLERANCE,
    velocity_tolerance: float = DEFAULT_VELOCITY_TOLERANCE,
    arrival_timeout_s: float = DEFAULT_ARRIVAL_TIMEOUT_S,
) -> None:
    def dwell() -> None:
        test_case.wait_for(dwell_s)

    move_to(test_case, high_position, position_tolerance, velocity_tolerance, arrival_timeout_s)
    dwell()

    move_to(test_case, low_position, position_tolerance, velocity_tolerance, arrival_timeout_s)
    dwell()


@step
def set_tuning_params(
    test_case: BaseYdriveTest,
    velocity_limit: float = MAX_LOAD_VELOCITY_LIMIT,
    filter_bw: float = MAX_LOAD_FILTER_BW,
    position_gain: float = MAX_LOAD_POSITION_GAIN,
    velocity_gain: float = MAX_LOAD_VELOCITY_GAIN,
    velocity_integrator: float = MAX_LOAD_VELOCITY_INTEGRATOR,
    spinout_mechanical_threshold: float = MAX_LOAD_SPINOUT_MECHANICAL_THRESHOLD,
    spinout_electrical_threshold: float = MAX_LOAD_SPINOUT_ELECTRICAL_THRESHOLD,
) -> None:
    testbed: YdriveTestbed = test_case.testbed
    testbed.command.set_controller_config_vel_limit(velocity_limit)
    testbed.command.set_controller_config_input_filter_bandwidth(filter_bw)
    testbed.command.set_controller_config_pos_gain(position_gain)
    testbed.command.set_controller_config_vel_gain(velocity_gain)
    testbed.command.set_controller_config_vel_integrator_gain(velocity_integrator)
    testbed.command.set_controller_config_spinout_mechanical_power_threshold(spinout_mechanical_threshold)
    testbed.command.set_controller_config_spinout_electrical_power_threshold(spinout_electrical_threshold)
