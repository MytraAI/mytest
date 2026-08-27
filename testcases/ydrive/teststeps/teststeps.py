"""Test steps for ydrive.

prepare_for_operation: cold stand to ready-to-arm - bus up, faults
cleared, control and input mode set, tuning applied. Leaves the axis
idle behind an engaged brake.

Steps that wait for a person - await_operator, prompt_for_run_details -
are not here: they are the same on every stand and live in
testcases/teststeps/operator.py.

brake_from_speed: runs up to a trigger speed, then idles the motor and
drops the brake rail so the brake stops a moving load. Records the speed
and the stopping distance, and returns where the load came to rest.

release_brake_in_place: hands a stopped load back to the controller
without moving it.

establish_origin_by_hand: hands the load to a person, and makes where
they leave it position 0.

establish_reference_by_camera: hands the load to a person to park at the
marker, picks the camera that can see it, and makes that position
absolute.

MarkerWatch: watches for the marker through one leg and re-references the
axis to it - the one thing on this stand that can see the load slip past
the motor. Watches rather than reads, because the fixture crosses the
camera's view in flight and does not stop there.

move_to: commands one target position, blocks until arrived and settled,
and returns where arrival was accepted. cycle_leg is one leg of the
stroke under a test's cycling tolerances.

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

import logging
from typing import Callable, Optional

from hardware.odrive import odrive_errors
from testbeds.ydrive_testbed.ydrive_testbed import (
    BRAKE_SETTLE_S,
    METERS_PER_TURN,
    Motion,
    YdriveTestbed,
)
from hardware.clients.telemetry_client import TelemetryTimeout
from hardware.clients.command_client import CommandClientError
from testcases.step import step
from testcases.teststeps.operator import await_operator
from testcases.utils import Stopwatch
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

DEFAULT_POST_BRAKE_DWELL_S = 5.0
"""How long the brake holds a load it has just stopped before the stopping distance
is taken, so creep while it holds counts against that distance."""

DEFAULT_STOP_TIMEOUT_S = 10.0
"""How long the load may take to come to rest after the brake closes. A brake that
never stops it is a failure, not something to keep waiting on."""

BRAKE_TRIGGER_VELOCITY_LIMIT = 24.0  # turns/s = 2.02 m/s at the stand's 0.084 m/turn
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
    """Clear the ODrive's latched errors and confirm they cleared, retrying until `timeout_s`.
    Retried rather than done once: DC_BUS_UNDER_VOLTAGE re-latches until the bus is up."""
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


def _require_still_driving(test_case: BaseYdriveTest, motion, activity: str) -> None:
    """Raise if the axis has stopped driving when it should be. The ODrive disarms itself on a
    fault and tells nobody, so a loop would wait out its timeout on a coasting load."""
    if motion.armed:
        return
    raise RuntimeError(
        f"test {test_case.test_id}: the axis stopped driving while {activity} - it disarmed "
        f"itself at {motion.position:.2f} turns doing {motion.velocity:.2f} turns/s. "
        f"{test_case.testbed.describe_errors()}"
    )


def _explain_unclearable(remaining: dict) -> str:
    """Split the remaining faults into what clear_errors resets and what it never could -
    undistinguished, a latched register and a live condition both read as retry."""
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
    """Cold stand to ready-to-arm: bus up, faults cleared, control and input mode set, tuning
    applied. Leaves the axis idle behind an engaged brake, and touches neither."""
    testbed: YdriveTestbed = test_case.testbed
    testbed.power_motor_bus(True)
    _clear_faults(test_case, clear_timeout_s)
    testbed.command.set_control_mode(control_mode)
    # Both, not just the control mode: the input mode decides whether a commanded
    # position is acted on at all - see INPUT_MODE_POS_FILTER.
    testbed.command.set_controller_config_input_mode(INPUT_MODE_POS_FILTER)
    _apply_tuning_params(test_case)


@step
def brake_from_speed(
    test_case: BaseYdriveTest,
    target: float,
    trigger_speed: float,
    stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S,
    post_brake_dwell_s: float = DEFAULT_POST_BRAKE_DWELL_S,
    velocity_tolerance: float = DEFAULT_VELOCITY_TOLERANCE,
    position_tolerance: float = DEFAULT_POSITION_TOLERANCE,
    on_engaged: Optional[Callable[[], None]] = None,
) -> float:
    """Accelerate toward `target`, then idle the motor and drop the brake rail so the brake stops
    a moving load - the inverse of engage_brake(), so it closes on a coasting axis."""
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
                "the velocity limit above it (see BRAKE_TRIGGER_VELOCITY_LIMIT) and the current "
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
    # DEFAULT_POST_BRAKE_DWELL_S. Nothing here touches the rail or the axis: they
    # were left where they should be above.
    test_case.wait_for(post_brake_dwell_s)
    rested_at = testbed.get_motion().position

    test_case.set_state("brake_speed_m_s", speed * METERS_PER_TURN)
    test_case.set_state("stopping_distance_m", abs(rested_at - position) * METERS_PER_TURN)
    return rested_at


def release_brake_in_place(test_case: BaseYdriveTest) -> None:
    """Hand a stopped load back to the controller without moving it, by parking the setpoint
    where the axis is first - release_brake() alone would lunge for a stale setpoint."""
    testbed: YdriveTestbed = test_case.testbed
    held_at = testbed.get_pos_estimate()
    testbed.command.set_position(held_at)
    test_case.set_state("position_target", held_at)
    release_brake(test_case)


@step
def dwell_braked(test_case: BaseYdriveTest, dwell_s: float) -> None:
    """Hold the load on the brake for `dwell_s` with the axis idle - the state that dissipates
    nothing. Not unwound in a finally: if this raises, the load stays where the brake has it."""
    engage_brake(test_case)
    test_case.wait_for(dwell_s)
    release_brake(test_case)


def establish_origin_by_hand(test_case: BaseYdriveTest) -> float:
    """Hand the load to a person and make where they leave it position 0, returned in turns. It
    is held by nothing on return, so the caller takes it back with release_brake_in_place()."""
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
def establish_reference_by_camera(test_case: BaseYdriveTest, marker_position: float) -> None:
    """Have a person park the load where the camera sees the marker, pick the camera that
    can, write `marker_position` there, and re-teach. The park is what makes all three possible."""
    testbed: YdriveTestbed = test_case.testbed
    release_brake(test_case)
    testbed.command.set_axis_state("IDLE")
    await_operator(
        test_case,
        "move the load by hand until the CAMERA can see the marker on the fixture, "
        "then acknowledge",
    )

    # Selection recognises the camera AND confirms the fixture is at the marker, so
    # when it works both are verified. When it does not - a camera that has been
    # moved since the view was committed is the ordinary cause, and it invalidates
    # the view without invalidating the mount - the run proceeds on the configured
    # camera with the marker position taken on the operator's word, which is what
    # every hand-set origin on this stand has always rested on. Recorded either way,
    # because "verified" and "asserted" are different claims about the same number.
    try:
        chosen = testbed.vision.select_best_camera()
    except CommandClientError as exc:
        test_case.set_state("camera_selected_by", "configuration")
        logger.warning(
            "test %s: no camera recognised the committed reference view (%s). Proceeding "
            "on the configured camera, with the marker position taken on trust - re-teach "
            "the committed view with `--teach-default` if the camera has moved",
            test_case.test_id, exc,
        )
    else:
        test_case.set_state("camera_selected_by", "reference view")
        # Not camera_source: the vision driver publishes that name itself, continuously,
        # and state is merged into every device's rows - so pushing it here would
        # overwrite the driver's own reading with a one-shot copy of it.
        test_case.set_state("marker_match_score", chosen["match_score"])
        logger.info(
            "test %s: camera %s sees the marker at %.4f - candidates %s",
            test_case.test_id, chosen["camera_source"], chosen["match_score"],
            chosen["considered"],
        )

    testbed.command.set_pos_estimate(marker_position)
    test_case.set_state("position_claimed_at_marker", marker_position)
    logger.info(
        "test %s: the marker is position %.1f turns, and every position is now absolute",
        test_case.test_id, marker_position,
    )

    # Re-taught here, with the fixture parked and the camera chosen: this run's
    # corrections are measured against today's light rather than a picture taken
    # whenever the committed view was captured.
    taught = testbed.vision.teach()
    logger.info("test %s: re-taught the reference view here - %s", test_case.test_id, taught)


MARKER_SEARCH_WINDOW_TURNS = 10.0
"""How far from the marker the axis may read and an alignment still be believed.

A SANITY GATE ON WHERE, not a measurement of drift. The tape is periodic and the
search runs over the whole frame, so a match somewhere else in the stroke is
possible; nothing outside this window is a place the marker can be. Ten turns
either side is 0.84 m, which covers the foot or two of drift the camera was sited
to catch and still excludes the rest of a 110-turn stroke.

Centred on the marker rather than on the top of the stroke, which are 15 turns
apart - a window around the top would not contain the marker at all, and the
correction could never fire."""


class MarkerWatch:
    """Watches for the marker through one leg of the stroke, then re-references the axis.

    THE FIXTURE DOES NOT STOP AT THE MARKER. It crosses the camera's view during the
    turnaround at about 7.7 turns/s and is gone: measured, 0.58 s inside 2 turns of the
    marker, and by the time the leg's arrival is accepted the load has been pulled back
    to roughly 8 turns short of the commanded end - some 23 turns from the marker. A
    single read after arrival never sees it, which is why this watches instead.

    Two phases, on purpose. Watching happens per frame and only records; the correction
    is applied between legs, because writing pos_estimate shifts input_pos and
    pos_setpoint with it, and doing that mid-leg moves the target out from under the
    arrival test that commanded it."""

    def __init__(self, test_case: BaseYdriveTest) -> None:
        self.test_case = test_case
        self.seen_at: Optional[float] = None
        self.best_score: float = 0.0
        self.taught: bool = True

    def __call__(self, motion: Motion) -> None:
        """One frame of the leg. Records where the axis read when the marker was FIRST seen -
        first, so it is always the same crossing of the same view at the same sign of speed,
        and whatever lag there is between the two streams biases every correction alike."""
        if self.seen_at is not None:
            return
        if abs(motion.position - self.test_case.position_claimed_at_marker) > MARKER_SEARCH_WINDOW_TURNS:
            return
        try:
            marker = self.test_case.testbed.get_marker_alignment()
        except TelemetryTimeout:
            # A camera that has stopped publishing must not stop the stroke. It shows up
            # as distance_since_correction_m climbing, which is where it belongs.
            return
        self.best_score = max(self.best_score, marker.score)
        self.taught = marker.taught
        if marker.aligned:
            self.seen_at = motion.position

    def apply(self) -> None:
        """Re-reference the axis if the marker was seen, and publish either way. Called between
        legs, with the load stopped and the leg's own bookkeeping already closed."""
        test_case = self.test_case
        test_case.set_state("marker_match_score", self.best_score)

        if not self.taught:
            logger.warning(
                "test %s: the camera has no reference view, so nothing can be corrected - "
                "teach one with `python -m hardware.vision_home.main --teach-default`",
                test_case.test_id,
            )
            return

        if self.seen_at is None:
            # A bumped camera, or a turnaround that stops reaching the marker, stops
            # corrections dead - and with nothing bounding drift the load then walks
            # exactly as it did before. distance_since_correction_m rising and staying up
            # is that; it is derived from the mark below, not published here.
            logger.info(
                "test %s: no marker alignment this leg (best score %.3f) - nothing "
                "corrected, %.0f m since the last one",
                test_case.test_id, self.best_score,
                test_case.total_distance_m - test_case.distance_at_last_correction_m,
            )
            return

        # What the axis reads NOW, less what it read at the sighting, is how far the load
        # has moved since - so the marker's number plus that gap is what here should be
        # called, and the correction is the rest.
        here = test_case.testbed.get_pos_estimate()
        should_read = test_case.position_claimed_at_marker + (here - self.seen_at)
        correction = should_read - here
        # Impulse-free: the firmware shifts input_pos and pos_setpoint by the same amount,
        # so this changes what positions MEAN rather than moving the load.
        # The driver skips the step across this write rather than booking it as travel -
        # see the odrive backend's _accumulate_turns_traveled().
        test_case.testbed.command.set_pos_estimate(should_read)
        # The mark, not the channel: distance_since_correction_m is derived from this on
        # every frame, so moving the mark is the whole of what a landed correction records.
        test_case.distance_at_last_correction_m = test_case.total_distance_m
        logger.info(
            "test %s: re-referenced %+.4f turns (%+.1f mm) - marker seen at %.3f, "
            "score %.3f",
            test_case.test_id, correction, correction * METERS_PER_TURN * 1000,
            self.seen_at, self.best_score,
        )


@step
def move_to(
    test_case: BaseYdriveTest,
    target: float,
    position_tolerance: float = DEFAULT_POSITION_TOLERANCE,
    velocity_tolerance: float = DEFAULT_VELOCITY_TOLERANCE,
    arrival_timeout_s: float = DEFAULT_ARRIVAL_TIMEOUT_S,
    each_frame: Optional[Callable[[Motion], None]] = None,
) -> float:
    """Command one target, block until arrived and settled, and return where arrival was
    accepted - which under a loose gate is short of the target, and after an overshoot is on
    the way back from it. Distance travelled does NOT come from here: the odrive driver counts
    the path frame by frame as turns_traveled, which is the only account that has the
    overshoot in it.

    `each_frame` sees every frame of the move, for what has to be watched WHILE the load is
    moving rather than after it stops. It runs at the stream's rate, so it must be cheap, and
    it must not move the axis - this loop's arrival test is judging the target it commanded."""
    testbed: YdriveTestbed = test_case.testbed
    testbed.command.set_position(target)
    test_case.set_state("position_target", target)
    deadline: Stopwatch = Stopwatch(duration_s=arrival_timeout_s)
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
        if each_frame is not None:
            each_frame(motion)
        within_tolerance: bool = (
            abs(motion.position - target) <= position_tolerance
            and abs(motion.velocity) <= velocity_tolerance
        )
        if within_tolerance:
            return motion.position
        if deadline.expired:
            raise TimeoutError(
                f"test {test_case.test_id}: position didn't settle at {target} within {arrival_timeout_s}s"
            )


def cycle_leg(test_case: BaseYdriveTest, target: float,
              each_frame: Optional[Callable[[Motion], None]] = None) -> float:
    """One end-to-end leg of a normal stroke, under the test's own cycling tolerances rather
    than move_to's defaults. The brake is not touched and the axis stays armed."""
    return move_to(
        test_case,
        target,
        position_tolerance=test_case.CYCLE_POSITION_TOLERANCE,
        velocity_tolerance=test_case.CYCLE_VELOCITY_TOLERANCE,
        arrival_timeout_s=test_case.MOVE_TIMEOUT_S,
        each_frame=each_frame,
    )


def _await_axis_armed(test_case: BaseYdriveTest, armed: bool, timeout_s: float) -> None:
    """Block until `axis_is_armed` reads `armed`, or raise with describe_errors(). Requesting a
    state only writes requested_state, and the ODrive can decline it."""
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
    """Engage the brake, then idle the axis, confirming it idled - the brake grabs before the
    controller lets go, and a braked axis must not be armed."""
    testbed: YdriveTestbed = test_case.testbed
    testbed.power_brake_bus(False)
    test_case.wait_for(BRAKE_SETTLE_S)
    testbed.command.set_axis_state("IDLE")
    _await_axis_armed(test_case, armed=False, timeout_s=arm_timeout_s)
    test_case.set_state("brake_engaged", True)


def release_brake(test_case: BaseYdriveTest, arm_timeout_s: float = DEFAULT_ARM_TIMEOUT_S) -> None:
    """Arm the axis, confirm it armed, then release the brake - the controller takes hold before
    the brake lets go. Safe only while the setpoint matches; see release_brake_in_place()."""
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
    """One low<->high cycle, the brake holding each dwell with the axis idled. Across every
    transition the load is held by the controller, the brake, or both - never neither."""

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
    """Write the controller and motor configuration this stand runs under, in RAM - nothing here
    saves, so a run cannot leave a stand configured differently than it found it."""
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
