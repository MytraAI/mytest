"""The zdrive brake endurance test: stopping a moving load, over and over.

The descent is a deliberate drop: the axis stays idle and the load falls under its
own weight, so the controller is never in the loop and cannot be asked for more
current than its limit allows. What that buys is asserted here - the axis must be
confirmed idle before the rail is released, the brake must close on the way out
however the fall ends, and the fall must be bounded by position as well as by
speed, because an idle axis has no authority to abort with.

The measurement's baseline is asserted too. Stopping distance is taken from the
frame after the brake was commanded, not from the frame that tripped the trigger.

These run against fakes - no subprocess, no instrument.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from testbeds.zdrive_testbed.zdrive_testbed import METERS_PER_TURN, Motion
from testcases.zdrive.channels import DEFAULT_STATE
from testcases.zdrive.rulebooks.zdrive_rulebook import (
    BRAKE_ENDURANCE_TEST_NAME,
    MAX_STOPPING_DISTANCE_M,
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
TARGET = 0.0
"""Where the load is falling toward - the bottom of the stroke."""

FRAMES = [
    (-50.0, 0.0),    # started_at, at the top with the brake just released
    (-49.0, 10.0),   # falling, under the trigger
    (-47.0, 26.0),   # crosses the trigger -> brake commanded. NOT the baseline.
    (-45.0, 20.0),   # baseline: first frame after the rail dropped
    (-44.0, 5.0),    # still moving
    (-43.5, 0.0),    # stopped
    (-43.4, 0.0),    # rested_at: 0.1 turns of creep across the rest
]
BASELINE_POSITION, BASELINE_VELOCITY = FRAMES[3]
RESTED_POSITION = FRAMES[-1][0]


class FakeBrakeTestbed:
    """Hands out a scripted sequence of motion frames, so a measured distance can
    be checked against an exact expected number rather than a simulation's drift.

    The last frame repeats once exhausted, which is what lets a loop waiting on
    "stopped" terminate."""

    def __init__(self, frames=None, armed: bool = False):
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
    distance = brake_from_speed(case, target=TARGET, trigger_speed=TRIGGER)
    return case, testbed, distance


# --- the two orderings ------------------------------------------------------


def test_the_controller_never_enters_the_descent():
    """The whole point of the drop: asked to hold a descent this axis runs away,
    and the velocity error then commands more current than the limit allows until
    the firmware disarms. Nothing here may command a position or arm the axis."""
    _, testbed, _ = _brake_once()
    assert not [c for c in testbed.calls if c.startswith("move:")]
    assert "axis:CLOSED_LOOP_CONTROL" not in testbed.calls


def test_the_idle_is_confirmed_before_the_brake_is_released():
    """Releasing the brake onto an armed axis hands the load to a controller whose
    setpoint is wherever the last move left it, which it would then lunge for."""
    _, testbed, _ = _brake_once()
    released = testbed.calls.index("brake:release")
    assert "armed?" in testbed.calls[:released], (
        "the rail was released without confirming the axis was idle"
    )


def test_an_axis_that_is_still_armed_stops_the_run_before_the_brake_is_released():
    """The load must not be handed to a controller nobody asked to take it."""
    testbed = FakeBrakeTestbed(armed=True)
    case = FakeTestCase(testbed)
    with pytest.raises(RuntimeError, match="did not idle"):
        brake_from_speed(case, target=TARGET, trigger_speed=TRIGGER, arm_timeout_s=0.05)
    assert "brake:release" not in testbed.calls


def test_the_brake_closes_even_if_the_fall_raises():
    """Once the rail is released the load is held by nothing, so a fatal bound or
    a stop request during the fall must still leave the brake holding."""
    testbed = FakeBrakeTestbed()
    case = FakeTestCase(testbed)

    calls = {"n": 0}
    def boom():
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("fatal bound during the fall")
    case.check_should_continue = boom

    with pytest.raises(RuntimeError, match="fatal bound"):
        brake_from_speed(case, target=TARGET, trigger_speed=TRIGGER)
    assert testbed.calls.index("brake:release") < testbed.calls.index("brake:engage")
    assert case.state["brake_engaged"] is True


def test_the_fall_is_bounded_by_position_as_well_as_speed():
    """An idle axis has no authority to abort with, so the stroke is the only other
    limit. The brake still closes; the cycle is logged as not having reached its
    trigger."""
    never_fast = [(-50.0, 0.0), (-40.0, 5.0), (-30.0, 5.0), (-25.0, 5.0),
                  (-20.0, 5.0), (-19.0, 2.0), (-18.5, 0.0), (-18.4, 0.0)]
    testbed = FakeBrakeTestbed(frames=never_fast)
    case = FakeTestCase(testbed)
    distance = brake_from_speed(case, target=TARGET, trigger_speed=TRIGGER, backstop_turns=20.0)
    assert "brake:engage" in testbed.calls
    assert distance > 0
    assert case.state["brake_speed_m_s"] < TRIGGER


# --- what the measurement is baselined on -----------------------------------


def test_the_distance_is_baselined_after_the_brake_command_not_on_the_trigger():
    """Confirming the idle costs a frame, and baselining on the trigger frame would
    charge that frame's coasting to the brake. The frame after the rail drops is
    the first one the brake has been asked to do anything about."""
    _, _, distance = _brake_once()
    expected = (RESTED_POSITION - BASELINE_POSITION) * METERS_PER_TURN
    assert distance == pytest.approx(expected)

    trigger_frame_position = FRAMES[2][0]
    from_trigger = (RESTED_POSITION - trigger_frame_position) * METERS_PER_TURN
    assert distance < from_trigger, "still measuring from the trigger frame"


def test_the_recorded_speed_is_the_one_the_brake_saw_not_the_trigger():
    """On a near-self-locking axis friction slows the load while the axis idles, so
    the engagement speed is below the trigger. What was asked for lives in the
    run's metadata instead."""
    case, _, _ = _brake_once()
    assert case.state["brake_speed_m_s"] == pytest.approx(BASELINE_VELOCITY * METERS_PER_TURN)
    assert case.state["brake_speed_m_s"] < FRAMES[2][1], "recorded the trigger frame"


def test_the_distance_and_speed_come_off_the_same_frame():
    """A speed from one frame and a position from another describe two different
    instants, which is not a measurement of anything."""
    case, _, distance = _brake_once()
    assert case.state["brake_speed_m_s"] == pytest.approx(BASELINE_VELOCITY * METERS_PER_TURN)
    assert distance == pytest.approx((RESTED_POSITION - BASELINE_POSITION) * METERS_PER_TURN)


def test_creep_across_the_rest_counts_against_the_distance():
    """Nothing drives across the rest, so movement over it is the brake giving way
    and belongs in the number."""
    _, _, distance = _brake_once()
    settled_position = FRAMES[-2][0]
    assert RESTED_POSITION > settled_position, "the fixture has no creep to count"
    assert distance > (settled_position - BASELINE_POSITION) * METERS_PER_TURN


def test_the_distance_is_published_in_millimetres():
    """The bound is written in millimetres, so the channel has to be."""
    case, _, distance = _brake_once()
    assert case.state["stopping_distance_m"] == pytest.approx(distance)
    assert distance == pytest.approx(1.6 * METERS_PER_TURN)


# --- the failures it has to report ------------------------------------------


def test_a_brake_that_never_stops_the_load_raises():
    """Reported rather than waited on: a load still moving after the timeout is one
    the brake is not stopping."""
    never_stops = [(-50.0, 0.0), (-49.0, 10.0), (-47.0, 26.0)] + [(-46.0 + i * 0.1, 26.0) for i in range(50)]
    testbed = FakeBrakeTestbed(frames=never_stops)
    case = FakeTestCase(testbed)
    with pytest.raises(TimeoutError, match="still moving"):
        brake_from_speed(case, target=TARGET, trigger_speed=TRIGGER, stop_timeout_s=0.05)


# --- the bound --------------------------------------------------------------


def test_the_stopping_distance_bound_is_fatal_and_undebounced():
    """One bad stop IS the event: it is one number per brake event, not a sampled
    signal that can spike, so debouncing would mean waiting for a second one."""
    bound = next(b for b in ZDRIVE_RULEBOOK.bounds if b.channel == "stopping_distance_m")
    assert bound.fatal
    assert bound.persistence_s is None
    assert bound.upper == MAX_STOPPING_DISTANCE_M == 0.25
    assert bound.lower is None


def test_the_bounded_channel_is_seeded_so_a_fresh_run_can_start():
    """A numeric bound on a channel carrying no value is unevaluable, and the
    runner treats unevaluable as a stop - so None here would end every run on its
    first frame, before anything moved."""
    assert DEFAULT_STATE["stopping_distance_m"] == 0.0
    assert DEFAULT_STATE["brake_speed_m_s"] == 0.0
    assert DEFAULT_STATE["brake_cycles"] == 0
    bound = next(b for b in ZDRIVE_RULEBOOK.bounds if b.channel == "stopping_distance_m")
    assert bound.evaluate(DEFAULT_STATE) is False, "a fresh run must be able to start"


def test_the_bound_is_a_gross_fault_net_not_a_performance_figure():
    """0.25 m is 26 turns. A healthy stop is a fraction of a turn, because the axis
    is close to self-locking - so this catches the brake AND the screw having let
    go, rather than grading a brake."""
    assert MAX_STOPPING_DISTANCE_M / METERS_PER_TURN == pytest.approx(26.04, abs=0.01)
    assert MAX_STOPPING_DISTANCE_M < abs(BrakeEnduranceTest.TOP_POSITION) * METERS_PER_TURN


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


def test_the_tuning_is_written_once_and_never_changed_per_cycle():
    """The drop needs no ceiling of its own - the axis is idle for it - so a cycle
    has one tuning, and the descent after the brake runs at the ordinary limit."""
    source = _code_of(BrakeEnduranceTest.main_execution)
    assert source.count("set_tuning_params") == 1


def test_the_drop_follows_the_hold_with_nothing_in_between():
    """hold_on_brake leaves the stand braked and idle, which is exactly what
    brake_from_speed expects. Re-arming between them would put the controller back
    in a descent it cannot hold."""
    source = _code_of(BrakeEnduranceTest.main_execution)
    hold = source.index("hold_on_brake")
    drop = source.index("brake_from_speed")
    assert "release_brake_in_place" not in source[hold:drop]


def test_the_cycle_rests_at_the_bottom_on_the_brake():
    """Braked before idled and dwelt on, with the load on its hard stop - the one
    place on this axis where a rest costs nothing and risks nothing."""
    source = _code_of(BrakeEnduranceTest.main_execution)
    assert source.index("engage_brake(self)") < source.index("wait_for(self.DWELL_S)")
    assert BrakeEnduranceTest.DWELL_S == 300.0


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
