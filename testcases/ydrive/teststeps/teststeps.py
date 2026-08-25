"""Test steps for ydrive.

prepare_for_operation: cold stand to ready-to-arm - bus up, faults
cleared, control and input mode set, tuning applied. Leaves the axis
idle behind an engaged brake.

await_operator / prompt_for_SN_ER_load: wait for a person, the second
collecting the run's serial, ticket and load. Both keep polling for a
fatal bound, a stop request and a lost recorder while they wait.

brake_from_speed: runs up to a trigger speed, then idles the motor and
drops the brake rail so the brake stops a moving load. Records the speed
and the stopping distance, and returns where the load came to rest.

release_brake_in_place: hands a stopped load back to the controller
without moving it.

establish_origin_by_hand: hands the load to a person, and makes where
they leave it position 0.

move_to: commands one target position, blocks until arrived and
settled, and returns the furthest position reached along the way - which
past an overshoot is not the position arrival was accepted at, and is
what a caller measuring distance travelled accumulates. See its docstring
for when that equals the path the load took and when it falls short.

engage_brake / release_brake: the brake and the axis state moved together,
each confirming the axis reached the state it was asked for - so the motor
never drives against an engaged brake, and the brake never lets go of a load
the controller has not taken. Not steps: they would bury their caller's
`current_step`.

dwell_braked: holds the load on the brake, axis idle - the state that
dissipates nothing.

cycle_position: one low<->high cycle with a braked dwell at each end.
Assumes the axis is armed and the brake released.

set_tuning_params: the motor's current limits, the controller's velocity
limit, filter bandwidth, gains and spinout thresholds - in RAM, so a run
leaves the board's saved configuration alone."""
from __future__ import annotations

import json

import logging
import time
from typing import Callable, Dict, NamedTuple, Optional, Sequence, Tuple

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
"""How long a brake transition waits for the axis to report the state it asked for.
Under TelemetryClient's 5 s staleness deadline, or a silent stream raises first."""

DEFAULT_CONTROL_MODE = "POSITION_CONTROL"

INPUT_MODE_POS_FILTER = 3
"""ODrive InputMode.POS_FILTER, the mode a filtered position move needs. Set
explicitly: a stand left in VEL_RAMP accepts every input_pos and ignores it."""
DEFAULT_CLEAR_TIMEOUT_S = 5.0
"""How long prepare_for_operation keeps clearing before it gives up - long enough
for the bus to come up, since DC_BUS_UNDER_VOLTAGE re-latches until it has."""

CLEAR_SETTLE_S = 0.25
"""Seconds between clearing and reading the result, so the frame checked was
produced after the clear rather than before it."""

DEFAULT_POST_BRAKE_REST_S = 5.0
"""How long the brake holds a load it has just stopped before the stopping distance
is taken, so creep while it holds counts against that distance."""

DEFAULT_STOP_TIMEOUT_S = 10.0
"""How long the load may take to come to rest after the brake closes. A brake that
never stops it is a failure, not something to keep waiting on."""

OPERATOR_POLL_INTERVAL_S = 0.1
"""How often an operator-gated wait re-checks - slower than Stopwatch's tick because
a person is what is being waited on. Every tick still runs the abort checks."""

OVER_ENERGY_VELOCITY_LIMIT = 24.0  # turns/s = 2.02 m/s at the stand's 0.084 m/turn
"""Velocity ceiling for a test needing a speed the normal tuning forbids, 15% above
the trigger: a loaded axis nears its ceiling asymptotically."""

MAX_LOAD_VELOCITY_LIMIT = 18.3  # turns/s
MAX_LOAD_FILTER_BW = 10.0  # 1/s
MAX_LOAD_POSITION_GAIN = 10.0
MAX_LOAD_VELOCITY_GAIN = 0.8
MAX_LOAD_VELOCITY_INTEGRATOR = 0.2
MAX_LOAD_SPINOUT_MECHANICAL_THRESHOLD = -100.0  # W
MAX_LOAD_SPINOUT_ELECTRICAL_THRESHOLD = 100.0  # W

MOTOR_CURRENT_SOFT_MAX = 18.0  # A
"""What the controller may command, so it decides how hard the axis pushes and how
fast the load reaches speed. The loaded stand sits at it for most of a stroke."""

MOTOR_CURRENT_HARD_MAX = 27.0  # A - reachable: the inverter is a transformer, phase amps exceed bus amps
"""Where measured phase current latches CURRENT_LIMIT_VIOLATION and disarms. Half
again above the soft limit, which the axis sits at for most of a stroke."""


def _clear_faults(test_case: BaseYdriveTest, timeout_s: float) -> None:
    """Clear the ODrive's latched errors and confirm they cleared.

    Retried rather than done once: below the board's under-voltage trip level a
    clear succeeds and DC_BUS_UNDER_VOLTAGE re-latches, so retrying waits out the
    bus ramp without this step inventing a voltage threshold. Raises with the
    remaining faults decoded - an error that will not clear is one the axis will
    refuse to arm with.

    What cleared is not reported here; the driver logs every watched channel's
    transitions into its own logs.txt."""
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
    """Raise if the axis has stopped driving when it should be.

    The ODrive disarms itself on a fault and tells nobody, so a loop watching for a
    position or a speed keeps waiting while the load coasts - brake released,
    controller idle, held by neither - until a timeout measured in tens of seconds.
    Failing here costs one frame, and teardown then engages the brake."""
    if motion.armed:
        return
    raise RuntimeError(
        f"test {test_case.test_id}: the axis stopped driving while {doing} - it disarmed "
        f"itself at {motion.position:.2f} turns doing {motion.velocity:.2f} turns/s. "
        f"{test_case.testbed.describe_errors()}"
    )


def _explain_unclearable(remaining: dict) -> str:
    """Split the remaining faults into what clear_errors resets and what it never
    could.

    A latched register still set means clearing did not take; a live condition means
    clearing was never the answer. Undistinguished, both read as "retry"."""
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
    control and input mode set, tuning applied.

    Order matters. The bus is energized first, because the ODrive latches
    DC_BUS_UNDER_VOLTAGE while unpowered; clearing then runs against a live bus and
    is confirmed, since a latched error is enough for the board to refuse
    CLOSED_LOOP_CONTROL. The input mode is set as well as the control mode - a stand
    left in VEL_RAMP ignores commanded positions entirely (see
    INPUT_MODE_POS_FILTER).

    The only thing on this stand that energizes the motor bus, so a test that never
    calls it leaves the stand cold.

    Does NOT arm the axis or touch the brake: the load stays held by the brake until
    release_brake() hands it over. Applies the default tuning; call
    set_tuning_params() afterwards for different gains."""
    testbed: YdriveTestbed = test_case.testbed
    testbed.power_motor_bus(True)
    _clear_faults(test_case, clear_timeout_s)
    testbed.command.set_control_mode(control_mode)
    # Both, not just the control mode: the input mode decides whether a commanded
    # position is acted on at all - see INPUT_MODE_POS_FILTER.
    testbed.command.set_controller_config_input_mode(INPUT_MODE_POS_FILTER)
    _apply_tuning_params(test_case)


def _await_ack(
    test_case: BaseYdriveTest,
    instruction: str,
    fields: Sequence[str] = (),
    choices: Optional[Dict[str, Sequence[str]]] = None,
) -> str:
    """Publish an instruction, wait for the operator's marker, and return its
    contents - empty for a plain acknowledgement, JSON when values were asked for.

    Waits indefinitely, but not blindly: every tick calls check_should_continue(),
    so a fatal bound, a stop request or a lost recorder is still noticed while
    somebody has their hands on the hardware. That is why this is a polled marker
    file and not input(). The instruction is published as `operator_prompt` and
    cleared after, so a recorded run shows waiting rather than looking like a hang."""
    path = test_case.operator_ack_path()
    path.unlink(missing_ok=True)  # a stale ack from an earlier run must not skip this
    test_case.set_state("operator_prompt", instruction)
    logger.warning("test %s: WAITING FOR OPERATOR - %s", test_case.test_id, instruction)
    logger.warning("test %s: click the window, or `python -m tools.operator_ack`", test_case.test_id)

    window = spawn_operator_prompt(test_case.test_id, instruction, fields, choices)
    clock: Stopwatch = Stopwatch()
    try:
        while True:
            test_case.check_should_continue()
            if path.exists():
                answered = path.read_text()
                path.unlink(missing_ok=True)
                test_case.set_state("operator_prompt", None)
                logger.info(
                    "test %s: operator acknowledged after %.0fs",
                    test_case.test_id, clock.elapsed_s(),
                )
                return answered
            time.sleep(OPERATOR_POLL_INTERVAL_S)
    finally:
        # However this ended - acknowledged, a fatal bound, an operator stop - the
        # window is asking for something nobody is waiting for any more, and a
        # stale one left on a stand's screen is worse than none.
        if window is not None:
            window.terminate()


class RunDetail(NamedTuple):
    """One thing the operator is asked for before a run.

    `label` is read, `channel` is stored, and they are separate so rewording a
    prompt cannot rename a channel that stored runs are keyed by. `choices` makes
    the prompt a dropdown, and is enforced on the answer however it arrives."""

    label: str
    channel: str
    choices: Tuple[str, ...] = ()


@step
def prompt_for_SN_ER_load(
    test_case: BaseYdriveTest, fields: Sequence[RunDetail]
) -> Dict[str, str]:
    """Ask the operator for the details that identify this run, and publish them.

    A field with choices is a dropdown and its answer is checked against them - the
    window cannot produce anything else, but `tools.operator_ack --answer` can.

    Published as run state, so the engine merges them into every recorded row. The
    channels have to be seeded (../channels.py) or the engine fixes its header
    before they exist and drops them. Asked before anything is energized."""
    answered = _await_ack(
        test_case,
        "enter this run's details",
        [field.label for field in fields],
        {field.label: field.choices for field in fields if field.choices},
    )
    try:
        answers = json.loads(answered) if answered else {}
    except ValueError:
        answers = {}

    details: Dict[str, str] = {}
    for field in fields:
        value = answers.get(field.label)
        if not value:
            raise RuntimeError(
                f"test {test_case.test_id}: no answer for {field.label!r} - a run that cannot be "
                "attributed to a DUT is not worth the hours it takes. Acknowledge with the "
                "window, or `python -m tools.operator_ack --answer "
                f"'{field.label}=...'` for a stand with no display"
            )
        if field.choices and value not in field.choices:
            raise RuntimeError(
                f"test {test_case.test_id}: {value!r} is not one of the values {field.label!r} "
                f"accepts ({', '.join(field.choices)}). A serial the record cannot match to a DUT "
                "is worse than no serial, so this is refused rather than stored"
            )
        details[field.channel] = value
        test_case.set_state(field.channel, value)
    logger.info("test %s: run details %s", test_case.test_id, details)
    return details


@step
def await_operator(test_case: BaseYdriveTest, instruction: str) -> None:
    """Publish an instruction for a person and wait until they acknowledge it.

    A window opens with a button (tools/operator_prompt.py), and
    `python -m tools.operator_ack` answers the same marker from a terminal, which
    is what a headless stand uses. See _await_ack for the wait itself."""
    _await_ack(test_case, instruction)


@step
def brake_from_speed(
    test_case: BaseYdriveTest,
    target: float,
    trigger_speed: float,
    stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S,
    rest_s: float = DEFAULT_POST_BRAKE_REST_S,
    velocity_tolerance: float = DEFAULT_VELOCITY_TOLERANCE,
    position_tolerance: float = DEFAULT_POSITION_TOLERANCE,
    on_engaged: Optional[Callable[[], None]] = None,
) -> float:
    """Accelerate toward `target` and let the brake stop the load once it reaches
    `trigger_speed` turns/s.

    THE ORDER IS THE INVERSE OF engage_brake(): the motor is idled first and the
    brake closes on a coasting axis, since doing it the other way would drive the
    motor into a closing brake. The axis is never commanded to stop - what stops the
    load is the brake, which is the measurement.

    STOPPING DISTANCE IS EVERYTHING AFTER THE BRAKE IS COMMANDED: the coast before
    it bites, the deceleration, and any creep during `rest_s`. The start is the
    trigger frame - the command, not the physical engagement, which is up to
    BRAKE_SETTLE_S later and unobservable from here.

    Bounded by the stroke, not a clock: it ends at the trigger speed or on arrival,
    and arriving short raises with the peak speed reached, since the peak says
    whether the speed is achievable at all. Also raises if the load never comes to
    rest. Publishes `brake_speed_m_s` and `stopping_distance_m`, in metres.

    Returns the position the load came to rest at, so a caller measuring distance
    travelled learns where the brake put it without a second read - the same
    contract move_to() has, for the same reason.

    `on_engaged` is called the instant the rail drops, before anything that can
    fail. A caller counting brake events has to hear about them there rather than
    from a return value: everything after this point can raise - the load may never
    stop, and @step re-checks for a fatal bound on the way out, which the stopping
    distance published below is itself able to trip - and an event the brake
    performed is one the DUT has been through whether the run survived it or not."""
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
    if on_engaged is not None:
        on_engaged()

    stop_deadline: Stopwatch = Stopwatch(duration_s=stop_timeout_s)
    while True:
        test_case.check_should_continue()
        motion = testbed.get_motion()
        if abs(motion.velocity) <= velocity_tolerance:
            break
        if stop_deadline.expired:
            raise TimeoutError(
                f"test {test_case.test_id}: the load was still moving "
                f"{abs(motion.velocity):.2f} turns/s {stop_timeout_s}s after the brake was "
                f"commanded, having travelled "
                f"{abs(motion.position - position) * METERS_PER_TURN:.2f} m"
            )

    # The brake keeps what it stopped, and only then is the distance taken - see
    # DEFAULT_POST_BRAKE_REST_S. Nothing here touches the rail or the axis: they
    # were left where they should be above.
    test_case.wait_for(rest_s)
    rested_at = testbed.get_motion().position

    test_case.set_state("brake_speed_m_s", speed * METERS_PER_TURN)
    test_case.set_state("stopping_distance_m", abs(rested_at - position) * METERS_PER_TURN)
    return rested_at


def release_brake_in_place(test_case: BaseYdriveTest) -> None:
    """Hand a stopped load back to the controller without moving it.

    release_brake() arms before powering the rail, which is safe only while the
    setpoint matches the axis. After a brake stop it does not - the last command was
    the far end of the stroke - so arming would lunge for it. The setpoint is parked
    at the current position first. Not a step: publishing itself as `current_step`
    would bury whichever step called it."""
    testbed: YdriveTestbed = test_case.testbed
    held_at = testbed.get_pos_estimate()
    testbed.command.set_position(held_at)
    test_case.set_state("position_target", held_at)
    release_brake(test_case)


@step
def dwell_braked(test_case: BaseYdriveTest, dwell_s: float) -> None:
    """Hold the load on the brake for `dwell_s`, with the axis idle.

    The brake is magnet-applied, so holding costs no coil power, and an idled axis
    draws no current - nothing dissipates, which is what a thermal reading recovers
    over. Not unwound in a `finally`: if wait_for() raises, the load should stay
    where the brake has it."""
    engage_brake(test_case)
    test_case.wait_for(dwell_s)
    release_brake(test_case)


def establish_origin_by_hand(test_case: BaseYdriveTest) -> float:
    """Hand the load to a person, have them put it at the end of the stroke the brake
    should stop it toward, and make where they left it position 0. Returns that
    origin, in turns.

    THE LOAD IS HELD BY NOTHING while the operator works: the brake is released and
    then the axis idled, so it is free to push by hand. Safe on this stand because
    the axis is not gravity-loaded - on one that was, this is the state where the
    load falls. It is still held by nothing when this returns, so the caller takes
    it back with release_brake_in_place() before commanding anything.

    Rezeroing is in software: the device is not zeroed, because there is no command
    for that in the declared channel set, so the offset is published as
    `position_origin` instead. Without it a stored run's absolute positions cannot
    be interpreted, since they are relative to wherever a person happened to stop.

    Not a @step: await_operator() is one, and a step that contains another reports
    twice for one action."""
    release_brake(test_case)
    test_case.testbed.command.set_axis_state("IDLE")
    await_operator(
        test_case,
        "move the load by hand to the end of the stroke it should brake TOWARD "
        "(this becomes position 0), then acknowledge",
    )
    origin = test_case.testbed.get_pos_estimate()
    test_case.set_state("position_origin", origin)
    logger.info("test %s: position origin set at %.3f turns", test_case.test_id, origin)
    return origin


@step
def move_to(
    test_case: BaseYdriveTest,
    target: float,
    position_tolerance: float = DEFAULT_POSITION_TOLERANCE,
    velocity_tolerance: float = DEFAULT_VELOCITY_TOLERANCE,
    arrival_timeout_s: float = DEFAULT_ARRIVAL_TIMEOUT_S,
) -> float:
    """Command one target position, block until arrived and settled, and return the
    FURTHEST position reached along the way, in the direction of travel.

    THE FURTHEST POSITION, NOT THE ONE ARRIVAL WAS ACCEPTED AT, and on an
    overshooting stand they are not the same. An overshoot peak wider than
    `position_tolerance` cannot satisfy arrival, so the load is accepted on the way
    back and the accepted frame sits near the target with the excursion past it
    already behind. Returning the peak instead is what lets a caller measure
    distance: between two consecutive peaks the load moves monotonically, so the
    gap between them is the path it took rather than the stroke it was asked for.

    IT EQUALS THE PATH ONLY WHEN THE LOAD REVERSES OUTSIDE `position_tolerance`.
    That is this stand's regime - it overshoots by more than the tolerance, so the
    peak cannot satisfy arrival and is always seen. A load whose overshoot fits
    inside the tolerance is accepted on the way in instead, before it reverses, and
    the excursion after that frame is neither reported nor tracked: the result is an
    under-count, never an over-count, and nothing signals the change. A lighter load
    or a different DUT is where that happens.

    A peak is a good place to read a position: the load is reversing there, so the
    telemetry stream's ~79 ms frame period costs millimetres - at cruise the same
    frame period is worth over 100 mm."""
    testbed: YdriveTestbed = test_case.testbed
    testbed.command.set_position(target)
    test_case.set_state("position_target", target)
    deadline: Stopwatch = Stopwatch(duration_s=arrival_timeout_s)
    # Tracked from the first frame of the move rather than from a read before it,
    # so learning which way the load is going costs no extra telemetry frame.
    furthest: Optional[float] = None
    outward: float = 0.0
    # Closed-loop: block on live readings until we've actually arrived AND
    # settled (not just passing through the target while still moving). Each
    # get_motion() call blocks on the next telemetry frame, so this loop is
    # naturally paced by the stream rather than a hot spin.
    while True:
        test_case.check_should_continue()
        # One frame for all of it, so "arrived AND settled" is judged at a single
        # moment rather than from instants a frame apart - see Motion.
        motion = testbed.get_motion()
        _require_still_driving(test_case, motion, f"moving to {target}")
        if furthest is None:
            furthest = motion.position
            outward = 1.0 if target >= motion.position else -1.0
        elif (motion.position - furthest) * outward > 0:
            furthest = motion.position
        within_tolerance: bool = (
            abs(motion.position - target) <= position_tolerance
            and abs(motion.velocity) <= velocity_tolerance
        )
        if within_tolerance:
            return furthest
        if deadline.expired:
            raise TimeoutError(
                f"test {test_case.test_id}: position didn't settle at {target} within {arrival_timeout_s}s"
            )


def _await_axis_armed(test_case: BaseYdriveTest, armed: bool, timeout_s: float) -> None:
    """Block until `axis_is_armed` reads `armed`, or raise.

    Requesting an axis state only writes `requested_state`, and the ODrive can
    decline it - a latched error refuses CLOSED_LOOP_CONTROL - so both brake
    transitions wait for the axis to report it rather than assuming. Paced by the
    telemetry stream, polling check_should_continue() throughout. The timeout
    diagnostic comes from describe_errors(), so its values share one instant."""
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
    """Engage the brake, then idle the axis, confirming it idled.

    The brake grabs first, so the load is held before the controller lets go; the
    reverse leaves it held by nothing for the settle time. A braked axis must not be
    armed - the controller would hold position against a locked output, and any
    position error becomes torque into a mechanical stop.

    Raises if the axis does not idle, which means the controller is still driving
    against an engaged brake; the brake is holding by then, so raising is safe. The
    settle wait goes through wait_for(), which keeps polling rather than sleeping
    blind."""
    testbed: YdriveTestbed = test_case.testbed
    testbed.power_brake_bus(False)
    test_case.wait_for(BRAKE_SETTLE_S)
    testbed.command.set_axis_state("IDLE")
    _await_axis_armed(test_case, armed=False, timeout_s=arm_timeout_s)
    test_case.set_state("brake_engaged", True)


def release_brake(test_case: BaseYdriveTest, arm_timeout_s: float = DEFAULT_ARM_TIMEOUT_S) -> None:
    """Arm the axis, confirm it armed, and only then release the brake - the inverse
    of engage_brake().

    The controller takes hold before the brake lets go, so the load is never unheld.
    Safe only while the position setpoint still matches the axis, which holds when
    the last move left `input_pos` where the axis is dwelling - see
    release_brake_in_place() for when it does not.

    The confirmation is the point: arming is asynchronous and can be declined, so
    releasing on the strength of having asked would drop the load onto a controller
    that never took it. Returns once the brake has had time to let go."""
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
    """One low<->high cycle, the brake holding each dwell with the axis idled.

    The brake engages only once move_to() reports arrived and settled, and releases
    before the next move: engaging mid-move brakes a moving axis, and moving before
    the brake lets go drives into it. Across every transition the load is held by
    the controller, the brake, or both - never neither.

    An aborted dwell leaves the brake engaged and the axis idle.
    brake_during_dwell=False cycles without touching the brake or the axis state."""

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
        # at the one moment the reason for stopping is unknown, and the teardown
        # that follows only ever brings a MOVING load to rest - a load the brake is
        # already holding it leaves alone.
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

    In RAM every run - nothing here calls save_configuration() - so a run cannot
    leave a stand configured differently than it found it, at the cost of having to
    set them every time."""
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
