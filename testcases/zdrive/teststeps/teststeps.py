"""Test steps for zdrive.

Deliberately zdrive's own rather than shared with ydrive. The two stands overlap
heavily - both are an ODrive behind a bus with a magnet-applied brake - but the
overlap is in shape, not in numbers or in what is safe: zdrive is VERTICAL and
GRAVITY-LOADED where ydrive is not, and that changes which orderings are allowed.
Keeping them separate keeps each stand's logic answerable on its own.

THIS AXIS IS VERTICAL AND GRAVITY-LOADED. Nothing but the brake or the
controller holds the load, and with neither holding it descends. Two
consequences run through every step here:

  - the brake and the axis state always move together, controller-first on the
    way in and brake-first on the way out, so the load is never held by neither.
    engage_brake()/release_brake() are the only places that ordering is
    expressed.
  - releasing the brake with the axis IDLE is only safe at the bottom of the
    stroke, where the load is resting on its hard stop. There is exactly one
    step that does it, release_brake_for_positioning(), and it says so.

The travelling gear is light - around 20 lb - so a person can move it by hand at
the bottom, and the brake-only hold is holding that weight rather than a
substantial one.

POSITIONS ARE IN TURNS, not metres. This stand has no published metres-per-turn,
so nothing here converts; a distance is a number of turns and is named as such.
The stroke runs from 0 at the bottom, where the load bottoms out, to TOP_OF_STROKE
turns at the top - a NEGATIVE number, because up is negative on this drive.

prepare_for_operation: cold stand to ready-to-arm - bus up, faults cleared,
control and input mode set, tuning applied. Leaves the axis idle behind an
engaged brake.

set_tuning_params: the controller's velocity limit, filter bandwidth, gains,
spinout thresholds and overspeed tolerance - in RAM, so a run leaves the board's
saved configuration alone. The motor's current limits are NOT here: ZdriveTestbed
writes those, and one owner per setting is the point.

prompt_for_SN_ER_load: ask the operator which DUT, which ticket and what load,
and publish the answers so every recorded row carries them.

await_operator: wait for a person, polling for a fatal bound, a stop request and
a lost recorder throughout.

release_brake_for_positioning: the one step that leaves the load held by nothing.

move_to: commands one target position and blocks until arrived and settled.

hold_on_brake: the measurement. Engages the brake, idles the axis, holds for a
dwell, and reports how far the load moved while nothing but the brake held it.

engage_brake / release_brake / release_brake_in_place: the brake and the axis
state moved together, each confirming the axis reached the state it was asked
for. Not steps: they would bury their caller's `current_step`."""
from __future__ import annotations

import json
import logging
import time
from typing import Dict, NamedTuple, Optional, Sequence, Tuple

from hardware.odrive import odrive_errors
from testbeds.zdrive_testbed.zdrive_testbed import (
    BRAKE_SETTLE_S,
    METERS_PER_TURN,
    Motion,
    ZdriveTestbed,
)
from testcases.step import step
from testcases.zdrive.rulebooks.zdrive_rulebook import MAX_TEMPERATURE_C
from testcases.utils import Stopwatch, spawn_operator_prompt
from testcases.zdrive.testcases.base_zdrive_test import BaseZdriveTest

logger = logging.getLogger(__name__)

TOP_OF_STROKE = -55.0
"""The top of the usable stroke, in turns from the bottom. Negative because up is
negative on this drive."""

BOTTOM_OF_STROKE = 0.0
"""The bottom of the stroke, where the load rests on its hard stop. Also the
origin: every target is relative to wherever the operator leaves the load."""

DEFAULT_POSITION_TOLERANCE = 0.5  # turns
DEFAULT_VELOCITY_TOLERANCE = 0.05  # turns/s
DEFAULT_ARRIVAL_TIMEOUT_S = 30.0
"""How long a move may take. 55 turns at the 18 turns/s velocity limit is 3.1 s,
so this is generous enough for a slow load and short enough that a stalled axis
reports rather than hanging the run."""

TEARDOWN_POSITION_TOLERANCE = 1.0  # turns
"""How close to the bottom a teardown descent has to get before it is called
done. Looser than a move's tolerance: the point is that the load is resting on
its stop rather than suspended, not that it is precisely placed."""

TEARDOWN_DESCENT_TIMEOUT_S = 10.0
"""How long the teardown descent is attempted before everything is switched off
regardless of where the load got to.

An attempt, not a guarantee. The full stroke takes 3.1 s at the velocity limit,
so a healthy descent finishes well inside this; a descent that does not is one
where something is already wrong, and waiting longer on a stand nobody is
watching buys less than shutting it down does. However it ends, the brake is
engaged and the bus dropped - so a load that did not make it to the bottom is
left held by the brake, which is where it would have been anyway."""

DEFAULT_STOP_TIMEOUT_S = 10.0
"""How long the load may still be moving after the brake was commanded before the
run gives up on it stopping. Generous against BRAKE_SETTLE_S plus a decelerating
load, and short enough that a brake which never bit is reported rather than
waited on."""

DEFAULT_BRAKE_BACKSTOP_TURNS = 20.0
"""How close to the target the load may get before the brake is dropped whatever
speed it is doing.

A stroke limit, not a timing one. With the axis idle there is no controller to
abort with, so this is the only thing between a load that never reaches its
trigger speed and the hard stop at the bottom. Measured stops from 60 turns/s run
to about 11 turns, so this leaves most of a stop's worth of margin again."""

DEFAULT_POST_BRAKE_REST_S = 5.0
"""How long the brake keeps what it stopped before the stopping distance is taken,
so creep counts against that distance. Nothing drives across it, so any movement
is the brake giving way rather than the axis being commanded."""

DEFAULT_ARM_TIMEOUT_S = 3.0
"""How long a brake transition waits for the axis to report the state it asked
for. Under TelemetryClient's 5 s staleness deadline, or a silent stream raises
first."""

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

OPERATOR_POLL_INTERVAL_S = 0.1
"""How often an operator-gated wait re-checks - slower than Stopwatch's tick
because a person is what is being waited on. Every tick still runs the abort
checks."""

VELOCITY_LIMIT = 18.0  # turns/s
FILTER_BW = 20.0  # 1/s
POSITION_GAIN = 32.0
VELOCITY_GAIN = 0.8
VELOCITY_INTEGRATOR = 0.4
SPINOUT_MECHANICAL_THRESHOLD = -50.0  # W
SPINOUT_ELECTRICAL_THRESHOLD = 50.0  # W
"""The tuning this stand runs under, as the board is configured today."""

VELOCITY_INTEGRATOR_LIMIT = 10.0  # Nm
"""Ceiling on the torque the velocity loop's integrator alone may command.

THE BOARD SHIPS THIS AT INFINITY, which on a gravity-loaded axis means the one
term that has to carry the load's weight is also the one term with no bound. The
integrator is not optional here: at rest the velocity error is zero, so the
proportional terms contribute nothing and holding the load is entirely the
integrator's job.

Sized from what holding actually costs rather than from the current limit.
Measured at 1000 lb stationary, the axis draws 32 A - 8.1 Nm - of which the
integrator carries 6.9 Nm. This is about 45% above that, so a heavier hold or a
worse spot in the stroke still has room.

Deliberately below the soft current limit's torque equivalent, which at 55 A and
this motor's 0.2506 Nm/A is 13.8 Nm: the integrator alone can reach roughly 40 A,
so it can no longer saturate the current limit by itself. What it does NOT bound
is the proportional path - a runaway the velocity error is fighting reaches the
limit through vel_gain, with the integrator near zero."""

VELOCITY_LIMIT_TOLERANCE = 1.5
"""Multiple of the velocity limit at which the axis raises an overspeed error, so
the trip sits at 27 turns/s against an 18 turns/s limit. Tighter than the board's
default of 2.0: on a gravity-loaded axis an overspeed is the load running away,
and there is less of the stroke left by the time a wider tolerance notices."""


def _clear_faults(test_case: BaseZdriveTest, timeout_s: float) -> None:
    """Clear the ODrive's latched errors and confirm they cleared.

    Retried rather than done once: below the board's under-voltage trip level a
    clear succeeds and DC_BUS_UNDER_VOLTAGE re-latches, so retrying waits out the
    bus ramp without this step inventing a voltage threshold. Raises with the
    remaining faults decoded - an error that will not clear is one the axis will
    refuse to arm with."""
    testbed: ZdriveTestbed = test_case.testbed
    deadline = Stopwatch(duration_s=timeout_s)
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


def _explain_unclearable(remaining: Dict[str, str]) -> str:
    """Split the remaining faults into what clear_errors resets and what it never
    could.

    A latched register still set means clearing did not take; a live condition
    means clearing was never the answer. Undistinguished, both read as "retry"."""
    latched = {n: t for n, t in remaining.items() if n in odrive_errors.LATCHED_CHANNELS}
    conditions = {n: t for n, t in remaining.items() if n in odrive_errors.CONDITION_CHANNELS}
    parts = []
    if latched:
        parts.append(f"still latched after being cleared: {latched}")
    if conditions:
        parts.append(
            f"conditions clear_errors cannot clear: {conditions} - these describe the board now, "
            "so the cause has to change: the bus being up, the encoder cabling, or which encoder "
            "axis0.config.load_encoder/commutation_encoder is set to read"
        )
    other = {k: v for k, v in remaining.items() if k not in latched and k not in conditions}
    if other:
        parts.append(f"other: {other}")
    return "; ".join(parts)


def _require_still_driving(test_case: BaseZdriveTest, motion: Motion, doing: str) -> None:
    """Raise if the axis has stopped driving when it should be.

    The ODrive disarms itself on a fault and tells nobody, so a loop watching for
    a position keeps waiting while the load coasts - and on this axis coasting
    means descending. Failing here costs one frame, and teardown then engages the
    brake."""
    if motion.armed:
        return
    raise RuntimeError(
        f"test {test_case.test_id}: the axis stopped driving while {doing} - it disarmed itself "
        f"at {motion.position:.2f} turns doing {motion.velocity:.2f} turns/s. "
        f"{test_case.testbed.describe_errors()}"
    )


def _await_axis_armed(test_case: BaseZdriveTest, armed: bool, timeout_s: float) -> None:
    """Block until `axis_is_armed` reads `armed`, or raise.

    Requesting an axis state only writes `requested_state`, and the ODrive can
    decline it - a latched error refuses CLOSED_LOOP_CONTROL - so both brake
    transitions wait for the axis to report it rather than assuming."""
    testbed: ZdriveTestbed = test_case.testbed
    deadline = Stopwatch(duration_s=timeout_s)
    while True:
        test_case.check_should_continue()
        if testbed.get_axis_armed_status() == armed:
            return
        if deadline.expired:
            raise RuntimeError(
                f"test {test_case.test_id}: axis did not "
                f"{'arm' if armed else 'idle'} within {timeout_s}s - {testbed.describe_errors()}"
            )


@step
def prepare_for_operation(
    test_case: BaseZdriveTest,
    control_mode: str = DEFAULT_CONTROL_MODE,
    clear_timeout_s: float = DEFAULT_CLEAR_TIMEOUT_S,
) -> None:
    """Bring the stand from cold to ready-to-arm: bus up, no latched faults,
    control and input mode set, tuning applied.

    Order matters. The bus is energized first, because the ODrive latches
    DC_BUS_UNDER_VOLTAGE while unpowered; clearing then runs against a live bus
    and is confirmed, since a latched error is enough for the board to refuse
    CLOSED_LOOP_CONTROL. The input mode is set as well as the control mode - a
    stand left in VEL_RAMP ignores commanded positions entirely.

    The only thing on this stand that energizes the motor bus, so a test that
    never calls it leaves the stand cold.

    Does NOT arm the axis or touch the brake: the load stays held by the brake
    until a release hands it over."""
    testbed: ZdriveTestbed = test_case.testbed
    testbed.power_motor_bus(True)
    _clear_faults(test_case, clear_timeout_s)
    testbed.command.set_control_mode(control_mode)
    testbed.command.set_controller_config_input_mode(INPUT_MODE_POS_FILTER)
    _apply_tuning_params(test_case)


@step
def set_tuning_params(
    test_case: BaseZdriveTest,
    velocity_limit: float = VELOCITY_LIMIT,
    filter_bw: float = FILTER_BW,
    position_gain: float = POSITION_GAIN,
    velocity_gain: float = VELOCITY_GAIN,
    velocity_integrator: float = VELOCITY_INTEGRATOR,
    spinout_mechanical_threshold: float = SPINOUT_MECHANICAL_THRESHOLD,
    spinout_electrical_threshold: float = SPINOUT_ELECTRICAL_THRESHOLD,
    velocity_integrator_limit: float = VELOCITY_INTEGRATOR_LIMIT,
    velocity_limit_tolerance: float = VELOCITY_LIMIT_TOLERANCE,
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
        velocity_integrator_limit,
        velocity_limit_tolerance,
    )


def _apply_tuning_params(
    test_case: BaseZdriveTest,
    velocity_limit: float = VELOCITY_LIMIT,
    filter_bw: float = FILTER_BW,
    position_gain: float = POSITION_GAIN,
    velocity_gain: float = VELOCITY_GAIN,
    velocity_integrator: float = VELOCITY_INTEGRATOR,
    spinout_mechanical_threshold: float = SPINOUT_MECHANICAL_THRESHOLD,
    spinout_electrical_threshold: float = SPINOUT_ELECTRICAL_THRESHOLD,
    velocity_integrator_limit: float = VELOCITY_INTEGRATOR_LIMIT,
    velocity_limit_tolerance: float = VELOCITY_LIMIT_TOLERANCE,
) -> None:
    """Write the controller configuration this stand runs under.

    In RAM every run - nothing here calls save_configuration() - so a run cannot
    leave the stand configured differently than it found it, at the cost of
    having to set them every time.

    The motor's current limits are ZdriveTestbed's, written in its start(), and
    are not touched here: two writers for one setting is how a stand ends up
    running under limits nobody declared."""
    testbed: ZdriveTestbed = test_case.testbed
    testbed.command.set_controller_config_vel_limit(velocity_limit)
    testbed.command.set_controller_config_vel_limit_tolerance(velocity_limit_tolerance)
    testbed.command.set_controller_config_input_filter_bandwidth(filter_bw)
    testbed.command.set_controller_config_pos_gain(position_gain)
    testbed.command.set_controller_config_vel_gain(velocity_gain)
    testbed.command.set_controller_config_vel_integrator_gain(velocity_integrator)
    testbed.command.set_controller_config_vel_integrator_limit(velocity_integrator_limit)
    testbed.command.set_controller_config_spinout_mechanical_power_threshold(
        spinout_mechanical_threshold
    )
    testbed.command.set_controller_config_spinout_electrical_power_threshold(
        spinout_electrical_threshold
    )


def _await_ack(
    test_case: BaseZdriveTest,
    instruction: str,
    fields: Sequence[str] = (),
    choices: Optional[Dict[str, Sequence[str]]] = None,
) -> str:
    """Publish an instruction, wait for the operator's marker, and return its
    contents - empty for a plain acknowledgement, JSON when values were asked for.

    A window opens with a button (tools/operator_prompt.py), and
    `python -m tools.operator_ack` leaves the same marker from a terminal, which
    is what a stand with no display uses. Either way this polls for the marker
    file rather than blocking on input(), so every tick still runs
    check_should_continue() - a fatal bound, a stop request or a lost recorder
    ends the run instead of waiting on someone who may have walked away. On this
    axis that matters more than most: one of the steps that precedes an operator
    prompt here is the one that leaves the load held by nothing.

    The instruction is published as `operator_prompt` and cleared afterwards, so
    a recorded run shows what it was waiting for rather than looking like a
    hang."""
    path = test_case.operator_ack_path()
    path.unlink(missing_ok=True)  # a stale ack from an earlier run must not skip this
    test_case.set_state("operator_prompt", instruction)
    logger.warning("test %s: WAITING FOR OPERATOR - %s", test_case.test_id, instruction)
    logger.warning("test %s: click the window, or `python -m tools.operator_ack`", test_case.test_id)

    window = spawn_operator_prompt(test_case.test_id, instruction, fields, choices)
    clock = Stopwatch()
    try:
        while not path.exists():
            test_case.check_should_continue()
            time.sleep(OPERATOR_POLL_INTERVAL_S)
        answered = path.read_text()
        path.unlink(missing_ok=True)
        logger.info(
            "test %s: operator acknowledged after %.0fs", test_case.test_id, clock.elapsed_s(),
        )
        return answered
    finally:
        test_case.set_state("operator_prompt", None)
        # However this ended, the window is asking for something nobody is
        # waiting for any more, and a stale one left on a stand's screen is worse
        # than none.
        if window is not None:
            window.terminate()


@step
def await_operator(test_case: BaseZdriveTest, instruction: str) -> None:
    """Block until a person acknowledges `instruction`. See _await_ack for the
    wait itself."""
    _await_ack(test_case, instruction)


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
    test_case: BaseZdriveTest, fields: Sequence[RunDetail]
) -> Dict[str, str]:
    """Ask the operator for the details that identify this run, and publish them.

    A field with choices is a dropdown and its answer is checked against them - the
    window cannot produce anything else, but `tools.operator_ack --answer` can.

    Published as run state, so the engine merges them into every recorded row. The
    channels have to be seeded (../channels.py) or the engine fixes its header
    before they exist and drops them. Asked before anything is energized, and
    before the brake is released: it needs a person and does not need the stand,
    and on this axis the release is the moment the load is held by nothing."""
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


def engage_brake(test_case: BaseZdriveTest, arm_timeout_s: float = DEFAULT_ARM_TIMEOUT_S) -> None:
    """Engage the brake, then idle the axis, confirming it idled.

    The brake grabs first, so the load is held before the controller lets go; the
    reverse leaves it held by nothing, and on this axis that means descending. A
    braked axis must not be armed - the controller would hold position against a
    locked output, and any position error becomes torque into a mechanical stop.

    Raises if the axis does not idle, which means the controller is still driving
    against an engaged brake; the brake is holding by then, so raising is safe."""
    testbed: ZdriveTestbed = test_case.testbed
    testbed.power_brake_bus(False)
    test_case.wait_for(BRAKE_SETTLE_S)
    testbed.command.set_axis_state("IDLE")
    _await_axis_armed(test_case, armed=False, timeout_s=arm_timeout_s)
    test_case.set_state("brake_engaged", True)


def release_brake(test_case: BaseZdriveTest, arm_timeout_s: float = DEFAULT_ARM_TIMEOUT_S) -> None:
    """Arm the axis, confirm it armed, and only then release the brake - the
    inverse of engage_brake().

    The controller takes hold before the brake lets go, so the load is never
    unheld. Safe only while the position setpoint still matches the axis, which
    holds when the last move left `input_pos` where the axis is dwelling - see
    release_brake_in_place() for when it does not.

    The confirmation is the point: arming is asynchronous and can be declined, so
    releasing on the strength of having asked would drop the load onto a
    controller that never took it."""
    testbed: ZdriveTestbed = test_case.testbed
    testbed.command.set_axis_state("CLOSED_LOOP_CONTROL")
    _await_axis_armed(test_case, armed=True, timeout_s=arm_timeout_s)
    testbed.power_brake_bus(True)
    test_case.wait_for(BRAKE_SETTLE_S)
    test_case.set_state("brake_engaged", False)


def release_brake_in_place(test_case: BaseZdriveTest) -> None:
    """Hand a held load back to the controller without moving it.

    release_brake() arms before powering the rail, which is safe only while the
    setpoint matches the axis. After the brake has held the load it may not - if
    the brake crept or slipped, the axis is no longer where the last move left
    the setpoint - so the setpoint is parked at the current position first.
    Arming to a stale setpoint would lunge for it.

    That case is not hypothetical here: measuring brake slip is what this stand's
    hold step does, so the load having moved is an expected outcome rather than a
    fault."""
    testbed: ZdriveTestbed = test_case.testbed
    held_at = testbed.get_pos_estimate()
    testbed.command.set_position(held_at)
    test_case.set_state("position_target", held_at)
    release_brake(test_case)


def release_brake_for_positioning(test_case: BaseZdriveTest) -> None:
    """Release the brake and idle the axis, leaving the load held by NOTHING, so a
    person can move it by hand.

    THE ONLY SAFE PLACE TO CALL THIS IS THE BOTTOM OF THE STROKE, where the load
    is resting on its hard stop and has nowhere to descend to. Called anywhere
    above it, the load goes down under its own weight the moment the controller
    lets go.

    The handover still goes controller-first - arm, release the brake, then idle -
    so the brake is never the thing that lets go of a load the controller has not
    taken. The unheld state is the last step, and deliberate."""
    release_brake(test_case)
    test_case.testbed.command.set_axis_state("IDLE")
    _await_axis_armed(test_case, armed=False, timeout_s=DEFAULT_ARM_TIMEOUT_S)


def establish_origin_at_bottom(test_case: BaseZdriveTest) -> float:
    """Hand the load to a person, have them put it on its stop, and make where they
    left it position 0. Returns that origin, in turns.

    THE LOAD IS HELD BY NOTHING while the operator works - see
    release_brake_for_positioning(), which is safe only at the bottom of the stroke
    because the load has nowhere left to descend to. That is the whole reason this
    step exists at the bottom rather than wherever a test would prefer to start.

    Rezeroing is in software: the device is not zeroed, because there is no command
    for that in the declared channel set, so the offset is published as
    `position_origin` instead. Without it a stored run's absolute positions cannot
    be interpreted, since they are relative to wherever a person happened to stop.

    Not a @step: await_operator() is one, and a step that contains another reports
    twice for one action."""
    release_brake_for_positioning(test_case)
    await_operator(
        test_case,
        "move the drive to the BOTTOM of the stroke, where the load rests on its stop "
        "(this becomes position 0), then acknowledge",
    )
    origin = test_case.testbed.get_pos_estimate()
    test_case.set_state("position_origin", origin)
    logger.info("test %s: position origin set at %.3f turns", test_case.test_id, origin)
    return origin


@step
def move_to(
    test_case: BaseZdriveTest,
    target: float,
    position_tolerance: float = DEFAULT_POSITION_TOLERANCE,
    velocity_tolerance: float = DEFAULT_VELOCITY_TOLERANCE,
    arrival_timeout_s: float = DEFAULT_ARRIVAL_TIMEOUT_S,
) -> None:
    """Command one target position and block until arrived AND settled.

    Both conditions from one frame, so "arrived and settled" is judged at a single
    moment rather than from instants a frame apart. Each read blocks on the next
    telemetry frame, so the loop is paced by the stream rather than spinning."""
    testbed: ZdriveTestbed = test_case.testbed
    testbed.command.set_position(target)
    test_case.set_state("position_target", target)
    deadline = Stopwatch(duration_s=arrival_timeout_s)
    while True:
        test_case.check_should_continue()
        motion = testbed.get_motion()
        _require_still_driving(test_case, motion, f"moving to {target:.2f} turns")
        if (
            abs(motion.position - target) <= position_tolerance
            and abs(motion.velocity) <= velocity_tolerance
        ):
            return
        if deadline.expired:
            raise TimeoutError(
                f"test {test_case.test_id}: position did not settle at {target:.2f} turns within "
                f"{arrival_timeout_s}s - reached {motion.position:.2f} doing "
                f"{motion.velocity:.2f} turns/s"
            )


@step
def brake_from_speed(
    test_case: BaseZdriveTest,
    target: float,
    trigger_speed: float,
    backstop_turns: float = DEFAULT_BRAKE_BACKSTOP_TURNS,
    stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S,
    rest_s: float = DEFAULT_POST_BRAKE_REST_S,
    velocity_tolerance: float = DEFAULT_VELOCITY_TOLERANCE,
    arm_timeout_s: float = DEFAULT_ARM_TIMEOUT_S,
) -> float:
    """Let the load fall, and stop it with the brake once it reaches
    `trigger_speed` turns/s. Returns the stopping distance in millimetres.

    THE CONTROLLER IS NOT IN THIS LOOP. The axis stays idle throughout, so the
    load is accelerating under its own weight and nothing but the brake will stop
    it. That is the measurement, and it is also what keeps the axis out of a fight
    it cannot win: asked to hold a descent this axis runs away, and the velocity
    error then commands more current than the limit allows until the firmware
    disarms - which is a harder stop from a higher speed than this step's own
    trigger would have taken.

    ENTERED BRAKED AND IDLE, as hold_on_brake() leaves the stand. The idle is
    confirmed before the rail is released, because releasing the brake while the
    axis is armed hands the load to a controller whose setpoint is wherever the
    last move left it.

    THE BRAKE CLOSES ON THE WAY OUT NO MATTER WHAT. Once the rail is released the
    load is held by nothing, so the rail is dropped in a `finally` - a fatal
    bound, a stop request or a lost recorder during the fall must not leave a
    falling load behind.

    BOUNDED BY POSITION AS WELL AS BY SPEED. `backstop_turns` from `target` the
    brake is dropped regardless of how fast the load is going, because with the
    axis idle there is no controller authority to abort with and the stroke is
    finite. Reaching it means the load never got to `trigger_speed`, which is
    logged rather than raised: the brake still stopped it, and the speed it
    engaged at is recorded either way.

    STOPPING DISTANCE IS EVERYTHING AFTER THE BRAKE IS COMMANDED: the coast
    before it bites, the deceleration, and any creep across `rest_s`. It is
    baselined on the first frame after the rail is dropped, which still precedes
    physical engagement - that is up to BRAKE_SETTLE_S later and unobservable
    from here. `brake_speed_m_s` comes off that same frame, so the speed
    recorded is the one the brake saw."""
    testbed: ZdriveTestbed = test_case.testbed

    # Never release the brake onto an armed axis: its setpoint is the last move's
    # target, and it would lunge for it.
    _await_axis_armed(test_case, armed=False, timeout_s=arm_timeout_s)

    started_at = testbed.get_motion().position
    test_case.set_state("position_target", target)
    testbed.power_brake_bus(True)
    test_case.set_state("brake_engaged", False)

    peak_speed = 0.0
    reached_trigger = False
    fell_to = started_at
    try:
        while True:
            test_case.check_should_continue()
            motion = testbed.get_motion()
            peak_speed = max(peak_speed, abs(motion.velocity))
            fell_to = motion.position
            if abs(motion.velocity) >= trigger_speed:
                reached_trigger = True
                break
            if abs(motion.position - target) <= backstop_turns:
                break
    finally:
        # The load is falling and only this stops it - see this step's docstring.
        testbed.power_brake_bus(False)
        test_case.set_state("brake_engaged", True)

    if not reached_trigger:
        # Reported from the loop's last frame rather than by reading another one:
        # every get_motion() blocks for a fresh frame, and a frame spent here is
        # travel charged to the brake that it had not been asked for yet.
        travelled = abs(fell_to - started_at)
        logger.warning(
            "test %s: the load reached the %.1f-turn backstop without ever doing "
            "%.2f turns/s - it peaked at %.2f turns/s over %.1f turns (%.3f m). The brake "
            "was dropped on position instead, so this cycle's engagement speed is not the "
            "one that was asked for",
            test_case.test_id, backstop_turns, trigger_speed, peak_speed,
            travelled, travelled * METERS_PER_TURN,
        )

    # The baseline for everything below, taken as one frame so the speed recorded
    # and the position it was reached at describe the same instant.
    braked_from = testbed.get_motion()

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
                f"{abs(motion.position - braked_from.position) * METERS_PER_TURN:.3f} m"
            )

    # The brake keeps what it stopped, and only then is the distance taken - see
    # DEFAULT_POST_BRAKE_REST_S. Nothing here touches the rail or the axis: they
    # were left where they should be above.
    test_case.wait_for(rest_s)
    rested_at = testbed.get_motion().position

    stopping_distance_m = abs(rested_at - braked_from.position) * METERS_PER_TURN
    test_case.set_state("brake_speed_m_s", abs(braked_from.velocity) * METERS_PER_TURN)
    test_case.set_state("stopping_distance_m", stopping_distance_m)
    return stopping_distance_m


@step
def hold_on_brake(test_case: BaseZdriveTest, hold_s: float, origin: float = 0.0) -> float:
    """Hold the load on the brake alone for `hold_s`, and report how far it moved, in metres.

    The measurement this stand exists to take. The axis is idled, so for the whole
    dwell the only thing opposing the load's weight is the brake, and any movement
    is the brake giving way rather than the controller yielding.

    Returns the slip in metres, signed the way the stroke is: on this drive up is
    negative, so a load that descends slips POSITIVE. Published as `brake_slip_m` so
    it lands in the recorded run rather than only in a log line.

    A MICRON IS THE FLOOR, NOT THE MEASUREMENT. Over 73 holds of 5 s at 1000 lb the
    recorded slip was +/-0.000001 m, which is one encoder count: this brake did not
    measurably give way. A bound on this catches a brake that has let go, not one
    that is wearing.

    THE LOG LINE IS RELATIVE TO `origin`, the value establish_origin_at_bottom()
    returned. Positions on this stand are only meaningful against that origin,
    since the device is never zeroed and the raw estimate carries whatever offset
    the board woke up with - so a line reporting the raw number reads as a
    different height than the one the test asked for. The slip itself is a
    difference and is unaffected either way.

    Not unwound in a `finally`: if the wait raises, the load should stay where the
    brake has it rather than being handed to a controller nobody is watching."""
    testbed: ZdriveTestbed = test_case.testbed
    engage_brake(test_case)
    held_from = testbed.get_pos_estimate()
    test_case.wait_for(hold_s)
    held_to = testbed.get_pos_estimate()

    slip_m = (held_to - held_from) * METERS_PER_TURN
    test_case.set_state("brake_slip_m", slip_m)
    logger.info(
        "test %s: brake held %.1fs at %.3f turns, slipped %+.6f m to %.3f turns",
        test_case.test_id, hold_s, held_from - origin, slip_m, held_to - origin,
    )
    return slip_m


FET_WAIT_C = 70.0
"""FET temperature at or above which a cycle waits instead of lifting.

Fourteen degrees below the 83.96 C at which this board starts derating its own current
limit - measured off the drive, not a datasheet figure. A derate would reduce the
current available to a lift without announcing it, which changes what the test does
while the test goes on believing it did the same thing every cycle.

The ER-64 run peaked at 32.0 C, but it was armed 2.6% of the time behind a 300 s dwell.
A cycling hold is armed nearer half the time on the same 1000 lb load, so this threshold
is expected to do real work rather than sit unused."""

TC_HEADROOM_C = 5.0
"""How close a thermocouple may get to its own fatal bound before a cycle waits.

Against zdrive_rulebook's MAX_TEMPERATURE_C, so the wait tracks the bound rather than
restating it: move the bound and this moves with it. Five degrees is enough to stop a
cycle rather than the run - the alternative is a fatal bound firing on a stand that
would have cooled if anything had asked it to."""

THERMAL_WAIT_S = 60.0
"""How long to wait before re-reading, when anything is too hot to lift."""


def temperatures_need_a_wait(test_case: BaseZdriveTest) -> Optional[str]:
    """Whether anything on this stand is too hot to start another lift, and which thing.

    ONE PLACE THAT DECIDES, over all three sensors: the drive's own FET thermistor and
    both wired thermocouples. Returns a description of the hottest objection, or None to
    proceed. A caller waits and asks again.

    The two limits are expressed differently on purpose. The FET has its own absolute
    threshold, because what it is protecting against is the board silently derating. The
    thermocouples are compared against their own fatal bound less a margin, because what
    they are protecting against is that bound firing and ending the run - so the number
    that matters is the one already declared, not a second copy of it."""
    testbed: ZdriveTestbed = test_case.testbed
    fet = testbed.get_fet_temperature_c()
    test_case.set_state("fet_temperature_c", fet)
    if fet >= FET_WAIT_C:
        return f"the inverter FET is at {fet:.1f} C, at or above the {FET_WAIT_C:.0f} C ceiling"

    tc_ceiling = MAX_TEMPERATURE_C - TC_HEADROOM_C
    hot = {n: t for n, t in testbed.get_tc_temperatures_c().items() if t >= tc_ceiling}
    if hot:
        worst = max(hot, key=hot.get)
        return (
            f"thermocouple {worst} is at {hot[worst]:.1f} C, within {TC_HEADROOM_C:.0f} C "
            f"of its {MAX_TEMPERATURE_C:.0f} C fatal bound"
        )
    return None


def wait_for_thermal_headroom(test_case: BaseZdriveTest) -> int:
    """Block until nothing on the stand is too hot to lift, and report how long it took.

    Returns the number of THERMAL_WAIT_S waits, so a caller can record it. Unbounded,
    deliberately: a stand that cannot cool is one whose cycle rate has collapsed, and
    stopping the run over that would be worse than continuing slowly. What makes that
    survivable rather than silent is that the count is published - a run quietly
    producing a tenth of the cycles anyone expected looks exactly like a healthy run
    otherwise.

    Called with the load on its bottom stop, the brake engaged and the axis idle, which
    is the only state in this cycle where waiting an arbitrary length of time costs
    nothing and risks nothing."""
    waits = 0
    while True:
        objection = temperatures_need_a_wait(test_case)
        test_case.set_state("thermal_waits", waits)
        if objection is None:
            if waits:
                logger.info("test %s: cool enough to lift after %d wait(s)",
                            test_case.test_id, waits)
            return waits
        waits += 1
        logger.warning(
            "test %s: %s - holding at the bottom for %.0f s (wait %d)",
            test_case.test_id, objection, THERMAL_WAIT_S, waits,
        )
        test_case.set_state("thermal_waits", waits)
        test_case.wait_for(THERMAL_WAIT_S)


ARM_SETTLE_S = 0.5
"""How long to give the axis to act on a requested state during teardown, before
reading once whether it took."""


def lower_to_bottom_for_teardown(
    test_case: BaseZdriveTest,
    target: float,
    descent_s: float = TEARDOWN_DESCENT_TIMEOUT_S,
) -> None:
    """Command the load down to `target` and give it `descent_s` to get there.

    WHY THIS EXISTS. Every other ending leaves the load wherever it got to, held
    by the brake - and on this stand the brake is the component under test, so a
    run that dies at the top leaves a suspended load depending on the one thing
    being measured, with nobody watching. This puts it on its hard stop instead.

    AN ATTEMPT, NOT A GUARANTEE, and deliberately not a loop watching for
    arrival. It commands the descent, waits, and returns; the caller's next move
    is ZdriveTestbed.stop(), which engages the brake, idles the axis and drops the
    bus whatever happened. A load that did not make it down is left held by the
    brake - which is where it would have been without this at all, so the worst
    case is no worse and there is nothing for a loop to decide.

    Called through TestCase.teardown_step(), which logs rather than raises, so
    nothing here needs to catch: a failure part-way leaves the brake engaged by
    the teardown that follows.

    The one reading it does take is whether the axis armed. Releasing the brake
    on the strength of having *asked* would hand a gravity load to a controller
    that may have declined."""
    testbed: ZdriveTestbed = test_case.testbed

    held_at = testbed.get_pos_estimate()
    if abs(held_at - target) <= TEARDOWN_POSITION_TOLERANCE:
        logger.info(
            "test %s: load is already at the bottom (%.2f turns) - nothing to lower",
            test_case.test_id, held_at,
        )
        return

    logger.warning(
        "test %s: lowering the load from %.2f to %.2f turns before shutdown, so it is not left "
        "suspended on the brake", test_case.test_id, held_at, target,
    )
    # Park the setpoint where the axis actually is: after a hold it may have
    # crept, and arming to a stale setpoint would lunge for it.
    testbed.command.set_position(held_at)
    testbed.command.set_axis_state("CLOSED_LOOP_CONTROL")
    time.sleep(ARM_SETTLE_S)
    if not testbed.get_axis_armed_status():
        logger.error(
            "test %s: the axis would not arm, so the load stays where the brake has it, at "
            "%.2f turns", test_case.test_id, held_at,
        )
        return

    testbed.power_brake_bus(True)
    time.sleep(BRAKE_SETTLE_S)
    testbed.command.set_position(target)
    time.sleep(descent_s)
    # Best effort: an unusable position estimate must not replace this line with
    # a traceback, since this is the log a person reads to find out where the
    # load ended up. The shutdown that follows does not depend on knowing.
    try:
        ended_at = f"{testbed.get_pos_estimate():.2f} turns"
    except Exception as exc:
        ended_at = f"an unreadable position ({exc})"
    logger.info(
        "test %s: descent commanded and given %.0fs; the load is at %s and the stand is "
        "about to be shut down", test_case.test_id, descent_s, ended_at,
    )
