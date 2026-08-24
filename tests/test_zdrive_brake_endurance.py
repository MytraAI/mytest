"""The zdrive brake endurance test: stopping a moving load, over and over.

Two orderings carry the safety of this test and both are inversions of something
else in the module, which is why they are asserted rather than trusted: the motor
is idled BEFORE the brake closes (the opposite of engage_brake), and the idle is
CONFIRMED before the rail drops (the same as engage_brake). Get the first wrong
and the motor drives into a closing brake; get the second wrong and a still-armed
axis pushes a full-stroke position error into one.

The measurement's baseline is asserted too. Stopping distance is taken from the
frame after the brake was commanded, not from the frame that tripped the trigger,
so confirming the idle cannot inflate it.

These run against fakes - no subprocess, no instrument.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from testbeds.zdrive_testbed.zdrive_testbed import MM_PER_TURN, Motion
from testcases.zdrive.channels import DEFAULT_STATE
from testcases.zdrive.rulebooks.zdrive_rulebook import (
    BRAKE_ENDURANCE_TEST_NAME,
    MAX_STOPPING_DISTANCE_MM,
    TEST_NAMES,
    ZDRIVE_RULEBOOK,
)
from testcases.zdrive.testcases.testcases import BrakeEnduranceTest
from testcases.zdrive.teststeps import teststeps
from testcases.zdrive.teststeps.teststeps import brake_from_speed

def _code_of(target) -> str:
    """The source of `target` with docstrings stripped, so an assertion about what
    the code does is not satisfied - or defeated - by prose describing it."""
    source = textwrap.dedent(inspect.getsource(target))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)
    return ast.unparse(tree)


TRIGGER = 25.0

# (position, velocity) handed out by successive get_motion() calls. The order the
# step reads them in is: started_at, then one per accelerate-loop pass, then the
# post-brake baseline, then one per stop-loop pass, then rested_at.
FRAMES = [
    (0.0, 0.0),      # started_at
    (1.0, 10.0),     # accelerating, under the trigger
    (3.0, 26.0),     # crosses the trigger -> brake commanded. NOT the baseline.
    (5.0, 20.0),     # baseline: first frame after the rail dropped
    (6.0, 5.0),      # still moving
    (6.5, 0.0),      # stopped
    (6.6, 0.0),      # rested_at: 0.1 turns of creep across the rest
]
BASELINE_POSITION, BASELINE_VELOCITY = FRAMES[3]
RESTED_POSITION = FRAMES[-1][0]


class FakeBrakeTestbed:
    """Hands out a scripted sequence of motion frames, so a measured distance can
    be checked against an exact expected number rather than a simulation's drift.

    The last frame repeats once exhausted, which is what lets a loop waiting on
    "stopped" terminate."""

    def __init__(self, frames=None, armed: bool = True):
        self.calls: list[str] = []
        self.command = self
        self._frames = list(frames if frames is not None else FRAMES)
        self.armed = armed
        self.position = 0.0

    # --- ODrive side
    def set_position(self, target):
        self.calls.append(f"move:{target}")

    def set_axis_state(self, state):
        self.calls.append(f"axis:{state}")
        self.armed = state == "CLOSED_LOOP_CONTROL"

    def get_axis_armed_status(self):
        self.calls.append("armed?")
        return self.armed

    def get_motion(self):
        position, velocity = self._frames[0] if len(self._frames) == 1 else self._frames.pop(0)
        self.position = position
        return Motion(position=position, velocity=velocity, armed=self.armed)

    def get_pos_estimate(self):
        return self.position

    def describe_errors(self):
        return "no errors (fake)"

    # --- supply side
    def power_brake_bus(self, enabled):
        self.calls.append("brake:release" if enabled else "brake:engage")


class FakeTestCase:
    test_id = "test-zdrive-brake-endurance"

    def __init__(self, testbed):
        self.testbed = testbed
        self.state: dict = {}

    def set_state(self, name, value):
        self.state[name] = value

    def wait_for(self, seconds):
        self.testbed.calls.append(f"wait:{seconds}")

    def check_should_continue(self):
        pass


def _brake_once(**kwargs):
    testbed = FakeBrakeTestbed(**kwargs)
    case = FakeTestCase(testbed)
    distance = brake_from_speed(case, target=0.0, trigger_speed=TRIGGER)
    return case, testbed, distance


# --- the two orderings ------------------------------------------------------


def test_the_motor_is_idled_before_the_brake_closes():
    """The inverse of engage_brake(), and it has to be: the load is moving, so the
    brake closes on a coasting axis. The other order drives the motor into a
    closing brake."""
    _, testbed, _ = _brake_once()
    assert testbed.calls.index("axis:IDLE") < testbed.calls.index("brake:engage")


def test_the_idle_is_confirmed_before_the_rail_drops():
    """Requesting a state only writes requested_state and the ODrive can decline
    it. A still-armed axis holds position against a locked output, and here the
    setpoint is the far end of the stroke - so the error it would push into the
    brake is most of the travel."""
    _, testbed, _ = _brake_once()
    idled = testbed.calls.index("axis:IDLE")
    engaged = testbed.calls.index("brake:engage")
    assert "armed?" in testbed.calls[idled:engaged], (
        "the rail dropped without confirming the axis had idled"
    )


def test_an_axis_that_refuses_to_idle_stops_the_run_before_the_brake_closes():
    """A brake closing on a driving motor is the thing being prevented, so the
    failure has to come first."""
    testbed = FakeBrakeTestbed()
    testbed.set_axis_state = lambda state: testbed.calls.append(f"axis:{state}")  # never idles
    case = FakeTestCase(testbed)
    with pytest.raises(RuntimeError, match="did not idle"):
        brake_from_speed(case, target=0.0, trigger_speed=TRIGGER, arm_timeout_s=0.05)
    assert "brake:engage" not in testbed.calls


# --- what the measurement is baselined on -----------------------------------


def test_the_distance_is_baselined_after_the_brake_command_not_on_the_trigger():
    """Confirming the idle costs a frame, and baselining on the trigger frame would
    charge that frame's coasting to the brake. The frame after the rail drops is
    the first one the brake has been asked to do anything about."""
    _, _, distance = _brake_once()
    expected = (RESTED_POSITION - BASELINE_POSITION) * MM_PER_TURN
    assert distance == pytest.approx(expected)

    trigger_frame_position = FRAMES[2][0]
    from_trigger = (RESTED_POSITION - trigger_frame_position) * MM_PER_TURN
    assert distance < from_trigger, "still measuring from the trigger frame"


def test_the_recorded_speed_is_the_one_the_brake_saw_not_the_trigger():
    """On a near-self-locking axis friction slows the load while the axis idles, so
    the engagement speed is below the trigger. What was asked for lives in the
    run's metadata instead."""
    case, _, _ = _brake_once()
    assert case.state["brake_speed_turns_s"] == pytest.approx(BASELINE_VELOCITY)
    assert case.state["brake_speed_turns_s"] < FRAMES[2][1], "recorded the trigger frame"


def test_the_distance_and_speed_come_off_the_same_frame():
    """A speed from one frame and a position from another describe two different
    instants, which is not a measurement of anything."""
    case, _, distance = _brake_once()
    assert case.state["brake_speed_turns_s"] == pytest.approx(BASELINE_VELOCITY)
    assert distance == pytest.approx((RESTED_POSITION - BASELINE_POSITION) * MM_PER_TURN)


def test_creep_across_the_rest_counts_against_the_distance():
    """Nothing drives across the rest, so movement over it is the brake giving way
    and belongs in the number."""
    _, _, distance = _brake_once()
    settled_position = FRAMES[-2][0]
    assert RESTED_POSITION > settled_position, "the fixture has no creep to count"
    assert distance > (settled_position - BASELINE_POSITION) * MM_PER_TURN


def test_the_distance_is_published_in_millimetres():
    """The bound is written in millimetres, so the channel has to be."""
    case, _, distance = _brake_once()
    assert case.state["stopping_distance_mm"] == pytest.approx(distance)
    assert distance == pytest.approx(1.6 * MM_PER_TURN)


# --- the failures it has to report ------------------------------------------


def test_a_brake_that_never_stops_the_load_raises():
    """Reported rather than waited on: a load still moving after the timeout is one
    the brake is not stopping."""
    never_stops = [(0.0, 0.0), (1.0, 10.0), (3.0, 26.0)] + [(10.0 + i, 26.0) for i in range(50)]
    testbed = FakeBrakeTestbed(frames=never_stops)
    case = FakeTestCase(testbed)
    with pytest.raises(TimeoutError, match="still moving"):
        brake_from_speed(case, target=0.0, trigger_speed=TRIGGER, stop_timeout_s=0.05)


def test_arriving_without_reaching_the_trigger_raises_with_the_peak():
    """The peak is what says whether the speed is achievable at all, so it goes in
    the message rather than being left for someone to infer."""
    too_slow = [(0.0, 0.0), (-5.0, 8.0), (-1.0, 9.0), (0.0, 9.0)]
    testbed = FakeBrakeTestbed(frames=too_slow)
    case = FakeTestCase(testbed)
    with pytest.raises(RuntimeError, match="without ever reaching"):
        brake_from_speed(case, target=0.0, trigger_speed=TRIGGER)


# --- the bound --------------------------------------------------------------


def test_the_stopping_distance_bound_is_fatal_and_undebounced():
    """One bad stop IS the event: it is one number per brake event, not a sampled
    signal that can spike, so debouncing would mean waiting for a second one."""
    bound = next(b for b in ZDRIVE_RULEBOOK.bounds if b.channel == "stopping_distance_mm")
    assert bound.fatal
    assert bound.persistence_s is None
    assert bound.upper == MAX_STOPPING_DISTANCE_MM == 250.0
    assert bound.lower is None


def test_the_bounded_channel_is_seeded_so_a_fresh_run_can_start():
    """A numeric bound on a channel carrying no value is unevaluable, and the
    runner treats unevaluable as a stop - so None here would end every run on its
    first frame, before anything moved."""
    assert DEFAULT_STATE["stopping_distance_mm"] == 0.0
    assert DEFAULT_STATE["brake_speed_turns_s"] == 0.0
    assert DEFAULT_STATE["brake_cycles"] == 0
    bound = next(b for b in ZDRIVE_RULEBOOK.bounds if b.channel == "stopping_distance_mm")
    assert bound.evaluate(DEFAULT_STATE) is False, "a fresh run must be able to start"


def test_the_bound_is_a_gross_fault_net_not_a_performance_figure():
    """250 mm is 26 turns. A healthy stop is a fraction of a turn, because the axis
    is close to self-locking - so this catches the brake AND the screw having let
    go, rather than grading a brake."""
    assert MAX_STOPPING_DISTANCE_MM / MM_PER_TURN == pytest.approx(26.04, abs=0.01)
    assert MAX_STOPPING_DISTANCE_MM < abs(BrakeEnduranceTest.TOP_POSITION) * MM_PER_TURN


# --- the test case ----------------------------------------------------------


def test_the_test_name_is_registered_against_the_rulebook():
    """A TEST_NAME missing from TEST_NAMES means the whole rulebook silently never
    runs for this test."""
    assert BrakeEnduranceTest.TEST_NAME == BRAKE_ENDURANCE_TEST_NAME
    assert BRAKE_ENDURANCE_TEST_NAME in TEST_NAMES


def test_the_lift_stays_inside_the_stroke():
    assert teststeps.TOP_OF_STROKE < BrakeEnduranceTest.TOP_POSITION
    assert BrakeEnduranceTest.TOP_POSITION < BrakeEnduranceTest.BOTTOM_POSITION
    assert BrakeEnduranceTest.BOTTOM_POSITION == teststeps.BOTTOM_OF_STROKE


def test_the_trigger_sits_under_the_rundown_ceiling():
    """Above it the axis clamps below the trigger and the brake never fires; at the
    overspeed trip the axis disarms mid-descent instead."""
    assert BrakeEnduranceTest.TRIGGER_SPEED_TURNS_S < BrakeEnduranceTest.RUNDOWN_VELOCITY_LIMIT
    trip = BrakeEnduranceTest.RUNDOWN_VELOCITY_LIMIT * teststeps.VELOCITY_LIMIT_TOLERANCE
    assert BrakeEnduranceTest.TRIGGER_SPEED_TURNS_S < trip


def test_the_lift_runs_at_the_normal_limit_and_only_the_rundown_is_raised():
    """Holding 1000 lb already draws about 52 A of a 55 A soft limit, so there is
    no current left for the acceleration a raised ceiling would ask for on the way
    up. Down is where the headroom is."""
    assert BrakeEnduranceTest.RUNDOWN_VELOCITY_LIMIT > teststeps.VELOCITY_LIMIT
    source = _code_of(BrakeEnduranceTest.main_execution)
    lift = source.index("TOP_POSITION")
    raised = source.index("RUNDOWN_VELOCITY_LIMIT")
    assert lift < raised, "the ceiling is raised before the lift rather than after it"


def test_the_tuning_is_restored_before_the_final_descent():
    """Otherwise the last stretch to the bottom runs under the run-down's ceiling
    rather than the normal one."""
    source = _code_of(BrakeEnduranceTest.main_execution)
    restore = source.rindex("set_tuning_params(self)")
    last_move = source.rindex("move_to(")
    assert restore < last_move


def test_the_cycle_rests_at_the_bottom_on_the_brake():
    """Braked before idled and dwelt on, with the load on its hard stop - the one
    place on this axis where a rest costs nothing and risks nothing."""
    source = _code_of(BrakeEnduranceTest.main_execution)
    assert source.index("engage_brake(self)") < source.index("wait_for(self.DWELL_S)")
    assert BrakeEnduranceTest.DWELL_S == 60.0


def test_every_channel_the_step_publishes_is_seeded():
    """The engine fixes a wide file's header from its first frames and drops a
    channel that appears later, so an unseeded channel is measured and then thrown
    away."""
    source = _code_of(brake_from_speed) + _code_of(BrakeEnduranceTest.main_execution)
    published = {
        line.split('set_state("', 1)[1].split('"', 1)[0]
        for line in source.splitlines()
        if 'set_state("' in line
    }
    published |= {"brake_cycles", "position_origin"}  # published by the test case itself
    missing = sorted(published - set(DEFAULT_STATE))
    assert not missing, f"published but never seeded, so the engine drops them: {missing}"
