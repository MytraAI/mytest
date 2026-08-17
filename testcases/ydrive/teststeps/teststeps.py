"""Test steps for ydrive.

move_to: commands a single target position and blocks (closed-loop)
until arrived and settled. Its own step - call it directly from a test
case, or via cycle_position below.

engage_brake / release_brake: the brake and the axis state moved
together, each confirming the axis actually reached the state it was
asked for before the brake is trusted - so the motor never drives
against an engaged brake, and the brake never lets go of a load the
controller has not taken. Not steps: they are sub-actions a step calls.

cycle_position: one low<->high position cycle (move_to, dwell, repeat
other direction), then returns - call it repeatedly from
main_execution() for a full cycling test. Assumes the axis is already
armed AND the brake released before this step runs - pre_test_setup
leaves the brake engaged deliberately, and a concrete test releases it
once the controller is holding (see EnduranceCycleTest). Each dwell is
then held by the brake rather than by the controller alone; see the
step's own docstring.

set_tuning_params: sets the controller's velocity limit, position
filter bandwidth, position/velocity/velocity-integrator gains, and
spinout power thresholds in one call.
"""
from __future__ import annotations

from hardware.odrive import odrive_errors
from testbeds.ydrive_testbed.ydrive_testbed import BRAKE_SETTLE_S, YdriveTestbed
from testcases.step import step
from testcases.utils import Stopwatch
from testcases.ydrive.testcases.base_ydrive_test import BaseYdriveTest

DEFAULT_DWELL_S = 1.0
DEFAULT_POSITION_TOLERANCE = 0.5  # turns
DEFAULT_VELOCITY_TOLERANCE = 0.05  # turns/s
DEFAULT_ARRIVAL_TIMEOUT_S = 10.0
DEFAULT_ARM_TIMEOUT_S = 5.0
"""How long a brake transition waits for the axis to report the state it was
asked for. Generous: arming runs a state machine, and the wait is paced by the
telemetry stream at ~12 Hz."""

MAX_LOAD_VELOCITY_LIMIT = 18.3  # turns/s
MAX_LOAD_FILTER_BW = 10.0  # 1/s
MAX_LOAD_POSITION_GAIN = 10.0
MAX_LOAD_VELOCITY_GAIN = 0.8
MAX_LOAD_VELOCITY_INTEGRATOR = 0.2
MAX_LOAD_SPINOUT_MECHANICAL_THRESHOLD = -100.0  # W
MAX_LOAD_SPINOUT_ELECTRICAL_THRESHOLD = 100.0  # W


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


def _await_axis_armed(test_case: BaseYdriveTest, armed: bool, timeout_s: float) -> None:
    """Block until `axis_is_armed` reads `armed`, or raise.

    Requesting an axis state only writes `requested_state`; the ODrive acts on it
    asynchronously and can decline - a latched error refuses CLOSED_LOOP_CONTROL.
    So neither brake transition may assume its axis-state request took effect,
    and both wait for the axis to actually report it.

    Paced by the telemetry stream, since each read blocks for the next frame, and
    polls check_fatal_violation() so a fatal bound is noticed here rather than
    after the wait. The diagnostic snapshot on timeout comes from get_channels()
    so its three values are from one instant."""
    testbed: YdriveTestbed = test_case.testbed
    deadline: Stopwatch = Stopwatch(duration_s=timeout_s)
    while True:
        test_case.check_fatal_violation()
        if testbed.get_axis_armed_status() is armed:
            return
        if deadline.expired:
            channels = testbed.get_channels()
            errors = channels.get("active_errors")
            raise RuntimeError(
                f"test {test_case.test_id}: axis did not "
                f"{'arm' if armed else 'idle'} within {timeout_s}s - "
                f"axis_current_state={odrive_errors.describe('axis_current_state', channels.get('axis_current_state'))}, "
                f"active_errors={errors} ({odrive_errors.describe('active_errors', errors)}), "
                f"procedure_result={odrive_errors.describe('axis_procedure_result', channels.get('axis_procedure_result'))}"
            )


def engage_brake(test_case: BaseYdriveTest, arm_timeout_s: float = DEFAULT_ARM_TIMEOUT_S) -> None:
    """Engage the brake, then idle the axis, confirming the axis is idle before
    returning.

    Not a @step: this is a sub-action within a step, and publishing itself as
    `current_step` would bury whichever step actually called it.

    The brake grabs first, so the load is held by the brake before the controller
    lets go of it - the reverse order leaves the load held by nothing for the
    brake's settle time. The axis is then idled, because a braked axis must not
    be armed: the controller would hold position against a locked output, and any
    position error becomes torque into a mechanical stop.

    Raises if the axis does not report itself idle, which means the controller is
    still driving against an engaged brake. The brake is holding at that point,
    so raising is safe.

    The settle wait goes through test_case.wait_for(), which polls for a fatal
    bound, a stop request and a lost recorder on every tick - a plain sleep here
    would be the one blind wait in a loop that otherwise checks throughout."""
    testbed: YdriveTestbed = test_case.testbed
    testbed.power_brake_bus(False)
    test_case.wait_for(BRAKE_SETTLE_S)
    testbed.command.set_axis_state("IDLE")
    _await_axis_armed(test_case, armed=False, timeout_s=arm_timeout_s)
    test_case.set_state("brake_engaged", True)


def release_brake(test_case: BaseYdriveTest, arm_timeout_s: float = DEFAULT_ARM_TIMEOUT_S) -> None:
    """Arm the axis, confirm it is actually armed, and only then release the
    brake - the inverse of engage_brake().

    The controller takes hold before the brake lets go, so the load is never
    unheld. Arming against an engaged brake is safe as long as the position
    setpoint still matches where the axis is, which it does here: the last
    move_to() left `input_pos` at the position being dwelt at.

    The confirmation is the point rather than a formality. Arming is asynchronous
    and can be declined - a latched error is enough - so releasing the brake on
    the strength of having *asked* for CLOSED_LOOP_CONTROL would drop the load
    onto a controller that never took it. This raises with the axis state and the
    decoded errors instead, leaving the brake engaged.

    Returns only once the brake has had time to let go, so a move commanded
    straight afterwards is not driven into it. That wait goes through
    test_case.wait_for(), which polls rather than blocking blind.

    Not a @step, for the same reason as engage_brake()."""
    testbed: YdriveTestbed = test_case.testbed
    testbed.command.set_axis_state("CLOSED_LOOP_CONTROL")
    _await_axis_armed(test_case, armed=True, timeout_s=arm_timeout_s)
    testbed.power_brake_bus(True)
    test_case.wait_for(BRAKE_SETTLE_S)
    test_case.set_state("brake_engaged", False)


@step
def cycle_position(
    test_case: BaseYdriveTest,
    low_position: float,
    high_position: float,
    dwell_s: float = DEFAULT_DWELL_S,
    position_tolerance: float = DEFAULT_POSITION_TOLERANCE,
    velocity_tolerance: float = DEFAULT_VELOCITY_TOLERANCE,
    arrival_timeout_s: float = DEFAULT_ARRIVAL_TIMEOUT_S,
    brake_during_dwell: bool = True,
) -> None:
    """One low<->high cycle, with the brake holding each dwell and the axis idled
    while it does.

    The brake is engaged only once the axis has arrived and settled - move_to()
    blocks until both - and released before the next move is commanded. Engaging
    mid-move would brake a moving axis; commanding a move before the brake let go
    would drive the motor into a mechanical stop.

    Across each transition the load is held by the controller, or the brake, or
    both - never neither, and never by a controller pulling against an engaged
    brake. engage_brake()/release_brake() are what enforce that.

    brake_during_dwell=False cycles without touching the brake or the axis state
    at all, for a stand whose brake isn't wired to the supply, or to compare runs
    with and without it."""

    def dwell() -> None:
        if not brake_during_dwell:
            test_case.wait_for(dwell_s)
            return
        engage_brake(test_case)
        try:
            test_case.wait_for(dwell_s)
        finally:
            # Released even if the dwell is cut short - wait_for() raises on a
            # fatal bound. Leaving the axis idle and braked is safe in itself,
            # but teardown moves back to position 0, and it would do that
            # against a held brake.
            release_brake(test_case)

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
