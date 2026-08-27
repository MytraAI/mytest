"""Concrete ydrive test cases.

EnduranceCycleTest: cycles indefinitely between two positions, tracking
cumulative distance as total_distance_m. Distance is computed from the
setpoints via METERS_PER_TURN, not measured, so it assumes each cycle
reaches its target. Teardown moves nothing: it stops where the last cycle
left it, and main_execution re-establishes its own reference next run.

BrakeEnduranceTest: stops a moving load with the brake over and over -
run 110 -> 0, brake at speed, hold, return, rest - recording the speed it
engaged at and how far the load ended up.

CycleBrakeEnduranceTest: the same brake event, but earned. Cycles the
full stroke with the brake released and the axis armed throughout, and
every BRAKE_INTERVAL_M of measured travel lets the brake stop the load
instead of the controller. Distance is the binding quantity, so it comes
from the odrive driver's turns_traveled - the path the axis took, frame by
frame - rather than from the setpoints it was aimed at.

ManualTest: no sequence of its own. Starts live evaluation and blocks on
wait_for(inf), so only a fatal bound ends it, keeping the drivers and
their endpoints alive for an operator (e.g. tools/manual_gui.py). Leaves
the axis IDLE and unconfigured and the bus off: control mode, tuning,
arming and powering a rail are the operator's to choose, and teardown
commands no motion because it cannot know where they left the axis."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from protocol.wire import DEVICE_ODRIVE
from testbeds.ydrive_testbed.ydrive_testbed import METERS_PER_TURN

from ..rulebooks.ydrive_rulebook import (
    BRAKE_ENDURANCE_TEST_NAME,
    CYCLE_BRAKE_ENDURANCE_TEST_NAME,
    ENDURANCE_CYCLE_TEST_NAME,
    MANUAL_TEST_NAME,
)
from .base_ydrive_test import BaseYdriveTest
from testcases.teststeps.operator import prompt_for_run_details
from ..teststeps.teststeps import (
    MAX_LOAD_VELOCITY_LIMIT,
    BRAKE_TRIGGER_VELOCITY_LIMIT,
    brake_from_speed,
    establish_reference_by_camera,
    MarkerWatch,
    cycle_leg,
    cycle_position,
    dwell_braked,
    establish_origin_by_hand,
    move_to,
    prepare_for_operation,
    release_brake,
    release_brake_in_place,
    set_tuning_params,
)

logger = logging.getLogger(__name__)


class EnduranceCycleTest(BaseYdriveTest):
    """Cycles ydrive indefinitely between two position setpoints, tracking cumulative distance traveled."""

    TEST_NAME = ENDURANCE_CYCLE_TEST_NAME

    LOW_POSITION = 0.0
    HIGH_POSITION = 10.0

    def __init__(self, test_id: Optional[str] = None, use_mock: bool = False, require_engine: bool = True):
        super().__init__(test_id, use_mock, require_engine=require_engine)
        self.total_distance_m = 0.0
        self.run_details: dict = {}
        """What the operator said this run is, once they have been asked. Empty until
        then, so a run that ends before the prompt still has a verdict."""

    def result_metadata(self) -> dict:
        """What this run was, for the verdict."""
        return dict(self.run_details)

    def main_execution(self) -> None:
        # Asked first, while nothing is energized: it needs a person and does not
        # need the stand, and a run nobody can attribute to a DUT is not worth the
        # hours it takes.
        self.run_details = prompt_for_run_details(self, self.RUN_DETAIL_FIELDS)

        # Bus up, latched faults cleared, control mode and tuning set - and the
        # axis still idle behind an engaged brake.
        prepare_for_operation(self)
        # Evaluation starts once the stand is in the state the bounds describe.
        # YDRIVE_RULEBOOK's undervoltage_bound is fatal and undebounced, so a
        # runner started over a de-energized bus fails the run on its first
        # frame - the bus reading volts is the correct answer to a question
        # nobody should be asking yet.
        #
        # All three streams: YDRIVE_RULEBOOK bounds the ODrive's bus channels, the
        # supply's power-limit flag and the DAQ's temperatures, and no one device
        # publishes another's. A bound whose channel is absent from a frame returns
        # no result, so each stream evaluates the bounds it carries.
        self.runner.start(
            self.testbed.telemetry, self.testbed.supply_telemetry, self.testbed.tc_daq_telemetry
        )
        # Arms the axis and then releases the brake, in that order -
        # pre_test_setup left the brake engaged and the axis idle on purpose.
        release_brake(self)
        move_to(self, self.LOW_POSITION)

        distance_per_cycle_m = 2 * abs(self.HIGH_POSITION - self.LOW_POSITION) * METERS_PER_TURN
        self.set_state("total_distance_m", self.total_distance_m)

        while True:
            cycle_position(self, low_position=self.LOW_POSITION, high_position=self.HIGH_POSITION)
            self.total_distance_m += distance_per_cycle_m
            self.set_state("total_distance_m", self.total_distance_m)
            logger.info("test %s: total distance traveled: %.3f m", self.test_id, self.total_distance_m)


class _BrakingYdriveTest(BaseYdriveTest):
    """Numbers and bookkeeping shared by the ydrive tests that stop a moving load with the brake.
    Not runnable, and holds no sequence: each subclass's main_execution states its own."""

    START_POSITION = 110.0
    """THE BOTTOM OF THE STROKE, where each braking run begins. The axis is driven here
    under position control; what the controller does between runs is each subclass's own."""

    BRAKE_TARGET_POSITION = 0.0
    """THE TOP OF THE STROKE, where each braking run is aimed; the brake stops the axis
    short of it. Travel is 110 -> 0, so position DECREASES going up and a late stop's
    overshoot carries past the top into negative turns, where the clearance is."""

    TRIGGER_SPEED_M_S = 1.75
    """A floor, not the speed the brake sees - unloaded, the axis crosses it within one
    frame and engages at the velocity limit. Set below the loaded stand's peak."""

    BRAKE_RUN_VELOCITY_LIMIT = BRAKE_TRIGGER_VELOCITY_LIMIT
    """The ceiling a run-up to the trigger speed needs, 15% above the trigger because a
    loaded axis nears its ceiling asymptotically.

    Above what the default tuning allows, which is the whole reason it is named: a
    subclass that needs it only for the run-up has to write it and put it back."""


    POST_BRAKE_DWELL_S = 5.0
    """How long the brake holds what it stopped before the distance is taken, so creep
    counts against that distance. Nothing drives, so movement across it is slip."""

    MOVE_TIMEOUT_S = 45.0
    """How long a move along the stroke may take: 110 turns is 5 s at VELOCITY_LIMIT and
    18.5 s at a 0.5 m/s cruise. Bounded, so a stalled axis is reported rather than
    waited on."""

    def __init__(
        self,
        test_id: Optional[str] = None,
        use_mock: bool = False,
        require_engine: bool = True,
        trigger_speed_m_s: Optional[float] = None,
    ):
        """trigger_speed_m_s overrides TRIGGER_SPEED_M_S, so a slower shakedown needs no
        edit to the class - as CycleDutPositionTest takes a shortened duration."""
        super().__init__(test_id, use_mock, require_engine=require_engine)
        self.brake_cycles = 0
        self.run_details: dict = {}
        self._trigger_speed_m_s = (
            trigger_speed_m_s if trigger_speed_m_s is not None else self.TRIGGER_SPEED_M_S
        )

    @property
    def trigger_speed_turns_s(self) -> float:
        return self._trigger_speed_m_s / METERS_PER_TURN

    def _count_brake_event(self) -> None:
        """Record that the brake has just stopped a moving load. Called at engagement rather than on
        return, because everything after engagement can raise and the event still happened."""
        self.brake_cycles += 1
        self.set_state("brake_cycles", self.brake_cycles)
        logger.info("test %s: brake cycle %d", self.test_id, self.brake_cycles)

    def result_metadata(self) -> dict:
        """What this run was, for the verdict - the operator's answers in the one record a reporting
        database ingests, where the state channels carry them per frame instead."""
        return {
            **self.run_details,
            "brake_cycles": self.brake_cycles,
            "trigger_speed_m_s": self._trigger_speed_m_s,
        }


class BrakeEnduranceTest(_BrakingYdriveTest):
    """Stops a moving load with the brake, over and over: run 110 -> 0, brake at speed, hold it there, return to 110, rest on the brake, recording the speed it engaged at and how far the load then travelled."""

    TEST_NAME = BRAKE_ENDURANCE_TEST_NAME

    START_LINE_DWELL_S = 300.0
    """How long each cycle rests at the start line, on the brake with the axis idle -
    nothing dissipates. Five minutes is ~12 events an hour, each from a brake that has
    had half as long to give its heat back."""

    def main_execution(self) -> None:
        # Asked first, while nothing is energized: it needs a person and does not
        # need the stand, and a run nobody can attribute to a DUT is not worth the
        # hours it takes.
        self.run_details = prompt_for_run_details(self, self.RUN_DETAIL_FIELDS)

        prepare_for_operation(self)
        # The trigger speed is above what the normal tuning allows, so the ceiling
        # is raised before anything moves - otherwise the axis clamps below the
        # trigger and the run-up never fires the brake. For the whole run: every
        # move this test makes is part of a braking cycle, so nothing here wants
        # the lower one.
        set_tuning_params(self, velocity_limit=self.BRAKE_RUN_VELOCITY_LIMIT)
        # Evaluation starts once the stand is in the state the bounds describe.
        # All three streams - see runner.start() for why a bound whose channel
        # reaches none of them reports a clean pass forever.
        self.runner.start(
            self.testbed.telemetry, self.testbed.supply_telemetry, self.testbed.tc_daq_telemetry
        )

        origin = establish_origin_by_hand(self)

        # Up to the start line once. establish_origin_by_hand left the load at 0
        # held by nothing, so the controller takes it back in place first.
        release_brake_in_place(self)
        move_to(self, origin + self.START_POSITION, arrival_timeout_s=self.MOVE_TIMEOUT_S)

        while True:
            # Run down toward 0 and let the brake stop it, wherever that is.
            brake_from_speed(
                self,
                target=origin + self.BRAKE_TARGET_POSITION,
                trigger_speed=self.trigger_speed_turns_s,
                post_brake_dwell_s=self.POST_BRAKE_DWELL_S,
                on_engaged=self._count_brake_event,
            )

            # Then hand the load back, return it to the start line, and rest
            # there on the brake before the next run-up.
            #
            # After the brake event, deliberately: a bad stop publishes
            # stopping_distance_m, stopping_distance_bound fires on it, and this
            # step's entry check raises before anything drives the load. So the
            # one run that ends with a brake that could not stop the load inside
            # MAX_STOPPING_DISTANCE_M is also the one that never moves it afterwards.
            release_brake_in_place(self)
            move_to(self, origin + self.START_POSITION, arrival_timeout_s=self.MOVE_TIMEOUT_S)
            dwell_braked(self, self.START_LINE_DWELL_S)


class CycleBrakeEnduranceTest(_BrakingYdriveTest):
    """Cycles the full stroke with the brake released, and every BRAKE_INTERVAL_M of measured
    travel lets the brake stop the moving load instead of the controller."""

    TEST_NAME = CYCLE_BRAKE_ENDURANCE_TEST_NAME

    BRAKE_INTERVAL_M = 1000.0
    """How far the counter must advance between brake events.

    Brake event N is owed at N times this, and the trigger compares the total
    against that multiple rather than counting down from the last event - so a
    cycle boundary landing past a multiple costs that interval a few metres and
    gives them straight back to the next one, with nothing accumulating.

    Checked between whole cycles, because a brake event runs the full stroke and
    has to start from the top. Measured at 1800 lb a cycle covers 24.3 m of track
    and takes 24 to 28 s, so an individual interval is 976 m to 1024 m with the
    average exactly this, and about 41 cycles and 17 to 20 minutes separate brake
    events - against a brake event of about 12 s.

    Metres of track, so it counts the overshoot past each end as well as the stroke
    - see the odrive's turns_traveled."""

    MARKER_POSITION = -15.0
    """Where the marker is, in the stroke's own coordinates: 15 turns ABOVE THE TOP, so
    NEGATIVE - BRAKE_TARGET_POSITION - 15, not START_POSITION + 15.

    THE SIGN IS THE WHOLE POINT. The camera looks at the top of the stroke, the top is
    0, and position decreases going up, so the parked fixture is at a negative number.
    Asserting a positive one told the axis it was a full stroke below where it was: the
    2026-08-25 14:23 run wrote 125, was commanded to 110, drove UP into the mechanical
    stop 7.9 turns later and sat there at the 18 A limit for 19 s. Nothing caught it,
    because 116 is within CYCLE_POSITION_TOLERANCE of 110.

    A PHYSICAL PLACE, and the reason this test's positions are absolute where
    BrakeEnduranceTest's are relative to a hand-set origin. The operator parks the
    fixture here at setup and pos_estimate is written to this number, so the same turn
    count means the same place in every run and stored positions compare across them.

    Also the clearance the brake needs. Measured turnarounds at 1800 lb reach 17.4 to
    18.5 turns past the commanded end, and that same run put the mechanical stop about
    8 turns above the park - so the whole excursion past the top fits in roughly 23
    turns, and this number is not free to grow."""

    CYCLE_VELOCITY_LIMIT = MAX_LOAD_VELOCITY_LIMIT
    """What the cycling runs at - the qualified duty tuning, not VELOCITY_LIMIT.

    The raised ceiling exists so the run-up can reach the trigger speed, and it goes
    on for the run-down alone. Accumulating a kilometre at a time above the duty
    tuning would change what that kilometre means."""

    CYCLE_POSITION_TOLERANCE = 10.0  # turns = 0.84 m
    """How near an end of the stroke counts as arrived while cycling.

    ON THIS STAND EVERY ARRIVAL OVERSHOOTS THIS: measured at 1800 lb the load runs
    17.3 turns past each end, so the peak can never satisfy arrival. The load
    reverses out there under its own momentum and is accepted on the way back, the
    first frame inside both this and CYCLE_VELOCITY_TOLERANCE. That excursion is
    still counted, because the driver counts the path frame by frame rather than the
    gap between setpoints - see the odrive's turns_traveled."""

    CYCLE_VELOCITY_TOLERANCE = 3.0  # turns/s = 0.25 m/s
    """How slowly the load has to be going to count as arrived.

    Wide enough that the pullback from an overshoot does not have to settle before
    the next leg is commanded, which is the time it saves. Which of this and
    CYCLE_POSITION_TOLERANCE is satisfied last depends on the arrival; neither is
    the binding one in general.

    THIS TOLERATES OVERSHOOT, IT DOES NOT REDUCE IT. The load travels as far past
    each end as the entry speed and the available deceleration put it; nothing here
    changes either.

    WHAT IT COSTS: a slow approach satisfies it too. An axis creeping at 2 turns/s
    while still 9 turns short - a jam, rising friction, a current-limited axis -
    reads as arrived and the cycle turns around, where under the default tolerances
    the same condition would raise move_to()'s arrival_timeout_s. Nothing is
    mis-reported, because the distance is measured either way and the short leg is
    counted short. What is lost is the signal that the stand is degrading, on a
    test built to run unattended for months. Detecting it properly needs a signed
    arrival - inside a tight tolerance, OR past the target and reversed - so that
    overshoot is recognized rather than tolerated by widening what arrival means."""

    def __init__(
        self,
        test_id: Optional[str] = None,
        use_mock: bool = False,
        require_engine: bool = True,
        trigger_speed_m_s: Optional[float] = None,
        brake_interval_m: Optional[float] = None,
    ):
        """brake_interval_m overrides BRAKE_INTERVAL_M so a shakedown needs no class edit."""
        super().__init__(test_id, use_mock, require_engine=require_engine, trigger_speed_m_s=trigger_speed_m_s)
        self._brake_interval_m = (
            brake_interval_m if brake_interval_m is not None else self.BRAKE_INTERVAL_M
        )
        self.distance_at_last_correction_m = 0.0
        """Where total_distance_m stood when the camera last re-referenced the axis."""
        self.position_claimed_at_marker: float = self.MARKER_POSITION
        """The position we tell the controller we are at when we detect the marker."""
        self._distance_at_run_start_m: Optional[float] = None
        """What the driver's travel counter read when this run's cycling began.

        Not zero, even though the testbed starts a fresh driver for every run: setup moves
        the load. The axis is idle while a person pushes it to the marker and the encoder
        counts every turn of it, which is not this run's mileage. None until
        main_execution seeds it, which is also what holds the derived channels back until
        there is something true to say."""

    def result_metadata(self) -> dict:
        """The base's answers plus the distance, which is this test's binding quantity and the one
        number a reader wants without querying the timeline."""
        return {
            **super().result_metadata(),
            "total_distance_m": self.total_distance_m,
            "brake_interval_m": self._brake_interval_m,
        }

    DERIVED_FROM_DEVICES = (DEVICE_ODRIVE,)

    def derived_channels(self, latest: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """This test's three distance channels, sampled off the ODrive's own travel count.

        Sampled rather than pushed because distance is the quantity this test is about, and
        a value pushed from a code path is only true at the moments that path runs: pushing
        it twice a cycle left the record holding one number for 270 consecutive frames and
        then jumping 10 m. None of these is counted here - all three are views of
        turns_traveled, so they cannot drift out of step with it or with each other."""
        frame = latest.get(DEVICE_ODRIVE)
        if frame is None or self._distance_at_run_start_m is None:
            return {}
        travelled = (
            self.testbed.turns_to_metres(frame["turns_traveled"]) - self._distance_at_run_start_m
        )
        return {
            "total_distance_m": travelled,
            "distance_since_brake_m": travelled - self.brake_cycles * self._brake_interval_m,
            "distance_since_correction_m": travelled - self.distance_at_last_correction_m,
        }

    @property
    def total_distance_m(self) -> float:
        """This run's distance in metres, read back from the state the derivation publishes.

        Read back rather than recomputed, so there is one computation of it: the loop that
        decides when a brake event is owed and the recorded channel cannot disagree."""
        return float(self.state_snapshot().get("total_distance_m", 0.0))

    def main_execution(self) -> None:
        # Asked first, while nothing is energized: it needs a person and does not
        # need the stand, and a run nobody can attribute to a DUT is not worth the
        # hours it takes.
        self.run_details = prompt_for_run_details(self, self.RUN_DETAIL_FIELDS)

        prepare_for_operation(self)
        # Opened at the cycling ceiling, not the raised one: all but a few seconds
        # of every interval is spent cycling, and VELOCITY_LIMIT goes on for the
        # run-down alone - see CYCLE_VELOCITY_LIMIT.
        set_tuning_params(self, velocity_limit=self.CYCLE_VELOCITY_LIMIT)
        # Evaluation starts once the stand is in the state the bounds describe.
        # All three streams - see runner.start() for why a bound whose channel
        # reaches none of them reports a clean pass forever.
        self.runner.start(
            self.testbed.telemetry, self.testbed.supply_telemetry, self.testbed.tc_daq_telemetry
        )

        # The operator parks the fixture at the marker and pos_estimate is written
        # there, so every position below is absolute and the same turn count means
        # the same place in every run - see MARKER_POSITION. It also picks the
        # camera, which is only identifiable while the marker is in view.
        establish_reference_by_camera(self, marker_position=self.MARKER_POSITION)
        release_brake_in_place(self)

        # The driver's counter starts when its process does, and setup moves the load:
        # the axis is idle while a person pushes it to the marker, and the encoder counts
        # every turn. Where that push started depends on where the last run left the
        # load, so booking it would put the first brake event early by a different
        # amount every run. This run's cycling mileage starts here, not there.
        self._distance_at_run_start_m = self.testbed.get_distance_travelled_m()

        # The load is at the marker - the operator left it there and
        # release_brake_in_place() took it back without moving it - so the move to the
        # start line is travel like any other. Through cycle_leg like every other leg
        # of this stroke: holding the first arrival to a tighter gate than the
        # thousands after it would make it the one leg that waits out a pullback.
        cycle_leg(self, self.START_POSITION)

        while True:
            # Brake event N is owed at N intervals of travel, compared against the
            # total rather than counted down from the last event - see
            # BRAKE_INTERVAL_M. Checked between whole cycles, and the body ends at the
            # start line, which is where a brake event has to begin.
            brake_owed_at_m = (self.brake_cycles + 1) * self._brake_interval_m
            while self.total_distance_m < brake_owed_at_m:
                # The leg to the top is the one the camera watches - see
                # MARKER_POSITION. Watched all the way rather than read at the end,
                # because the fixture crosses the view during the turnaround and is
                # gone. One leg per cycle, so nothing has to arm and fire to avoid
                # correcting twice; the correction lands between legs.
                watch = MarkerWatch(self)
                cycle_leg(self, self.BRAKE_TARGET_POSITION, each_frame=watch)
                watch.apply()
                # The body still ENDS at the start line, which is where a brake
                # event has to begin.
                cycle_leg(self, self.START_POSITION)
                logger.info(
                    "test %s: %.1f m travelled, brake event %d owed at %.0f m",
                    self.test_id, self.total_distance_m, self.brake_cycles + 1, brake_owed_at_m,
                )

            # The ceiling goes up only now, for the run-down, and comes back off
            # before anything else moves.
            set_tuning_params(self, velocity_limit=self.BRAKE_RUN_VELOCITY_LIMIT)
            brake_from_speed(
                self,
                target=self.BRAKE_TARGET_POSITION,
                trigger_speed=self.trigger_speed_turns_s,
                post_brake_dwell_s=self.POST_BRAKE_DWELL_S,
                on_engaged=self._count_brake_event,
            )
            # Tuning restored BEFORE the load is handed back, so the cycling that
            # follows runs under the duty ceiling instead of the run-down's. Written
            # while the axis is still idle behind the engaged brake, which is where
            # brake_from_speed left it.
            set_tuning_params(self, velocity_limit=self.CYCLE_VELOCITY_LIMIT)
            # After the brake event, deliberately: a bad stop publishes
            # stopping_distance_m, stopping_distance_bound fires on it, and this
            # step's entry check raises before anything drives the load. So the one
            # cycle that ends with a brake which could not stop the load is also the
            # one that never moves it afterwards.
            release_brake_in_place(self)


class ManualTest(BaseYdriveTest):
    """No test sequence of its own - keeps the ODrive driver process and command/telemetry endpoints alive, under live Rulebook evaluation, for an operator to command/view directly (e.g. via a GUI) until stopped."""

    TEST_NAME = MANUAL_TEST_NAME

    def main_execution(self) -> None:
        # All three streams, so the thermal and bus bounds cover an operator's
        # session too - see EnduranceCycleTest.
        self.runner.start(
            self.testbed.telemetry, self.testbed.supply_telemetry, self.testbed.tc_daq_telemetry
        )
        self.wait_for(float("inf"))
