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

rest_on_brake: waits while the brake holds a load it has just stopped,
touching neither the rail nor the axis - so drift over the window is
the brake slipping.

dwell_braked: engages the brake, holds the load with the axis idle for
a fixed time, then releases - the state that dissipates nothing, so a
thermal reading recovers over it.

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

set_tuning_params: sets the motor's current limits, the controller's
velocity limit, position filter bandwidth,
position/velocity/velocity-integrator gains, and spinout power
thresholds in one call - in RAM, so a run leaves the board's saved
configuration alone.
"""
from __future__ import annotations

import logging
import time

from hardware.odrive import odrive_errors
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

DEFAULT_STOP_TIMEOUT_S = 10.0
"""How long the load may take to come to rest after the brake is commanded. A
brake that never stops it is a failure, not something to keep waiting on."""

OPERATOR_POLL_INTERVAL_S = 0.1
"""How often an operator-gated wait re-checks. Slower than Stopwatch's 10 ms
tick, because a person is the thing being waited on - but every tick still runs
the same abort checks."""

OVER_ENERGY_VELOCITY_LIMIT = 24.0  # turns/s = 2.02 m/s at the stand's 0.084 m/turn
"""Velocity limit for a test that has to reach a speed the normal tuning will not
allow.

MAX_LOAD_VELOCITY_LIMIT below caps the load at 1.54 m/s, so a brake test
triggering at 1.75 m/s (20.83 turns/s) would sit at the clamp forever. This is 13%
above that trigger, and the margin is the point rather than slack: a loaded axis
approaches its ceiling asymptotically, so a limit set just above a trigger is a
trigger that never fires. Measured on this stand, a ceiling of 22 turns/s produced
a peak of 20.96 turns/s over the whole 8.75 m stroke - 5% short of its own
ceiling.

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

MOTOR_CURRENT_SOFT_MAX = 18.0  # A
"""What the controller is allowed to command. Torque is clamped here, so this is
the number that decides how hard the axis can push.

The loaded ydrive stand sits at this limit for roughly 72% of its stroke -
accelerating as hard as it is allowed for the first 4 m, then coasting up toward
its velocity ceiling on less. So this is the number that sets how quickly the load
reaches speed, and the one that decides whether it reaches a trigger at all."""

MOTOR_CURRENT_HARD_MAX = 27.0  # A
"""What the measured phase current may reach before the board latches
CURRENT_LIMIT_VIOLATION and disarms.

Half again above the soft limit, and the size of that gap matters: an axis that
sits at its soft limit for most of a stroke - which this one does - meets the
ceiling on the ordinary overshoot of a step response unless the ceiling is well
clear of it. A gap of a few amps is inside the ripple, and the board latches
CURRENT_LIMIT_VIOLATION and disarms mid-move.

Well inside what the board itself allows - it reports its own inverter limits as
100 A soft and 150 A hard - so this is the motor's protection, not the board's.
Set explicitly rather than inherited, because a stand can be found with a
CURRENT_LIMIT_VIOLATION already latched from a previous session.

Motor current, not bus current, and both limits are reachable: the inverter acts
as a transformer, drawing a small current at 48 V and putting a much larger one
through the phases at a low effective phase voltage. Power is what is conserved,
not current - so 20 A here is not 20 A asked of a supply that can deliver 8.75 A
at 48 V. What that supply's envelope constrains is sustained power, which shows up
as the rail sagging (in_power_limit_2, undervoltage_bound) rather than as bus
current climbing."""


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
                f"test {test_case.test_id}: the ODrive is not fit to operate after {timeout_s}s "
                f"- {_explain_unclearable(remaining)}"
            )


def _require_still_driving(test_case: BaseYdriveTest, motion, doing: str) -> None:
    """Raise if the axis has stopped driving while it was supposed to be.

    The ODrive disarms itself on a fault - a current limit violation, a spinout -
    and nothing tells the test: the axis simply stops driving. A loop watching for
    a position or a speed then keeps waiting, and the load coasts with the brake
    released and the controller idle, held by neither, until a timeout measured in
    tens of seconds fires. Observed: a return move whose axis disarmed on
    CURRENT_LIMIT_VIOLATION coasted for 45 s before the move gave up.

    So every loop that expects the axis to be driving checks it, and fails in one
    frame with the reason the board gives - which teardown then follows by engaging
    the brake."""
    if motion.armed:
        return
    raise RuntimeError(
        f"test {test_case.test_id}: the axis stopped driving while {doing} - it disarmed "
        f"itself at {motion.position:.2f} turns doing {motion.velocity:.2f} turns/s. "
        f"{test_case.testbed.describe_errors()}"
    )


def _explain_unclearable(remaining: dict) -> str:
    """Say which of the remaining faults clear_errors could have cleared, and which
    it never could.

    Worth the words at the one moment somebody is reading them: a latched register
    still set means clearing did not take, while a live condition means clearing was
    never the answer and the cause is physical or configured. Retrying either for
    another five seconds looks identical in a log without this."""
    latched = {name: text for name, text in remaining.items() if name in odrive_errors.LATCHED_CHANNELS}
    conditions = {name: text for name, text in remaining.items() if name in odrive_errors.CONDITION_CHANNELS}
    parts = []
    if latched:
        parts.append(f"still latched after being cleared: {latched}")
    if conditions:
        parts.append(
            f"conditions clear_errors cannot clear: {conditions} - these describe the board now, "
            "so the cause has to change: the bus being up, the encoder cabling, or which encoder "
            "axis0.config.load_encoder/commutation_encoder is set to read"
        )
        # Matched on the decoded text rather than a raw value, because that is what
        # this function is handed. Called out by name because the remedy is
        # mechanical and not guessable from "MISSING_INPUT" on the mappers, which
        # is what this failure otherwise leads with.
        if any("ENCODER_FIELD" in text for text in conditions.values()):
            parts.append(
                "the onboard encoder is reading a field it cannot resolve, which is why the "
                "mappers have no input: turn the wheel by hand, since a rotor parked where the "
                "field saturates reads that way until it moves. If it reads out of range at every "
                "position, the magnet's mounting is the problem"
            )
    other = {k: v for k, v in remaining.items() if k not in latched and k not in conditions}
    if other:
        parts.append(f"other: {other}")
    return "; ".join(parts)


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
    stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S,
    velocity_tolerance: float = DEFAULT_VELOCITY_TOLERANCE,
    position_tolerance: float = DEFAULT_POSITION_TOLERANCE,
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

    THE RUN-UP IS BOUNDED BY THE STROKE, NOT BY A CLOCK. It ends when the trigger
    speed is reached or when the axis arrives at `target` - whichever happens
    first. A time limit answered a different question: a loaded axis drove the
    whole 8.75 m stroke, peaked 2% under the trigger, decelerated into the target
    and then sat there, and the failure reported the speed at the moment the timer
    expired rather than the speed it had actually achieved. Arriving without ever
    reaching the trigger is the real finding, and it is a fact about the stroke.

    Raises then, reporting the *peak* speed rather than the last sample, since the
    peak is what says whether the requested speed is achievable at all. Also raises
    if the load never comes to rest after the brake closes - a brake that does not
    stop the load is a failure, not something to wait on."""
    testbed: YdriveTestbed = test_case.testbed

    testbed.command.set_position(target)
    test_case.set_state("position_target", target)

    started_at = testbed.get_motion().position
    peak_speed = 0.0
    while True:
        test_case.check_should_continue()
        # One frame for all of it, so the speed recorded and the position it was
        # reached at describe the same instant.
        motion = testbed.get_motion()
        _require_still_driving(test_case, motion, f"accelerating to {trigger_speed:.2f} turns/s")
        speed, position = abs(motion.velocity), motion.position
        peak_speed = max(peak_speed, speed)
        if speed >= trigger_speed:
            break
        if abs(position - target) <= position_tolerance:
            travelled = abs(position - started_at)
            raise RuntimeError(
                f"test {test_case.test_id}: the load arrived at {target:.1f} turns without ever "
                f"reaching {trigger_speed:.2f} turns/s "
                f"({trigger_speed * METERS_PER_TURN:.2f} m/s) - it peaked at "
                f"{peak_speed:.2f} turns/s ({peak_speed * METERS_PER_TURN:.2f} m/s) over "
                f"{travelled:.1f} turns ({travelled * METERS_PER_TURN:.2f} m). The load will not "
                "give that speed with this tuning: lower the trigger below the peak, or raise "
                "the velocity limit above it (see OVER_ENERGY_VELOCITY_LIMIT) and the current "
                "limits that feed it"
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
def rest_on_brake(test_case: BaseYdriveTest, seconds: float) -> None:
    """Leave the load where it is, on the brake, for `seconds`.

    For the pause straight after brake_from_speed(), which already left the rail
    down and the axis idle - so this touches neither. Using dwell_braked() here
    would re-engage what is already engaged and, worse, end by releasing the brake
    with release_brake(), which assumes the position setpoint still matches the
    axis. After a brake stop it does not: the setpoint is the far end of the
    stroke. release_brake_in_place() is what handles that, and it is the caller's
    to sequence.

    The load is held by the brake alone throughout, with nothing driving, so
    pos_estimate over this window is the brake's static holding - drift here is
    slip, on a brake that has just absorbed a stop."""
    test_case.wait_for(seconds)


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
        # One frame for all of it, so "arrived AND settled" is judged at a single
        # moment rather than from instants a frame apart - see Motion.
        motion = testbed.get_motion()
        _require_still_driving(test_case, motion, f"moving to {target}")
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
    current_soft_max: float = MOTOR_CURRENT_SOFT_MAX,
    current_hard_max: float = MOTOR_CURRENT_HARD_MAX,
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
        current_soft_max,
        current_hard_max,
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
    current_soft_max: float = MOTOR_CURRENT_SOFT_MAX,
    current_hard_max: float = MOTOR_CURRENT_HARD_MAX,
) -> None:
    """Write the controller and motor configuration this stand runs under.

    In RAM, every run: nothing here calls save_configuration(), so the board keeps
    whatever is in its flash and a run cannot leave a stand configured differently
    than it found it. What that costs is that these have to be set on every run -
    which is the point, since a value inherited from a previous session is how a
    stand ends up running under limits nobody chose."""
    testbed: YdriveTestbed = test_case.testbed
    # Hard ceiling before the soft limit that has to sit under it, so the pair is
    # never briefly inverted on a board whose previous soft limit was higher than
    # the ceiling being set now.
    testbed.command.set_motor_config_current_hard_max(current_hard_max)
    testbed.command.set_motor_config_current_soft_max(current_soft_max)
    testbed.command.set_controller_config_vel_limit(velocity_limit)
    testbed.command.set_controller_config_input_filter_bandwidth(filter_bw)
    testbed.command.set_controller_config_pos_gain(position_gain)
    testbed.command.set_controller_config_vel_gain(velocity_gain)
    testbed.command.set_controller_config_vel_integrator_gain(velocity_integrator)
    testbed.command.set_controller_config_spinout_mechanical_power_threshold(spinout_mechanical_threshold)
    testbed.command.set_controller_config_spinout_electrical_power_threshold(spinout_electrical_threshold)
