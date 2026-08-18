"""Test steps for ydrive.

prepare_for_operation: brings the stand from cold to ready-to-arm -
energizes the motor bus, clears whatever the ODrive has latched, sets
the control mode and applies the tuning. Leaves the axis idle and the
brake engaged, since arming is release_brake's to sequence.

await_operator: publishes an instruction and waits for a person to
say they have done it, still polling for a fatal bound, a stop request
and a lost recorder throughout.

brake_from_speed: accelerates toward a target, and the moment the load
reaches a trigger speed idles the motor and drops the brake rail, so
the brake stops a moving load. Records the speed it engaged at and how
far the load then travelled. The inverse of engage_brake's ordering,
deliberately: see its own docstring.

release_brake_in_place: hands a stopped load back to the controller
without moving it, by parking the setpoint where the axis actually is
before arming.

dwell_braked: holds the load on the brake, axis idle, for a fixed time -
the state that dissipates nothing, so a thermal reading recovers over it.

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

import logging
import time

from testbeds.ydrive_testbed.ydrive_testbed import (
    BRAKE_SETTLE_S,
    METERS_PER_TURN,
    YdriveTestbed,
)
from testcases.step import step
from testcases.utils import Stopwatch, spawn_operator_prompt
from testcases.ydrive.testcases.base_ydrive_test import BaseYdriveTest

logger = logging.getLogger(__name__)

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

INPUT_MODE_POS_FILTER = 3
"""ODrive InputMode.POS_FILTER - the input mode a filtered position move needs.

Set explicitly because it is persistent device state, and the value it is left at
decides whether a commanded position does anything at all. A stand found in
VEL_RAMP (2) accepted every `input_pos` this framework wrote, reported the axis
armed in POSITION_CONTROL, and commanded zero torque against a 110-turn error -
because VEL_RAMP tracks `input_vel`, which nothing here writes. Ten seconds of a
perfectly healthy-looking stand not moving, diagnosable only from
`controller_torque_setpoint` reading 0.

POS_FILTER rather than PASSTHROUGH because set_tuning_params configures
`input_filter_bandwidth`, which is the filter this mode applies; PASSTHROUGH would
ignore it and step the setpoint."""
DEFAULT_CLEAR_TIMEOUT_S = 5.0
"""How long prepare_for_operation keeps clearing before it gives up.

Long enough to cover the bus coming up: the supply's output ramps, and until it
is above the ODrive's own under-voltage trip level the board re-latches
DC_BUS_UNDER_VOLTAGE the moment it is cleared."""

CLEAR_SETTLE_S = 0.25
"""Seconds between clearing and reading the result, so the frame that is checked
was produced after the clear rather than before it."""

DEFAULT_TRIGGER_TIMEOUT_S = 15.0
"""How long brake_from_speed() waits for the load to reach its trigger speed.

Generous: the axis has to accelerate from rest under a filter bandwidth of 10/s.
What it catches is an axis that cannot reach the speed at all - a velocity limit
left at the normal tuning, a load heavier than the gains expect - rather than a
slow one."""

DEFAULT_STOP_TIMEOUT_S = 10.0
"""How long the load may take to come to rest after the brake is commanded. A
brake that never stops it is a failure, not something to keep waiting on."""

OPERATOR_POLL_INTERVAL_S = 0.1
"""How often an operator-gated wait re-checks. Slower than Stopwatch's 10 ms
tick, because a person is the thing being waited on - but every tick still runs
the same abort checks."""

OVER_ENERGY_VELOCITY_LIMIT = 22.0  # turns/s = 1.85 m/s at the stand's 0.084 m/turn
"""Velocity limit for a test that has to reach a speed the normal tuning will not
allow.

MAX_LOAD_VELOCITY_LIMIT below caps the load at 1.54 m/s, so a brake test
triggering at 1.8 m/s (21.43 turns/s) would wait forever at the clamp. This sits
just above that trigger.

It raises the ceiling only - the gains below were tuned against a 130-turn step
at 18.3 turns/s and are unchanged, so the axis is being run faster than it was
tuned for. Deliberate: the point of the test is the kinetic energy the brake has
to absorb, which is what this limit exists to allow."""

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
        remaining = testbed.get_faults()
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

    Sets the input mode as well as the control mode, because a stand can be left
    in one that ignores commanded positions entirely - see INPUT_MODE_POS_FILTER.

    Does NOT arm the axis or touch the brake. The brake is left engaged and the
    axis idle, so the load stays held by the brake until release_brake() hands
    it to the controller in the one order that never leaves it held by neither.

    Applies the default tuning; a test wanting different gains calls
    set_tuning_params() afterwards."""
    testbed: YdriveTestbed = test_case.testbed
    testbed.power_motor_bus(True)
    _clear_faults(test_case, clear_timeout_s)
    testbed.command.set_control_mode(control_mode)
    # Both, not just the control mode: the input mode decides whether a commanded
    # position is acted on at all - see INPUT_MODE_POS_FILTER.
    testbed.command.set_controller_config_input_mode(INPUT_MODE_POS_FILTER)
    _apply_tuning_params(test_case)


@step
def await_operator(test_case: BaseYdriveTest, instruction: str) -> None:
    """Publish an instruction for a person and wait until they acknowledge it.

    Waits indefinitely, because how long somebody takes is not something a test
    can put a limit on. What it does not do is stop watching the stand: every
    tick still calls check_should_continue(), so a fatal bound, an operator stop
    or a lost recorder is noticed while the run sits here - which is the whole
    reason this is a polled marker file rather than input(). A blocking read
    would suspend all three during the one part of a test where somebody has
    their hands on the hardware.

    The instruction is published as `operator_prompt` as well as logged, so a
    recorded run shows how long the stand sat waiting on a person - otherwise
    indistinguishable from a hang - and cleared afterwards so the channel means
    "waiting for this, now".

    A window opens with the instruction and a button (tools/operator_prompt.py);
    `python -m tools.operator_ack` does the same thing from a terminal, which is
    what a headless stand or an SSH session uses. Both write the one marker file
    this polls for, so neither is a special case here, and the window is closed
    once the wait ends however it ended."""
    path = test_case.operator_ack_path()
    path.unlink(missing_ok=True)  # a stale ack from an earlier run must not skip this
    test_case.set_state("operator_prompt", instruction)
    logger.warning("test %s: WAITING FOR OPERATOR - %s", test_case.test_id, instruction)
    logger.warning("test %s: click the window, or `python -m tools.operator_ack`", test_case.test_id)

    window = spawn_operator_prompt(test_case.test_id, instruction)
    clock: Stopwatch = Stopwatch()
    try:
        while True:
            test_case.check_should_continue()
            if path.exists():
                path.unlink(missing_ok=True)
                test_case.set_state("operator_prompt", None)
                logger.info(
                    "test %s: operator acknowledged after %.0fs",
                    test_case.test_id, clock.elapsed_s(),
                )
                return
            time.sleep(OPERATOR_POLL_INTERVAL_S)
    finally:
        # However this ended - acknowledged, a fatal bound, an operator stop - the
        # window is asking for something nobody is waiting for any more, and a
        # stale one left on a stand's screen is worse than none.
        if window is not None:
            window.terminate()


@step
def brake_from_speed(
    test_case: BaseYdriveTest,
    target: float,
    trigger_speed: float,
    trigger_timeout_s: float = DEFAULT_TRIGGER_TIMEOUT_S,
    stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S,
    velocity_tolerance: float = DEFAULT_VELOCITY_TOLERANCE,
) -> None:
    """Accelerate toward `target` and let the brake stop the load once it reaches
    `trigger_speed` turns/s.

    THE ORDER IS THE INVERSE OF engage_brake(), and has to be. That one drops the
    rail first so a stationary load is held before the controller lets go of it;
    here the load is moving, so the motor is idled first and the brake then closes
    on a coasting axis. Doing it the other way would have the motor driving into a
    closing brake, which is the one thing every step here avoids.

    The axis is never commanded to stop. It is idled, so what stops the load is
    the brake and the load's own friction - which is the measurement.

    Records the speed at engagement and how far the load travelled afterwards, as
    `brake_speed_m_s` and `stopping_distance_m` in metres, since that is what a
    limit is written in and what an operator reads. Distance is measured from the
    brake command, so it includes the coast through BRAKE_SETTLE_S -
    understating it by starting from first deceleration would flatter the brake.

    Publishing `stopping_distance_m` is what aborts the run on a bad stop:
    ydrive_rulebook bounds it fatally at 2 m, the runner merges published state
    into what it evaluates, and this step's own exit check raises once that lands.

    Raises if the load never reaches the trigger speed (the velocity limit is
    below it, or the axis cannot get there) and if it never comes to rest - a
    brake that does not stop the load is a failure, not something to wait on."""
    testbed: YdriveTestbed = test_case.testbed

    testbed.command.set_position(target)
    test_case.set_state("position_target", target)

    trigger_deadline: Stopwatch = Stopwatch(duration_s=trigger_timeout_s)
    while True:
        test_case.check_should_continue()
        # One frame for both, so the speed recorded and the position it was
        # reached at describe the same instant.
        motion = testbed.get_motion()
        speed, position = abs(motion.velocity), motion.position
        if speed >= trigger_speed:
            break
        if trigger_deadline.expired:
            raise TimeoutError(
                f"test {test_case.test_id}: the load never reached {trigger_speed} turns/s "
                f"within {trigger_timeout_s}s - reached {speed:.2f} turns/s. Check the "
                "controller's velocity limit, which clamps below the trigger unless a test "
                "raises it (see OVER_ENERGY_VELOCITY_LIMIT)"
            )

    # Idle first, then let the brake close on a coasting axis.
    testbed.command.set_axis_state("IDLE")
    testbed.power_brake_bus(False)
    test_case.set_state("brake_engaged", True)

    stop_deadline: Stopwatch = Stopwatch(duration_s=stop_timeout_s)
    while True:
        test_case.check_should_continue()
        motion = testbed.get_motion()
        if abs(motion.velocity) <= velocity_tolerance:
            stopped_at = motion.position
            break
        if stop_deadline.expired:
            raise TimeoutError(
                f"test {test_case.test_id}: the load was still moving "
                f"{abs(motion.velocity):.2f} turns/s {stop_timeout_s}s after the brake was "
                f"commanded, having travelled "
                f"{abs(motion.position - position) * METERS_PER_TURN:.2f} m"
            )

    test_case.set_state("brake_speed_m_s", speed * METERS_PER_TURN)
    test_case.set_state("stopping_distance_m", abs(stopped_at - position) * METERS_PER_TURN)


def release_brake_in_place(test_case: BaseYdriveTest) -> None:
    """Hand a stopped load back to the controller without moving it.

    release_brake() arms the axis before powering the rail, which is safe only
    while the position setpoint still matches where the axis is. After a brake
    stop it does not: the last command was a move to the far end of the stroke,
    and the load stopped wherever the brake caught it. Arming on that stale
    setpoint would have the controller lunge for the old target the instant the
    brake let go.

    So the setpoint is parked at the current position first. Not a step: it is a
    sub-action, and publishing itself as `current_step` would bury whichever step
    called it."""
    testbed: YdriveTestbed = test_case.testbed
    held_at = testbed.get_pos_estimate()
    testbed.command.set_position(held_at)
    test_case.set_state("position_target", held_at)
    release_brake(test_case)


@step
def dwell_braked(test_case: BaseYdriveTest, dwell_s: float) -> None:
    """Hold the load on the brake for `dwell_s`, with the axis idle.

    Held by the brake rather than by the controller because that is the state
    that dissipates nothing: the brake is magnet-applied, so holding costs no
    coil power, and an idled axis draws no motor current. A minute of the
    controller holding position instead would heat the motor through the very
    interval a thermal reading is supposed to be recovering over.

    Not unwound in a `finally`. wait_for() raises on a fatal bound, a stop request
    or a lost recorder, and on any of those the load should stay where the brake
    has it - handing it back to a controller at the one moment the reason for
    stopping is unknown is the wrong reflex, and teardown commands no motion."""
    engage_brake(test_case)
    test_case.wait_for(dwell_s)
    release_brake(test_case)


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
        # One frame for both, so "arrived AND settled" is judged at a single
        # moment rather than from instants a frame apart - see Motion.
        motion = testbed.get_motion()
        within_tolerance: bool = (
            abs(motion.position - target) <= position_tolerance
            and abs(motion.velocity) <= velocity_tolerance
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
    after the wait. The diagnostic on timeout comes from the testbed's
    describe_errors(), so its values are all from one instant."""
    testbed: YdriveTestbed = test_case.testbed
    deadline: Stopwatch = Stopwatch(duration_s=timeout_s)
    while True:
        test_case.check_should_continue()
        if testbed.get_axis_armed_status() == armed:
            return
        if deadline.expired:
            # Every watched channel, not a hand-picked few: disarm_reason and
            # last_drv_fault are what actually say why an axis refused to arm.
            decoded = testbed.describe_errors()
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
