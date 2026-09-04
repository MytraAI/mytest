"""Test steps for xdeploy: homing, cycling moves, thermal gating and teardown.

POSITIONS ARE IN TURNS from the homed hard stop at 0, and every cycled position
is negative. Negative is deploy, which lifts the load; positive is retract, which
is the direction gravity pulls it. So FULL_DEPLOY is the most negative number here
- FULL_DEPLOY is the most negative number here because deploying lifts, not
because it is the smallest. The load is resting on the ground at FULL_RETRACT and stays there for
everything retract of it, which is why the cycle can dwell and park there. Steps
that wait for a person live in testcases/teststeps/operator.py.

THERE IS NO BRAKE. Between arming after homing and disarming in teardown the
controller is the only thing holding the load, so no step here idles the axis
except the two that mean to: home_axis(), which runs before anything is lifted,
and teardown, which runs after the load is back on the ground.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from hardware.odrive import odrive_errors
from testbeds.xdeploy_testbed.xdeploy_testbed import (
    Motion,
    ODRIVE_MOTOR_SOFT_MAX_A,
    XdeployTestbed,
)
from testcases.step import step
from testcases.utils import Stopwatch
from testcases.xdeploy.rulebooks.xdeploy_rulebook import MAX_TEMPERATURE_C
from testcases.xdeploy.testcases.base_xdeploy_test import BaseXdeployTest

logger = logging.getLogger(__name__)

HOME_POSITION = 0.0
"""The retract hard stop, and the zero home_axis() writes to the board. BEYOND
FULL_RETRACT: it is the homing reference, not an end of the working stroke, and
nothing but homing goes there."""

FULL_RETRACT = -20.0
"""Where a cycle starts and ends: load down on the ground, clear of the hard stop."""

FULL_DEPLOY = -110.0
"""The deployed end of the stroke, where the load is carried highest."""

HOMING_SPEED_TURNS_S = 5.0
"""Retract creep speed for homing. Positive, which is also downhill."""

HOMING_CURRENT_A = 5.0
"""Phase current ceiling while creeping into the hard stop, restored to
ODRIVE_MOTOR_SOFT_MAX_A once home is found."""

HOMING_STALL_VELOCITY_TURNS_S = 0.2
"""Speed below which the creep counts as stopped."""

HOMING_STALL_FRAMES = 5
"""Consecutive frames under HOMING_STALL_VELOCITY_TURNS_S that mean the stop."""

HOMING_STALL_GRACE_S = 1.0
"""Grace before stall counting starts.

Load-bearing: the axis is at rest the instant it arms, so without this the first
frames read as a stall and homing declares the stop wherever it happened to be.
It also covers the case of starting already against the stop, which reads as a
stall as soon as the grace is over - which is the right answer."""

HOMING_RUNAWAY_SPEED_TURNS_S = 2.0 * HOMING_SPEED_TURNS_S
"""Speed above which the creep is no longer creeping but falling.

THE CREEP RUNS DOWNHILL ON A REDUCED CURRENT LIMIT, and holding a lifted load
back needs the torque that lifted it - far more than HOMING_CURRENT_A allows. So
a run that starts with the load still up (a previous run killed by power loss
rather than by its teardown) saturates the limit and accelerates instead of
creeping. The axis stays armed throughout, so nothing else here would notice: the
stop would still be found, the board still zeroed, and an uncontrolled descent of
the full load recorded as a normal homing."""

HOMING_TIMEOUT_S = 15.0
"""How long the creep runs before a stop that was never found is reported. At
HOMING_SPEED_TURNS_S this covers 75 turns, against the ~20 to the stop."""

DEFAULT_POSITION_TOLERANCE = 0.5  # turns
DEFAULT_VELOCITY_TOLERANCE = 0.05  # turns/s

DEFAULT_ARRIVAL_TIMEOUT_S = 60.0
"""How long a move may take before a stalled axis is reported rather than waited
on. DELIBERATELY GENEROUS - no cycle on this stand has been timed, and a tight
timeout that is wrong ends endurance runs in the middle of the night. Tighten it
from measured cycle_time_s."""

DEFAULT_ARM_TIMEOUT_S = 3.0
"""How long arming waits for the axis to report the state it asked for."""

DEFAULT_CLEAR_TIMEOUT_S = 5.0
"""How long clear_faults keeps clearing before it gives up."""

CLEAR_SETTLE_S = 0.25
"""Seconds between clearing and reading, so the frame checked is a later one."""

CONTROL_MODE_POSITION = "POSITION_CONTROL"
CONTROL_MODE_VELOCITY = "VELOCITY_CONTROL"

INPUT_MODE_PASSTHROUGH = 1
"""ODrive InputMode.PASSTHROUGH, which is what a velocity command needs."""

INPUT_MODE_POS_FILTER = 3
"""ODrive InputMode.POS_FILTER. Set explicitly: a board left in VEL_RAMP accepts
every input_pos and ignores it, which surfaces as a stall rather than a mode."""

FET_WAIT_C = 70.0
"""FET temperature at or above which a cycle waits instead of moving, below the
point at which this board starts silently derating its own current limit."""

TC_HEADROOM_C = 5.0
"""How close a thermocouple may get to its own fatal bound before a cycle waits.
Stops a cycle rather than the run."""

THERMAL_WAIT_S = 60.0
"""How long to wait before re-reading, when anything is too hot to move."""

DEFAULT_DWELL_S = 2.0
"""How long the axis holds at FULL_RETRACT between cycles before the temperatures
are checked. The load is on the ground there, so it holds nothing up."""

TEARDOWN_POSITION_TOLERANCE = 1.0  # turns
"""How close to FULL_RETRACT teardown has to get before it is called done."""

TEARDOWN_RETURN_TIMEOUT_S = 20.0
"""How long the teardown move is given before the stand is shut down regardless.
An attempt, not a guarantee - see park_for_teardown()."""

ARM_SETTLE_S = 0.5
"""How long teardown gives the axis to act on a requested state before reading
once whether it took."""


@step
def clear_faults(
    test_case: BaseXdeployTest, timeout_s: float = DEFAULT_CLEAR_TIMEOUT_S
) -> None:
    """Clear the ODrive's latched errors and confirm they cleared, retrying until
    timeout_s so a bus still coming up is waited out rather than reported.

    All this stand can do from cold: there is no rail to energize, and the modes
    are set by the steps that need them - homing and cycling want different
    ones."""
    testbed: XdeployTestbed = test_case.testbed
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
    """Split remaining faults into what clear_errors resets and what it never
    could - a live condition means clearing was never the answer."""
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


def _require_still_driving(test_case: BaseXdeployTest, motion: Motion, doing: str) -> None:
    """Raise if the axis has stopped driving when it should be.

    The ODrive disarms itself on a fault and tells nobody, so a loop watching for
    a position keeps waiting while the load runs positive to the ground."""
    if motion.armed:
        return
    raise RuntimeError(
        f"test {test_case.test_id}: the axis stopped driving while {doing} - it disarmed itself "
        f"at {motion.position:.2f} turns doing {motion.velocity:.2f} turns/s. "
        f"{test_case.testbed.describe_errors()}"
    )


def _await_axis_armed(test_case: BaseXdeployTest, armed: bool, timeout_s: float) -> None:
    """Block until `axis_is_armed` reads `armed`, or raise. Requesting a state
    only writes requested_state, and the ODrive can decline it."""
    testbed: XdeployTestbed = test_case.testbed
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
def home_axis(test_case: BaseXdeployTest) -> float:
    """Creep into the retract hard stop, write it as position 0, and return what
    the axis read there beforehand.

    THE ONLY STEP THAT MAY RUN WITH THE LOAD OFF THE GROUND AND THE AXIS IDLE, at
    its start: it creeps in the direction gravity already pulls, so a lifted load
    is set down on the way. Idles before zeroing to stop the creep pushing on the
    stop - the rezero itself is impulse-free, since the firmware shifts input_pos
    and pos_setpoint with it."""
    testbed: XdeployTestbed = test_case.testbed

    testbed.command.set_motor_config_current_soft_max(HOMING_CURRENT_A)
    testbed.command.set_control_mode(CONTROL_MODE_VELOCITY)
    testbed.command.set_controller_config_input_mode(INPUT_MODE_PASSTHROUGH)
    # Armed at a standstill, then given the creep - arming to a stale input_vel
    # would start the move before this step is watching it.
    testbed.command.set_velocity(0.0)
    testbed.command.set_axis_state("CLOSED_LOOP_CONTROL")
    _await_axis_armed(test_case, armed=True, timeout_s=DEFAULT_ARM_TIMEOUT_S)

    started_at = testbed.get_pos_estimate()
    logger.info(
        "test %s: homing at %+.1f turns/s from %.2f turns, current limited to %.1f A",
        test_case.test_id, HOMING_SPEED_TURNS_S, started_at, HOMING_CURRENT_A,
    )
    testbed.command.set_velocity(HOMING_SPEED_TURNS_S)

    try:
        stopped_at = _creep_to_stop(test_case)
    finally:
        # Whatever happened, stop commanding a creep and let go of the stop.
        testbed.command.set_velocity(0.0)
        testbed.command.set_axis_state("IDLE")

    _await_axis_armed(test_case, armed=False, timeout_s=DEFAULT_ARM_TIMEOUT_S)
    testbed.command.set_pos_estimate(HOME_POSITION)
    testbed.command.set_motor_config_current_soft_max(ODRIVE_MOTOR_SOFT_MAX_A)
    logger.info(
        "test %s: homed - the stop read %.2f turns and is now %.1f, %.1f turns of creep",
        test_case.test_id, stopped_at, HOME_POSITION, stopped_at - started_at,
    )
    return stopped_at


def _creep_to_stop(test_case: BaseXdeployTest) -> float:
    """Block until the creep has stalled against the stop, and return where.

    Not a @step: home_axis() is one, and a step inside a step reports twice."""
    testbed: XdeployTestbed = test_case.testbed
    deadline = Stopwatch(duration_s=HOMING_TIMEOUT_S)
    stall_grace = Stopwatch(duration_s=HOMING_STALL_GRACE_S)
    stalled_frames = 0
    while True:
        test_case.check_should_continue()
        motion = testbed.get_motion()
        _require_still_driving(test_case, motion, "creeping into the retract stop")
        # Positive is downhill, so a runaway is fast and positive.
        if motion.velocity > HOMING_RUNAWAY_SPEED_TURNS_S:
            raise RuntimeError(
                f"test {test_case.test_id}: the homing creep is running away - commanded "
                f"{HOMING_SPEED_TURNS_S:.1f} turns/s but the axis is doing {motion.velocity:.2f} "
                f"at {motion.position:.2f} turns. {HOMING_CURRENT_A:.1f} A cannot hold a lifted "
                "load back, so this is most likely a run starting with the load still up - it is "
                "descending to the ground under gravity, not creeping"
            )
        if stall_grace.expired:
            if abs(motion.velocity) < HOMING_STALL_VELOCITY_TURNS_S:
                stalled_frames += 1
                if stalled_frames >= HOMING_STALL_FRAMES:
                    return motion.position
            else:
                stalled_frames = 0
        if deadline.expired:
            raise TimeoutError(
                f"test {test_case.test_id}: no hard stop found in {HOMING_TIMEOUT_S}s of creep - "
                f"the axis is at {motion.position:.2f} turns still doing {motion.velocity:.2f} "
                "turns/s, so it is moving and nothing stopped it"
            )


@step
def prepare_to_cycle(test_case: BaseXdeployTest) -> None:
    """Put the drive in position control and arm it where it stands, ready for
    the first move.

    IN PLACE, AND THAT IS THE POINT: input_pos still holds whatever was last
    commanded before homing, which is not where the axis is, and arming to it
    would lunge for it."""
    testbed: XdeployTestbed = test_case.testbed
    testbed.command.set_control_mode(CONTROL_MODE_POSITION)
    testbed.command.set_controller_config_input_mode(INPUT_MODE_POS_FILTER)
    held_at = testbed.get_pos_estimate()
    testbed.command.set_position(held_at)
    test_case.set_state("position_target", held_at)
    testbed.command.set_axis_state("CLOSED_LOOP_CONTROL")
    _await_axis_armed(test_case, armed=True, timeout_s=DEFAULT_ARM_TIMEOUT_S)


@step
def move_to(
    test_case: BaseXdeployTest,
    target: float,
    position_tolerance: float = DEFAULT_POSITION_TOLERANCE,
    velocity_tolerance: float = DEFAULT_VELOCITY_TOLERANCE,
    arrival_timeout_s: float = DEFAULT_ARRIVAL_TIMEOUT_S,
) -> None:
    """Command one target position and block until arrived AND settled, both
    judged from a single frame so they describe one moment."""
    testbed: XdeployTestbed = test_case.testbed
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


def temperatures_need_a_wait(test_case: BaseXdeployTest) -> Optional[str]:
    """Whether anything is too hot to start another cycle, and which thing.

    One place that decides, over the drive's FET and every wired thermocouple.
    Returns the hottest objection, or None to proceed."""
    testbed: XdeployTestbed = test_case.testbed
    fet = testbed.get_fet_temperature_c()
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


@step
def wait_for_thermal_headroom(test_case: BaseXdeployTest) -> int:
    """Block until nothing is too hot to cycle, and report the waits this call made.

    The run's total is kept on the test case and published from here. Unbounded
    deliberately: a stand that cannot cool has a collapsed cycle rate rather than
    a failed run, and thermal_waits is what keeps that visible.

    CALLED AT FULL_RETRACT AND NOWHERE ELSE. The axis stays armed through the
    wait, but the load is on the ground there and the actuator is unloaded, so a
    wait of any length holds nothing up."""
    waits = 0
    while True:
        objection = temperatures_need_a_wait(test_case)
        if objection is None:
            if waits:
                logger.info("test %s: cool enough to cycle after %d wait(s)",
                            test_case.test_id, waits)
            return waits
        waits += 1
        # Banked on the test case as the wait begins, not by the caller once this
        # returns: check_should_continue() raises out of the wait below on a stop,
        # and a wait the verdict never heard about is one the run did not record.
        test_case.thermal_waits += 1
        test_case.set_state("thermal_waits", test_case.thermal_waits)
        logger.warning(
            "test %s: %s - holding at %.0f turns for %.0f s "
            "(wait %d of this cycle, %d in the run)",
            test_case.test_id, objection, FULL_RETRACT, THERMAL_WAIT_S, waits,
            test_case.thermal_waits,
        )
        test_case.wait_for(THERMAL_WAIT_S)


def cycle_position_forever(
    test_case: BaseXdeployTest, dwell_s: float = DEFAULT_DWELL_S
) -> None:
    """Cycle between FULL_RETRACT and FULL_DEPLOY until something stops the run,
    counting cycles and waiting at full retract whenever the stand is too hot.

    The endpoints are not parameters: they are stand geometry, and the caller
    positions the axis at FULL_RETRACT before this is called."""
    # Not a @step: move_to() and wait_for_thermal_headroom() are, and a step
    # containing another reports twice for one action.
    while True:
        clock = Stopwatch()
        move_to(test_case, FULL_DEPLOY)
        move_to(test_case, FULL_RETRACT)
        # Taken before the dwell and the thermal wait, so a cycle that had to
        # cool is still comparable with one that did not.
        cycle_time_s = clock.elapsed_s()
        test_case.set_state("cycle_time_s", cycle_time_s)

        test_case.wait_for(dwell_s)
        waits = wait_for_thermal_headroom(test_case)

        # The cycle is done: back at full retract, read, and cool enough to go
        # again. Published here so the count and the travel behind it describe
        # the same finished work.
        test_case.cycle_count += 1
        test_case.set_state("cycle_count", test_case.cycle_count)
        waited = f", after {waits} thermal wait(s)" if waits else ""
        logger.info(
            "test %s: cycle %d complete in %.1f s, %.1f turns travelled in all%s",
            test_case.test_id, test_case.cycle_count, cycle_time_s,
            test_case.total_travel_turns, waited,
        )


def park_for_teardown(
    test_case: BaseXdeployTest,
    target: float = FULL_RETRACT,
    return_timeout_s: float = TEARDOWN_RETURN_TIMEOUT_S,
) -> None:
    """Put the axis back at `target` and give it `return_timeout_s` to get there,
    so teardown does not disarm an axis that is holding the load up.

    AN ATTEMPT, NOT A GUARANTEE, and deliberately not a loop watching for
    arrival. What follows is XdeployTestbed.stop(), which disarms regardless -
    so a load that did not make it down is dropped from wherever it reached
    instead of from full deploy. Called through teardown_step(), which
    logs rather than raises."""
    testbed: XdeployTestbed = test_case.testbed

    held_at = testbed.get_pos_estimate()
    if abs(held_at - target) <= TEARDOWN_POSITION_TOLERANCE:
        logger.info(
            "test %s: load is already at %.2f turns with its weight on the ground - nothing "
            "to lower", test_case.test_id, held_at,
        )
        return

    logger.warning(
        "test %s: returning the load from %.2f to %.2f turns before shutdown, so it is not "
        "dropped from height", test_case.test_id, held_at, target,
    )
    # Park the setpoint where the axis actually is before arming: if the run died
    # mid-move the stale setpoint is somewhere else, and arming would lunge.
    testbed.command.set_control_mode(CONTROL_MODE_POSITION)
    testbed.command.set_controller_config_input_mode(INPUT_MODE_POS_FILTER)
    testbed.command.set_position(held_at)
    testbed.command.set_axis_state("CLOSED_LOOP_CONTROL")
    time.sleep(ARM_SETTLE_S)
    if not testbed.get_axis_armed_status():
        logger.error(
            "test %s: the axis would not arm, so the load stays at %.2f turns until the "
            "shutdown below drops it", test_case.test_id, held_at,
        )
        return

    testbed.command.set_position(target)
    time.sleep(return_timeout_s)
    # Best effort: an unusable position estimate must not replace this line with
    # a traceback, since this is the log a person reads to find where it ended up.
    try:
        ended_at = f"{testbed.get_pos_estimate():.2f} turns"
    except Exception as exc:
        ended_at = f"an unreadable position ({exc})"
    logger.info(
        "test %s: move commanded and given %.0fs; the load is at %s and the stand is about "
        "to be shut down", test_case.test_id, return_timeout_s, ended_at,
    )
