"""Test steps for ydrive.

prepare_for_operation: brings the stand from cold to ready-to-arm -
energizes the motor bus, clears whatever the ODrive has latched, sets
the control mode and applies the tuning. Leaves the axis idle and the
brake engaged, since arming is release_brake's to sequence.

move_to: commands a single target position and blocks (closed-loop)
until arrived and settled. Its own step - call it directly from a test
case, or via cycle_position below.

engage_brake / release_brake: the brake and the axis state moved
together, each confirming the axis actually reached the state it was
asked for before the brake is trusted - so the motor never drives
against an engaged brake, and the brake never lets go of a load the
controller has not taken. Not steps: they are sub-actions a step calls,
and publishing themselves as `current_step` would bury it.

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
DEFAULT_ARM_TIMEOUT_S = 3.0
"""How long a brake transition waits for the axis to report the state it was
asked for. Generous enough for the state machine to run, and the wait is paced by
the telemetry stream at ~12 Hz.

Must stay below TelemetryClient's own staleness deadline (5 s by default), since
the wait polls through telemetry: at or above it, a silent stream raises
TelemetryTimeout first and this timeout - the one that names the axis state and
the decoded errors - never fires."""

DEFAULT_CONTROL_MODE = "POSITION_CONTROL"
DEFAULT_CLEAR_TIMEOUT_S = 5.0
"""How long prepare_for_operation keeps clearing before it gives up.

Long enough to cover the bus coming up: the supply's output ramps, and until it
is above the ODrive's own under-voltage trip level the board re-latches
DC_BUS_UNDER_VOLTAGE the moment it is cleared."""

CLEAR_SETTLE_S = 0.25
"""Seconds between clearing and reading the result, so the frame that is checked
was produced after the clear rather than before it."""

MAX_LOAD_VELOCITY_LIMIT = 18.3  # turns/s
MAX_LOAD_FILTER_BW = 10.0  # 1/s
MAX_LOAD_POSITION_GAIN = 10.0
MAX_LOAD_VELOCITY_GAIN = 0.8
MAX_LOAD_VELOCITY_INTEGRATOR = 0.2
MAX_LOAD_SPINOUT_MECHANICAL_THRESHOLD = -100.0  # W
MAX_LOAD_SPINOUT_ELECTRICAL_THRESHOLD = 100.0  # W


def _clear_faults(test_case: BaseYdriveTest, timeout_s: float) -> None:
    """Clear the ODrive's latched errors, and confirm they actually cleared.

    Clearing is retried rather than done once, because the bus is coming up
    while this runs: below the board's under-voltage trip level, clearing
    succeeds and DC_BUS_UNDER_VOLTAGE re-latches on the next control cycle.
    Retrying until the reading is clean waits out the ramp without this step
    having to invent a voltage threshold - the board's own trip level decides.

    Raises with every remaining fault decoded, which is the useful failure: an
    error that will not clear is one the axis will refuse to arm with, and
    finding that out here beats finding it out at the first dwell.

    What was cleared is not reported from here. The driver already logs each
    watched channel that is set at startup and each transition back out of it,
    decoded, into that device's own logs.txt."""
    testbed: YdriveTestbed = test_case.testbed
    deadline: Stopwatch = Stopwatch(duration_s=timeout_s)
    while True:
        test_case.check_should_continue()
        testbed.command.clear_errors()
        test_case.wait_for(CLEAR_SETTLE_S)
        # One frame, so what is judged is a single instant rather than a
        # picture assembled from several.
        remaining = odrive_errors.faults_in_frame(testbed.get_channels())
        if not remaining:
            return
        if deadline.expired:
            raise RuntimeError(
                f"test {test_case.test_id}: ODrive still faulted after {timeout_s}s "
                f"of clear_errors - {remaining}"
            )


@step
def prepare_for_operation(
    test_case: BaseYdriveTest,
    control_mode: str = DEFAULT_CONTROL_MODE,
    clear_timeout_s: float = DEFAULT_CLEAR_TIMEOUT_S,
) -> None:
    """Bring the stand from cold to ready-to-arm: bus up, no latched faults,
    control mode and tuning set.

    The order is what makes it work. The bus is energized first, because the
    ODrive latches DC_BUS_UNDER_VOLTAGE while it is unpowered - a board that
    boots on USB alone always has it set, and an error latched from a previous
    run (a CURRENT_LIMIT_VIOLATION, say) sits alongside it. Clearing happens
    after, and is confirmed rather than assumed, since a latched error is enough
    for the ODrive to refuse CLOSED_LOOP_CONTROL: without this, arming fails at
    the first dwell with the stand already energized.

    This is the only thing on the ydrive stand that energizes the motor bus.
    Setup brings the stand up with both rails off, so a test that never calls
    this one leaves it de-energized.

    Does NOT arm the axis or touch the brake. The brake is left engaged and the
    axis idle, so the load stays held by the brake until release_brake() hands
    it to the controller in the one order that never leaves it held by neither.

    Applies the default tuning; a test wanting different gains calls
    set_tuning_params() afterwards."""
    testbed: YdriveTestbed = test_case.testbed
    testbed.power_motor_bus(True)
    _clear_faults(test_case, clear_timeout_s)
    testbed.command.set_control_mode(control_mode)
    _apply_tuning_params(test_case)


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
        test_case.check_should_continue()
        # One frame, both channels: read separately they come from instants a
        # frame apart, so "arrived AND settled" would never be evaluated against
        # a single moment - and it would cost two frames per iteration.
        channels = testbed.get_channels()
        within_tolerance: bool = (
            abs(channels["pos_estimate"] - target) <= position_tolerance
            and abs(channels["vel_estimate"]) <= velocity_tolerance
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
    So neither brake transition assumes its request took effect; both wait for the
    axis to report it.

    Paced by the telemetry stream, since each read blocks for the next frame, and
    polls check_should_continue() so a fatal bound, a stop request or a lost
    recorder is noticed here rather than
    after the wait. The diagnostic snapshot on timeout comes from get_channels(),
    so its values are all from one instant."""
    testbed: YdriveTestbed = test_case.testbed
    deadline: Stopwatch = Stopwatch(duration_s=timeout_s)
    while True:
        test_case.check_should_continue()
        if testbed.get_axis_armed_status() == armed:
            return
        if deadline.expired:
            # Decoded from odrive_errors' own declared fault set rather than a
            # hand-picked few, so the reason is in here - disarm_reason and
            # last_drv_fault are what actually say why an axis refused to arm.
            channels = testbed.get_channels()
            decoded = {
                name: odrive_errors.describe(name, channels[name])
                for name in odrive_errors.WATCHED_CHANNELS
                if name in channels
            }
            raise RuntimeError(
                f"test {test_case.test_id}: axis did not "
                f"{'arm' if armed else 'idle'} within {timeout_s}s - {decoded}"
            )


def engage_brake(test_case: BaseYdriveTest, arm_timeout_s: float = DEFAULT_ARM_TIMEOUT_S) -> None:
    """Engage the brake, then idle the axis, confirming the axis is idle before
    returning.

    The brake grabs first, so the load is held by the brake before the controller
    lets go of it - the reverse order leaves the load held by nothing for the
    brake's settle time. The axis is then idled, because a braked axis must not be
    armed: the controller would hold position against a locked output, and any
    position error becomes torque into a mechanical stop.

    Raises if the axis does not report itself idle, which means the controller is
    still driving against an engaged brake. The brake is holding by then, so
    raising is safe.

    The settle wait goes through test_case.wait_for(), which polls for a fatal
    bound, a stop request and a lost recorder on every tick - a plain sleep would
    be the one blind wait in a cycle that otherwise checks throughout."""
    testbed: YdriveTestbed = test_case.testbed
    testbed.power_brake_bus(False)
    test_case.wait_for(BRAKE_SETTLE_S)
    testbed.command.set_axis_state("IDLE")
    _await_axis_armed(test_case, armed=False, timeout_s=arm_timeout_s)
    test_case.set_state("brake_engaged", True)


def release_brake(test_case: BaseYdriveTest, arm_timeout_s: float = DEFAULT_ARM_TIMEOUT_S) -> None:
    """Arm the axis, confirm it is actually armed, and only then release the brake
    - the inverse of engage_brake().

    The controller takes hold before the brake lets go, so the load is never
    unheld. Arming against an engaged brake is safe as long as the position
    setpoint still matches where the axis is, which it does when the last move left
    `input_pos` at the position being dwelt at.

    The confirmation is the point rather than a formality. Arming is asynchronous
    and can be declined - a latched error is enough - so releasing the brake on the
    strength of having *asked* for CLOSED_LOOP_CONTROL would drop the load onto a
    controller that never took it. This raises with the axis state and the decoded
    errors instead, leaving the brake engaged.

    Returns only once the brake has had time to let go, so a move commanded
    straight afterwards is not driven into it."""
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

    An aborted dwell leaves the brake engaged and the axis idle rather than
    unwinding, so whatever stopped the test stops it with the load held.

    brake_during_dwell=False cycles without touching the brake or the axis state
    at all, for a stand whose brake isn't wired to the supply, or to compare runs
    with and without it."""

    def dwell() -> None:
        if not brake_during_dwell:
            test_case.wait_for(dwell_s)
            return
        engage_brake(test_case)
        test_case.wait_for(dwell_s)
        # Deliberately not in a `finally`. wait_for() raises on a fatal bound, a
        # stop request or a lost recorder, and on any of those the load should
        # stay where engage_brake() just put it: held by the brake, with the axis
        # idle. Releasing on the way out would hand the load back to a controller
        # at the one moment the reason for stopping is unknown, and nothing
        # downstream needs it - teardown commands no motion.
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
    _apply_tuning_params(
        test_case,
        velocity_limit,
        filter_bw,
        position_gain,
        velocity_gain,
        velocity_integrator,
        spinout_mechanical_threshold,
        spinout_electrical_threshold,
    )


def _apply_tuning_params(
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
