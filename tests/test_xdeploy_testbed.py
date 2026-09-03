"""The xdeploy stand's shape and its rulebook, plus the parts of XdeployTestbed
that can be checked without the hardware attached.

start()/stop() launch driver subprocesses and talk to a real ODrive and a real
thermocouple DAQ, so they are not exercised here. What is: that the stand
declares the two devices it owns and touches nothing else, that it writes the
stand's current ceilings and nothing more, that its teardown is
honest about what it cannot safe, that every bound names a channel some device
actually publishes, and that ManualTest waits for a person before it starts
evaluating - all of which are ordinary logic and all of which would be silently
wrong in ways a live run might not reveal.
"""
from __future__ import annotations

import inspect

import pytest

from protocol.wire import (
    DEVICE_ODRIVE,
    DEVICE_TC_DAQ,
    TELEMETRY_ENDPOINTS,
)
from hardware.odrive.odrive_channels import TELEMETRY_CHANNELS as ODRIVE_TELEMETRY_CHANNELS
from hardware.tc_daq.tc_daq_channels import TELEMETRY_CHANNELS as TC_DAQ_TELEMETRY_CHANNELS
from testbeds.xdeploy_testbed.xdeploy_testbed import (
    Motion,
    ODRIVE_MOTOR_HARD_MAX_A,
    ODRIVE_MOTOR_SOFT_MAX_A,
    XdeployTestbed,
)
from testcases.xdeploy.channels import DEFAULT_STATE
from testcases.xdeploy.rulebooks.xdeploy_rulebook import (
    LIVE_TC_CHANNELS,
    MAX_BUS_VOLTAGE_V,
    MAX_FET_TEMPERATURE_C,
    MAX_TEMPERATURE_C,
    MIN_BUS_VOLTAGE_V,
    XDEPLOY_RULEBOOK,
)
from testcases.xdeploy.testcases.base_xdeploy_test import BaseXdeployTest
from testcases.xdeploy.testcases.testcases import CycleTest, ManualTest
from testcases.xdeploy.teststeps import teststeps


# --- what the stand is -------------------------------------------------------


def test_the_stand_owns_exactly_the_odrive_and_the_thermocouple_daq():
    """Two devices, and the test case claims the same two. A device declared here
    but not recorded by the engine refuses to start the run; one recorded but not
    declared goes to the continuous session record instead of to this run's
    directory."""
    assert XdeployTestbed.DEVICES == (DEVICE_ODRIVE, DEVICE_TC_DAQ)
    assert BaseXdeployTest.DEVICES == XdeployTestbed.DEVICES


def test_every_declared_device_is_one_the_engine_can_subscribe_to():
    """A device name with no endpoint is a device nothing records, and the run
    would fail at require_recording_started() rather than here."""
    for device in XdeployTestbed.DEVICES:
        assert device in TELEMETRY_ENDPOINTS


def test_the_stand_holds_no_supply_client():
    """The bench supply is outside this framework: no client, no setpoint, no
    telemetry. If one ever appears, the rulebook's undervoltage_bound stops being
    the only account of the bus and ManualTest's opening prompt stops being the
    only way to satisfy it - so both have to be revisited, not just this test."""
    assert not hasattr(XdeployTestbed, "supply")
    assert not hasattr(XdeployTestbed, "power_motor_bus")
    assert not hasattr(XdeployTestbed, "check_rails")


def test_start_writes_the_stand_s_current_ceilings():
    """The motor's ratings and this stand's regen threshold are the stand's to
    own, so they are written every run rather than inherited from whoever last
    touched the board - which is also what lets homing drop the limit and put it
    back without reading the board's leftovers."""
    source = inspect.getsource(XdeployTestbed.start)
    assert "set_motor_config_current_soft_max(ODRIVE_MOTOR_SOFT_MAX_A)" in source
    assert "set_motor_config_current_hard_max(ODRIVE_MOTOR_HARD_MAX_A)" in source
    assert "set_board_config_max_regen_current(ODRIVE_MAX_REGEN_CURRENT_A)" in source
    assert source.index("verify_actions") < source.index("set_motor_config"), (
        "a write to an action the board does not have should fail as a missing action, "
        "not as a command timeout"
    )


def test_start_writes_no_tuning_and_nothing_persistent():
    """The controller tuning is the board's own and a run uses it as found, so
    start() writes no gain, no velocity limit and no filter bandwidth. It also
    never saves: the ceilings above live in RAM, so a run cannot leave the board
    configured differently than it found it."""
    source = inspect.getsource(XdeployTestbed.start)
    assert "set_controller_config" not in source
    assert "save_configuration" not in source


def test_start_writes_no_trip_levels():
    """undervoltage_bound mirrors the board's own trip level, and
    overvoltage_bound is the only overvoltage protection there is. Writing either
    trip here would move the thing those bounds are calibrated against."""
    source = inspect.getsource(XdeployTestbed.start)
    assert "dc_bus_undervoltage_trip_level" not in source
    assert "dc_bus_overvoltage_trip_level" not in source


def test_the_soft_limit_leaves_headroom_under_the_hard_one():
    """The soft limit is what the stand drives to and the hard one is what trips
    CURRENT_LIMIT_VIOLATION. Inverted or equal, the trip fires on ordinary work."""
    assert 0 < ODRIVE_MOTOR_SOFT_MAX_A < ODRIVE_MOTOR_HARD_MAX_A


def test_teardown_disarms_the_axis_before_it_closes_anything():
    """Disarming is the most a teardown can do here: there is no rail to drop and
    no brake to engage. It has to happen while the command client is still open,
    which is why it is the first step rather than one of them."""
    source = inspect.getsource(XdeployTestbed.stop)
    disarm = source.index("disarm the ODrive axis")
    disconnect = source.index("disconnect the ODrive backend")
    close = source.index("close {type(client).__name__}")
    assert disarm < disconnect < close


def test_teardown_switches_no_power_because_it_holds_none():
    """A teardown that appeared to de-energize this stand would be lying: the bus
    is a bench supply with no client here. If a supply is ever added, stop()
    gains a step and this test is what says so."""
    source = inspect.getsource(XdeployTestbed.stop)
    assert "enable_output" not in source
    assert "power_" not in source


def test_the_position_guard_cannot_block_the_shutdown():
    """stop() is what disarms the axis. If it read a position, an unusable one
    would raise mid-sequence and leave the drive armed."""
    assert "pos_estimate" not in inspect.getsource(XdeployTestbed.stop)
    assert "get_motion" not in inspect.getsource(XdeployTestbed.stop)


def test_teardown_continues_after_a_failing_step():
    """One unresponsive client must not leave the rest of the stand up."""
    calls = []

    def boom():
        calls.append("boom")
        raise RuntimeError("client is wedged")

    XdeployTestbed._safe("a step that fails", boom)
    XdeployTestbed._safe("a step that follows it", lambda: calls.append("ran anyway"))
    assert calls == ["boom", "ran anyway"]


def test_every_client_accessor_raises_before_start():
    """A client used before start() is a None dereference somewhere further on,
    where the message names neither the client nor the reason."""
    testbed = XdeployTestbed()
    for name in ("command", "telemetry", "sync_telemetry", "tc_daq_telemetry",
                 "sync_tc_daq_telemetry"):
        with pytest.raises(RuntimeError, match="before start"):
            getattr(testbed, name)


def test_the_runner_and_this_process_do_not_share_a_subscription():
    """A SUB socket is not thread-safe and a subscription delivers each frame
    once, so a testbed reading the runner's client would both tear messages and
    take frames the bounds were meant to see."""
    testbed = XdeployTestbed()
    testbed._telemetry = object()
    testbed._sync_telemetry = object()
    testbed._tc_daq_telemetry = object()
    testbed._sync_tc_daq_telemetry = object()
    assert testbed.telemetry is not testbed.sync_telemetry
    assert testbed.tc_daq_telemetry is not testbed.sync_tc_daq_telemetry


# --- driver log wiring -------------------------------------------------------


def test_each_driver_is_told_where_to_write_its_log():
    """The driver knows nothing about runs; the testbed that starts it is the
    only participant holding both the run's test_id and the engine's output dir,
    so it composes the path and passes it in."""
    from pathlib import Path

    from protocol.paths import DRIVER_LOG_FILENAME

    testbed = XdeployTestbed(output_dir=Path("/out"), test_id="run-42")
    for device in XdeployTestbed.DEVICES:
        flag, path = testbed._log_args(device)
        assert flag == "--log-file"
        assert path == f"/out/runs/run-42/{device}/{DRIVER_LOG_FILENAME}"


def test_without_a_run_the_drivers_are_given_no_log_flag():
    """A testbed used outside a run - a demo, a bring-up script - must still
    start, logging to the console only."""
    assert XdeployTestbed()._log_args(DEVICE_ODRIVE) == []
    assert XdeployTestbed(output_dir=None, test_id="run-42")._log_args(DEVICE_ODRIVE) == []
    assert XdeployTestbed(output_dir="/out", test_id=None)._log_args(DEVICE_ODRIVE) == []


# --- the rulebook ------------------------------------------------------------


def test_every_bounded_channel_is_one_something_actually_publishes():
    """A bound on a channel nothing publishes is never evaluated and never
    complains - it sits there reporting a clean pass on every frame. That is
    worse than no bound at all, so the declared surfaces are the check."""
    published = (set(ODRIVE_TELEMETRY_CHANNELS) | set(TC_DAQ_TELEMETRY_CHANNELS)
                 | set(DEFAULT_STATE))
    for bound in XDEPLOY_RULEBOOK.bounds:
        assert bound.channel in published, (
            f"{bound.label} bounds {bound.channel!r}, which neither an xdeploy device "
            "nor the run's own state publishes"
        )


def test_the_rulebook_spans_both_devices():
    """Which is why a runner has to be started against both streams. If every
    bound came from one device this would be a needless complication; it does
    not, and a runner given fewer silently evaluates only part."""
    bounded = {b.channel for b in XDEPLOY_RULEBOOK.bounds}
    assert bounded & set(ODRIVE_TELEMETRY_CHANNELS), "no bound reads the ODrive"
    assert bounded & set(TC_DAQ_TELEMETRY_CHANNELS), "no bound reads the thermocouple DAQ"


def test_the_bus_is_bounded_at_the_drive_and_nowhere_else():
    """There is no supply telemetry on this stand, so what the drive measures is
    the only account of the bus. The threshold is the board's own
    dc_bus_undervoltage_trip_level, so the run ends where the firmware acts."""
    undervoltage = [b for b in XDEPLOY_RULEBOOK.bounds if b.label == "undervoltage_bound"]
    assert len(undervoltage) == 1
    bound = undervoltage[0]
    assert bound.channel == "board_vbus_voltage"
    assert bound.lower == MIN_BUS_VOLTAGE_V == 10.5
    assert bound.upper is None
    assert bound.fatal
    assert bound.persistence_s is None, (
        "a bus already below the firmware's own trip level is not a sample to wait out"
    )


def test_only_the_wired_thermocouples_are_bounded():
    """The DAQ streams eight channels and publishes None for one it cannot read.
    A numeric bound on a None is unevaluable, and the runner stops a run it
    cannot evaluate - so bounding an unconnected channel aborts every run on its
    first frame."""
    bounded = {b.channel for b in XDEPLOY_RULEBOOK.bounds if b.channel.startswith("temperature_")}
    assert bounded == {f"temperature_{n}_c" for n in LIVE_TC_CHANNELS}


def test_the_thermal_bounds_are_fatal_and_tolerate_a_dropped_sample():
    """A thermocouple spikes from electrical noise as well as heat, and this DAQ
    drops the odd sample - so a violation is debounced and a momentary FAULT is
    given a separate, longer window before it stops the run."""
    thermal = [b for b in XDEPLOY_RULEBOOK.bounds if b.channel.startswith("temperature_")]
    assert thermal
    for bound in thermal:
        assert bound.upper == MAX_TEMPERATURE_C
        assert bound.lower is None      # cold is not a fault on this stand
        assert bound.fatal
        assert bound.persistence_s == 5.0
        assert bound.unevaluable_grace_s == 10.0


def test_every_bound_is_fatal():
    """This rulebook is a safety net, not a report. A non-fatal bound here would
    record a violation and let the run continue."""
    assert all(bound.fatal for bound in XDEPLOY_RULEBOOK.bounds)


def test_the_rulebook_names_every_xdeploy_test():
    """A test whose TEST_NAME is missing here is not looked up against these
    bounds by anything that resolves rulebooks by name - offline replay included."""
    assert BaseXdeployTest.TEST_NAME in XDEPLOY_RULEBOOK.test_names
    assert ManualTest.TEST_NAME in XDEPLOY_RULEBOOK.test_names
    assert CycleTest.TEST_NAME in XDEPLOY_RULEBOOK.test_names


def test_the_stand_carries_no_borrowed_motion_bound():
    """Nothing about xdeploy's motion has been measured, so no bound claims to
    know it. This is a deliberate, documented gap rather than an oversight - the
    axis is gravity-loaded with no brake, so a runaway currently ends the run
    only if it also takes the bus down or heats something. Delete this test on
    the day a measured overspeed bound replaces it."""
    bounded = {b.channel for b in XDEPLOY_RULEBOOK.bounds}
    assert "vel_estimate" not in bounded
    assert "motor_foc_iq_measured" not in bounded


# --- the manual test ---------------------------------------------------------


def test_the_manual_test_asks_before_it_starts_evaluating():
    """undervoltage_bound is fatal, ungated and undebounced, and nothing on this
    stand can energize the bus - so a runner started first ends the run on its
    first frame, every time, on a stand whose supply a person switches by hand.
    The order is the whole reason this test case overrides main_execution."""
    source = inspect.getsource(ManualTest.main_execution)
    assert source.index("await_operator") < source.index("runner.start"), (
        "the operator must be asked to bring the bus up before the runner starts"
    )


def test_the_manual_test_evaluates_both_streams():
    """A bound whose channel is absent from a frame returns no result, so a
    runner given one stream silently evaluates half this rulebook and passes the
    rest."""
    source = inspect.getsource(ManualTest.main_execution)
    assert "testbed.telemetry" in source
    assert "testbed.tc_daq_telemetry" in source


def test_the_manual_test_runs_until_it_is_stopped():
    """It has no sequence of its own: the point is to hold the drivers and their
    endpoints up under live evaluation for as long as somebody is working."""
    assert 'wait_for(float("inf"))' in inspect.getsource(ManualTest.main_execution)


# --- state channels ----------------------------------------------------------


def test_the_operator_prompt_channels_are_seeded():
    """The engine fixes a wide file's header from its first frames and drops a
    channel that appears later, so a prompt answered thirty seconds into a run
    would be missing from the record while the run reported a clean pass."""
    for channel in ("operator_prompt", "dut_serial_number", "er_ticket", "load_lb"):
        assert channel in DEFAULT_STATE


def test_the_serial_prompt_offers_this_stand_s_units_only():
    """The dropdown is the check. A dropdown listing every serial in the building
    checks almost nothing, and a run filed against another stand's unit looks
    correctly filed."""
    serial_field = next(f for f in BaseXdeployTest.RUN_DETAIL_FIELDS if f.channel == "dut_serial_number")
    assert serial_field.choices == ("XDEPLOY3",)


# --- the bus and FET bounds --------------------------------------------------


def test_the_bus_is_bounded_above_as_well_as_below():
    """Every cycle lowers a gravity load onto a bench supply that cannot sink,
    and this board has no overvoltage trip of its own - so this bound and the
    brake resistor are the whole of the protection."""
    overvoltage = [b for b in XDEPLOY_RULEBOOK.bounds if b.label == "overvoltage_bound"]
    assert len(overvoltage) == 1
    bound = overvoltage[0]
    assert bound.channel == "board_vbus_voltage"
    assert bound.upper == MAX_BUS_VOLTAGE_V == 52.0
    assert bound.lower is None
    assert bound.fatal
    assert bound.persistence_s is None, (
        "with no firmware trip underneath it, a bus climbing past this is not a "
        "sample to wait out"
    )


def test_the_bus_bounds_do_not_overlap():
    """A floor above the ceiling would make every frame violate one of them."""
    assert MIN_BUS_VOLTAGE_V < MAX_BUS_VOLTAGE_V


def test_the_drive_s_own_temperature_is_bounded_and_debounced():
    """One thermistor sampled every frame, on a run that may cycle for days: a
    single bad reading must not end it."""
    fet = [b for b in XDEPLOY_RULEBOOK.bounds if b.label == "fet_overtemperature_bound"]
    assert len(fet) == 1
    bound = fet[0]
    assert bound.channel == "motor_fet_thermistor_temperature"
    assert bound.upper == MAX_FET_TEMPERATURE_C == 80.0
    assert bound.fatal
    assert bound.persistence_s == 5.0


def test_a_cycle_waits_before_the_fatal_thermal_bounds_are_reached():
    """The waits exist so a warm lab collapses the cycle rate instead of ending
    the run. A wait threshold at or above its bound would never get the chance."""
    assert teststeps.FET_WAIT_C < MAX_FET_TEMPERATURE_C
    assert MAX_TEMPERATURE_C - teststeps.TC_HEADROOM_C < MAX_TEMPERATURE_C


# --- the stroke --------------------------------------------------------------


def test_the_stroke_runs_from_the_home_stop_out_to_full_deploy():
    """Deploy is negative and retract is positive, so every cycled position is at
    or below home. Ordering, not magnitudes: if these ever invert, the cycle
    drives into the hard stop instead of away from it."""
    assert teststeps.FULL_DEPLOY < teststeps.FULL_RETRACT < teststeps.HOME_POSITION


APPROXIMATE_GROUND_CONTACT = -40.0
"""Roughly where the load reaches the ground on the stand as it is today.

Lives here rather than in the teststeps because nothing is commanded against it -
it is not stand configuration, just the fact that makes the assertion below mean
something."""


def test_the_cycle_rests_where_the_load_is_on_the_ground():
    """The dwell, the thermal wait and the teardown target all sit at
    FULL_RETRACT with the axis armed but unloaded. That is only safe while
    FULL_RETRACT is retract of where the load sets down - move it deploy of that
    and every dwell is spent holding the load up on the controller alone."""
    assert teststeps.FULL_RETRACT > APPROXIMATE_GROUND_CONTACT
    assert teststeps.FULL_DEPLOY < APPROXIMATE_GROUND_CONTACT, (
        "the cycle would never lift the load at all"
    )


def test_the_cycle_stops_short_of_the_hard_stop():
    """FULL_RETRACT is clearance: cycling into the stop would drive the actuator
    against it thousands of times at full cycling current."""
    assert teststeps.FULL_RETRACT < teststeps.HOME_POSITION
    assert abs(teststeps.FULL_RETRACT - teststeps.HOME_POSITION) >= 1.0


def test_homing_creeps_toward_the_stop_at_a_reduced_current():
    """It pushes into a hard stop, so it does it slowly and with less current
    available than cycling gets."""
    assert teststeps.HOMING_SPEED_TURNS_S > 0, "positive is toward the retract stop"
    assert teststeps.HOMING_CURRENT_A < ODRIVE_MOTOR_SOFT_MAX_A


def test_homing_restores_the_cycling_current_limit_from_the_stand_not_the_board():
    """Nothing reads the board's leftover limit to put back, so a run that dies
    mid-home cannot ratchet the limit down for the next one."""
    source = inspect.getsource(teststeps.home_axis)
    assert "set_motor_config_current_soft_max(HOMING_CURRENT_A)" in source
    assert "set_motor_config_current_soft_max(ODRIVE_MOTOR_SOFT_MAX_A)" in source


def test_homing_idles_before_it_rezeroes_the_board():
    """The rezero itself is impulse-free - firmware shifts input_pos and
    pos_setpoint with it - so this is about the creep: the axis must stop pushing
    on the hard stop before homing hands it over, not go on driving into it while
    the frame is rewritten underneath."""
    source = inspect.getsource(teststeps.home_axis)
    assert source.index('set_axis_state("IDLE")') < source.index("set_pos_estimate")


# --- finding the hard stop ---------------------------------------------------


class _FakeCommand:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, args))
        return record


class _FakeTestbed:
    """Replays a scripted velocity profile through get_motion()."""

    def __init__(self, velocities, position=5.0):
        self._velocities = list(velocities)
        self.position = position
        self.command = _FakeCommand()
        self.frames = 0

    def get_motion(self):
        self.frames += 1
        velocity = self._velocities.pop(0) if self._velocities else 0.0
        self.position += velocity * 0.01
        return Motion(position=self.position, velocity=velocity, armed=True)

    def describe_errors(self):
        return {}


class _FakeTestCase:
    def __init__(self, testbed):
        self.testbed = testbed
        self.test_id = "fake-run"

    def check_should_continue(self):
        pass


def test_the_creep_is_not_declared_home_before_it_has_started_moving(monkeypatch):
    """THE BUG THIS EXISTS TO PREVENT: the axis is at rest the instant it arms,
    so stall detection with no spin-up grace reads the first frames as the stop
    and homes wherever the axis happened to be."""
    monkeypatch.setattr(teststeps, "HOMING_STALL_GRACE_S", 0.2)
    # At rest, then moving, then genuinely stalled against the stop.
    at_rest = [0.0] * 5
    moving = [5.0] * 40
    stalled = [0.0] * teststeps.HOMING_STALL_FRAMES
    testbed = _FakeTestbed(at_rest + moving + stalled)
    stopped_at = teststeps._creep_to_stop(_FakeTestCase(testbed))
    assert testbed.frames > len(at_rest), (
        "homing returned during the opening frames, before the axis had moved at all"
    )
    assert stopped_at == pytest.approx(testbed.position)


def test_an_axis_already_against_the_stop_still_homes(monkeypatch):
    """A run that died against the stop leaves the next one starting there. It
    never moves, so the grace has to end in the stop being found rather than in a
    timeout."""
    monkeypatch.setattr(teststeps, "HOMING_STALL_GRACE_S", 0.05)
    testbed = _FakeTestbed([0.0] * 500)
    assert teststeps._creep_to_stop(_FakeTestCase(testbed)) == pytest.approx(5.0)


def test_one_slow_frame_is_not_the_stop(monkeypatch):
    """Velocity dips - a tight spot, a sample of noise. The stop is a sustained
    stall, which is what HOMING_STALL_FRAMES counts."""
    monkeypatch.setattr(teststeps, "HOMING_STALL_GRACE_S", 0.0)
    dip = [5.0, 5.0, 0.0, 5.0, 5.0]
    testbed = _FakeTestbed(dip * 3 + [5.0] * 20 + [0.0] * teststeps.HOMING_STALL_FRAMES)
    teststeps._creep_to_stop(_FakeTestCase(testbed))
    assert testbed.frames > len(dip) * 3, "a single slow frame was taken for the stop"


def test_a_creep_that_never_stops_is_reported(monkeypatch):
    """No stop found means the axis is still moving and nothing caught it, which
    is not a position to zero the board against."""
    monkeypatch.setattr(teststeps, "HOMING_STALL_GRACE_S", 0.0)
    monkeypatch.setattr(teststeps, "HOMING_TIMEOUT_S", 0.2)
    with pytest.raises(TimeoutError, match="no hard stop found"):
        teststeps._creep_to_stop(_FakeTestCase(_FakeTestbed([5.0] * 100000)))


def test_a_creep_that_turns_into_a_descent_is_reported(monkeypatch):
    """HOMING_CURRENT_A cannot hold a lifted load back - it is a fifth of what
    lifting takes - so a run starting with the load up accelerates downhill
    instead of creeping. The axis stays armed the whole way, so without this the
    stop is still found and an uncontrolled descent is recorded as a normal
    homing."""
    monkeypatch.setattr(teststeps, "HOMING_STALL_GRACE_S", 0.0)
    runaway = [5.0, 8.0, 14.0, 25.0]
    with pytest.raises(RuntimeError, match="running away"):
        teststeps._creep_to_stop(_FakeTestCase(_FakeTestbed(runaway)))


def test_the_runaway_threshold_leaves_room_for_an_ordinary_creep(monkeypatch):
    """It has to sit above the commanded speed, or homing trips on itself."""
    assert teststeps.HOMING_RUNAWAY_SPEED_TURNS_S > teststeps.HOMING_SPEED_TURNS_S
    monkeypatch.setattr(teststeps, "HOMING_STALL_GRACE_S", 0.0)
    at_speed = [teststeps.HOMING_SPEED_TURNS_S] * 30
    testbed = _FakeTestbed(at_speed + [0.0] * teststeps.HOMING_STALL_FRAMES)
    teststeps._creep_to_stop(_FakeTestCase(testbed))  # does not raise


def test_a_disarm_during_the_creep_is_not_read_as_the_stop(monkeypatch):
    """A disarmed axis reads zero velocity. Taken for the stop, homing would zero
    the board against wherever the fault happened."""
    monkeypatch.setattr(teststeps, "HOMING_STALL_GRACE_S", 0.0)

    testbed = _FakeTestbed([5.0] * 10)
    testbed.get_motion = lambda: Motion(position=3.0, velocity=0.0, armed=False)
    with pytest.raises(RuntimeError, match="stopped driving"):
        teststeps._creep_to_stop(_FakeTestCase(testbed))


# --- the cycle test ----------------------------------------------------------


def test_the_cycle_test_asks_before_it_starts_evaluating():
    """Same reason as ManualTest: nothing here can energize the bus, and
    undervoltage_bound would end the run on its first frame."""
    source = inspect.getsource(CycleTest.main_execution)
    assert source.index("await_operator") < source.index("runner.start")


def test_the_cycle_test_evaluates_both_streams():
    """A bound whose channel is absent from a frame returns no result, so one
    stream would leave the thermocouple bounds silently unevaluated."""
    source = inspect.getsource(CycleTest.main_execution)
    assert "testbed.telemetry" in source
    assert "testbed.tc_daq_telemetry" in source


def test_homing_is_supervised():
    """The creep pushes into a hard stop. Starting the runner first is what puts
    the bus and thermal bounds over it."""
    source = inspect.getsource(CycleTest.main_execution)
    assert source.index("runner.start") < source.index("home_axis")


def test_nothing_moves_before_the_axis_has_been_homed():
    """Every target is absolute against a zero homing writes. Commanded before
    that, -110 means somewhere nobody chose."""
    source = inspect.getsource(CycleTest.main_execution)
    assert source.index("home_axis") < source.index("move_to")


def test_the_cycle_never_idles_the_axis():
    """With no brake the controller is the only thing holding the load, so a
    disarm inside the loop is a fault rather than a step - which is only true
    while no step in the loop performs one."""
    source = inspect.getsource(teststeps.cycle_position_forever)
    assert "IDLE" not in source
    assert "set_axis_state" not in source


def test_travel_is_counted_from_where_cycling_began():
    """The driver's counter runs from its own connect, so the homing creep and
    the one-off move down from the stop would otherwise be booked as cycling
    travel. Taken at FULL_RETRACT, which is where every cycle starts and ends."""
    source = inspect.getsource(CycleTest.main_execution)
    origin = source.index("_turns_at_cycling_start")
    assert source.index("home_axis") < origin
    assert source.index("move_to(self, FULL_RETRACT)") < origin
    assert origin < source.index("cycle_position_forever")


def test_the_cycle_time_excludes_the_dwell_and_any_thermal_wait():
    """It is the number a cycle_time_bound would later be set from, so a cycle
    that had to cool has to stay comparable with one that did not."""
    source = inspect.getsource(teststeps.cycle_position_forever)
    taken = source.index('set_state("cycle_time_s"')
    assert taken < source.index("wait_for(dwell_s)")
    # The call, not the mention of it in the comment above the loop.
    assert taken < source.index("wait_for_thermal_headroom(test_case)")


def test_the_cycle_loop_is_not_itself_a_step():
    """It contains move_to() and wait_for_thermal_headroom(), which are. A step
    inside a step reports twice for one action and overwrites current_step, so
    the recorded step would be the loop rather than the move it is doing."""
    assert not hasattr(teststeps.cycle_position_forever, "__wrapped__"), (
        "cycle_position_forever is decorated as a @step but contains steps"
    )
    for contained in ("move_to", "wait_for_thermal_headroom"):
        assert hasattr(getattr(teststeps, contained), "__wrapped__"), (
            f"{contained} is no longer a @step - the reason this one cannot be has changed"
        )


def test_a_cycle_ends_where_the_next_one_starts():
    """The loop goes to the top and back, so the position it leaves the axis at
    has to be the one it began from - otherwise the count and the travel drift
    apart from the geometry they claim to describe."""
    source = inspect.getsource(teststeps.cycle_position_forever)
    assert source.index("move_to(test_case, FULL_DEPLOY)") < source.index(
        "move_to(test_case, FULL_RETRACT)"
    )


def test_teardown_returns_the_load_before_the_stand_is_shut_down():
    """XdeployTestbed.stop() disarms, and a disarm at full deploy drops the load
    the length of the stroke."""
    source = inspect.getsource(CycleTest.post_test_teardown)
    assert "park_for_teardown" in source
    assert source.index("park_for_teardown") < source.index("super().post_test_teardown()")


def test_teardown_moves_nothing_before_the_axis_has_been_homed():
    """Before homing no absolute target means anything, and nothing has been
    lifted - so there is nothing to bring down and nowhere to send it."""
    assert "self._homed" in inspect.getsource(CycleTest.post_test_teardown)


def test_the_cycle_endpoints_are_not_a_parameter():
    """main_execution positions the axis at FULL_RETRACT and takes the travel
    origin there. If the loop could be pointed somewhere else, those two would
    silently disagree about where a cycle starts."""
    parameters = inspect.signature(teststeps.cycle_position_forever).parameters
    assert "bottom" not in parameters
    assert "top" not in parameters


def test_the_teardown_target_is_the_cycle_s_own_rest_position():
    """Not the hard stop: parking against it leaves the next run's creep no
    travel in which to tell arrival from an axis that never moved."""
    signature = inspect.signature(teststeps.park_for_teardown)
    assert signature.parameters["target"].default == teststeps.FULL_RETRACT


def test_every_channel_the_cycle_test_publishes_is_seeded():
    """The engine fixes each wide file's header from its first frames and drops a
    channel that appears later - so a measurement first written mid-run is
    missing from the record while the run reports a clean pass."""
    written = set()
    for source in (
        inspect.getsource(CycleTest.main_execution),
        inspect.getsource(CycleTest.derived_channels),
        inspect.getsource(teststeps),
    ):
        for line in source.splitlines():
            if 'set_state("' in line:
                written.add(line.split('set_state("')[1].split('"')[0])
            if '"total_travel_turns"' in line:
                written.add("total_travel_turns")
    assert written, "found no published channels to check - the scrape stopped working"
    for channel in written:
        assert channel in DEFAULT_STATE, (
            f"{channel!r} is published but not seeded in channels.py, so the engine drops it"
        )
