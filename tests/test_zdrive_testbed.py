"""The zdrive stand's power configuration and its rulebook, plus the parts of
ZdriveTestbed that can be checked without the hardware attached.

start()/stop() launch driver subprocesses and talk to a real ODrive and two real
supplies, so they are not exercised here. What is: the bus and rail declarations,
the envelope arithmetic that decides whether a configured current limit is even
reachable, the regen pair that has to stay ordered for the bus to hold its
setpoint, the teardown ordering that the brake's magnet-applied behaviour depends
on, the setpoint check, and whether every bound in the rulebook names a channel
some device actually publishes - all of which are ordinary logic and all of which
would be silently wrong in ways a live run might not reveal.
"""
from __future__ import annotations

import inspect

import pytest

from protocol.wire import (
    DEVICE_CPX400DP,
    DEVICE_N6974A,
    DEVICE_ODRIVE,
    DEVICE_TC_DAQ,
    TELEMETRY_ENDPOINTS,
)
from hardware.cpx400dp.rails import (
    MAX_CURRENT_A,
    MAX_VOLTAGE_V,
    POWER_ENVELOPE_W,
    Rail,
    deliverable_current_a,
)
from hardware.cpx400dp.cpx400dp_channels import OUTPUTS as CPX400DP_OUTPUTS
from hardware.n6974a.n6974a_channels import (
    SINK_FRACTION_BY_DISSIPATORS,
    TELEMETRY_CHANNELS as N6974A_TELEMETRY_CHANNELS,
)
from hardware.odrive.odrive_channels import TELEMETRY_CHANNELS as ODRIVE_TELEMETRY_CHANNELS
from hardware.tc_daq.tc_daq_channels import TELEMETRY_CHANNELS as TC_DAQ_TELEMETRY_CHANNELS
from testbeds.zdrive_testbed.zdrive_testbed import (
    BRAKE_BUS,
    MOTOR_BUS,
    N6974A_DISSIPATORS,
    ODRIVE_BUS_HARD_MAX_A,
    ODRIVE_BUS_SOFT_MAX_A,
    ODRIVE_MAX_REGEN_CURRENT_A,
    ODRIVE_MOTOR_HARD_MAX_A,
    ODRIVE_MOTOR_SOFT_MAX_A,
    RAILS,
    ZdriveTestbed,
)
from testcases.zdrive.channels import DEFAULT_STATE
from testcases.zdrive.rulebooks.zdrive_rulebook import (
    MAX_AXIS_SPEED_TURNS_S,
    MAX_BUS_CURRENT_A,
    MAX_BUS_VOLTAGE_V,
    MAX_MOTOR_CURRENT_A,
    MIN_BUS_CURRENT_A,
    ZDRIVE_RULEBOOK,
)


# --- what the stand is wired as ---------------------------------------------


def test_the_stand_is_wired_as_described():
    assert MOTOR_BUS.voltage_v == 48.0
    assert MOTOR_BUS.current_limit_a == 25.0
    assert MOTOR_BUS.sink_current_limit_a == -12.75
    assert MOTOR_BUS.protection_mode == "LOWZ"
    assert MOTOR_BUS.priority_mode == "VOLT"
    assert BRAKE_BUS.output == 1 and BRAKE_BUS.voltage_v == 24.0 and BRAKE_BUS.current_limit_a == 5.0


def test_the_motor_bus_is_not_a_cpx_rail():
    """The bus moved to the N6974A, whose rating and envelope have nothing to do
    with the CPX's. Typing it as a Rail would carry 420 W arithmetic that is
    false about this instrument, and an output number it does not have."""
    assert not isinstance(MOTOR_BUS, Rail)
    assert not hasattr(MOTOR_BUS, "output")


def test_no_two_rails_name_the_same_output():
    """Two rails on one output would silently mean one of them is never
    configured, while every read still answered."""
    assert len({rail.output for rail in RAILS}) == len(RAILS)


def test_start_switches_off_every_cpx_output_including_the_unused_one():
    """Output 2 is disconnected, but a supply adopts the output state it is
    started into - so an energized output with open terminals still gets
    dropped."""
    switched = []

    class RecordingSupply:
        def enable_output(self, output, enabled):
            switched.append((output, enabled))
        def set_voltage(self, output, volts): pass
        def set_current(self, output, amps): pass

    testbed = ZdriveTestbed()
    testbed._supply = RecordingSupply()
    testbed.check_rails = lambda: None
    testbed._configure_rails()

    assert [output for output, _ in switched] == list(CPX400DP_OUTPUTS), (
        "every output the instrument has must be switched off, not just the ones RAILS uses"
    )
    assert all(enabled is False for _, enabled in switched), "_configure_rails energized something"


def test_every_rail_is_within_the_instrument_s_absolute_ratings():
    for rail in RAILS:
        assert 0 < rail.voltage_v <= MAX_VOLTAGE_V, f"{rail.name} exceeds the supply's voltage range"
        assert 0 < rail.current_limit_a <= MAX_CURRENT_A, f"{rail.name} exceeds the supply's current range"


# --- the power envelope -----------------------------------------------------


@pytest.mark.parametrize(
    "voltage, expected",
    [
        (24.0, 17.5),  # 420 / 24
        (48.0, 8.75),  # 420 / 48 - the motor bus
        (60.0, 7.0),   # the manual's own 60 V / 7 A envelope point
        (42.0, 10.0),  # and its 42 V / 10 A point
        (20.0, MAX_CURRENT_A),  # below ~21 V the 20 A ceiling binds, not the power envelope
        (0.0, MAX_CURRENT_A),   # no division by zero
    ],
)
def test_deliverable_current_reproduces_the_published_envelope(voltage, expected):
    assert deliverable_current_a(voltage) == pytest.approx(expected)
    assert POWER_ENVELOPE_W == 420.0


def test_the_brake_rail_gets_real_current_limiting():
    assert BRAKE_BUS.is_within_envelope
    assert BRAKE_BUS.power_w == pytest.approx(120.0)


def test_the_cpx_driver_ceiling_is_now_the_brake_rail_s_own():
    """The ceiling start() passes the CPX driver is the maximum across the rails
    it still owns. With the motor bus gone to the N6974A that is the brake alone,
    so a per-backend ceiling finally sits at what the one remaining rail
    actually runs at rather than at the motor bus's 48 V."""
    assert max(rail.voltage_v for rail in RAILS) == BRAKE_BUS.voltage_v == 24.0
    assert max(rail.current_limit_a for rail in RAILS) == BRAKE_BUS.current_limit_a == 5.0


# --- the two-quadrant bus ---------------------------------------------------


def test_the_supply_absorbs_more_than_the_odrive_will_return():
    """The matched pair, and the direction of the inequality is the whole point.
    If the supply's willingness to absorb were the binding constraint, the bus
    would rise on a hard stop and the external clamp - which reports to nothing -
    would take over."""
    assert ODRIVE_MAX_REGEN_CURRENT_A < abs(MOTOR_BUS.sink_current_limit_a), (
        "the ODrive may push more regen than the supply will take"
    )


def test_the_sink_limit_is_what_one_dissipator_buys():
    """One N7909A on a 2 kW model sinks 50% of rating. Programming any less would
    leave capability unused; programming more is refused by the instrument, whose
    floor reflects only what it recognised at power-on."""
    assert N6974A_DISSIPATORS == 1
    assert MOTOR_BUS.sink_current_limit_a == pytest.approx(
        -SINK_FRACTION_BY_DISSIPATORS[N6974A_DISSIPATORS] * 25.5
    )


def test_the_sink_load_is_inside_the_dissipator_s_capacity():
    """Each N7909A dissipates 1 kW. Sinking 12.75 A at 48 V is 612 W, so the
    declared count can actually absorb what the bus is programmed to take."""
    assert MOTOR_BUS.sink_power_w == pytest.approx(612.0)
    assert MOTOR_BUS.sink_power_w < 1000.0 * N6974A_DISSIPATORS


def test_the_bus_setpoint_is_below_the_odrive_s_overvoltage_trip():
    """The ODrive trips itself at 55 V, and with the sense leads open the
    instrument regulates about 1% above the programmed value. Both have to leave
    the bus under that trip."""
    local_sense_high_v = MOTOR_BUS.voltage_v * 1.01
    assert local_sense_high_v < 55.0


def test_the_odrive_draws_less_than_the_supply_will_source():
    """The source-side mirror of the regen pair, and the direction of the
    inequality matters for the same reason. If the supply's current limit were
    the binding constraint it would leave voltage priority, and a constant-power
    load past a current limit collapses the bus rather than sagging it - which
    reaches the ODrive as DC_BUS_UNDER_VOLTAGE and drops whatever it was
    holding."""
    assert ODRIVE_BUS_SOFT_MAX_A < MOTOR_BUS.current_limit_a, (
        "the ODrive may draw more than the supply will source"
    )
    assert ODRIVE_BUS_HARD_MAX_A < MOTOR_BUS.current_limit_a, (
        "the drive's own trip sits above the supply's limit, so the bus collapses first"
    )


def test_the_bus_limits_are_ordered():
    """Soft backs off torque, hard disarms. A soft limit at or above the hard one
    would mean the controller is allowed to command its way straight into a trip
    instead of slowing down - the same ordering the motor limits keep."""
    assert ODRIVE_BUS_SOFT_MAX_A < ODRIVE_BUS_HARD_MAX_A


def test_the_motor_limits_are_ordered():
    """Soft is what the controller may command, hard is where firmware trips. A
    soft limit at or above the hard one would mean the controller is allowed to
    command its way straight into a fault."""
    assert ODRIVE_MOTOR_SOFT_MAX_A < ODRIVE_MOTOR_HARD_MAX_A
    # Both inside the inverter ceiling this hardware reports (100 A soft /
    # 150 A hard), so the motor limits are what the stand asks for rather than
    # what the board can deliver.
    assert ODRIVE_MOTOR_SOFT_MAX_A <= 100.0
    assert ODRIVE_MOTOR_HARD_MAX_A <= 150.0


# --- the testbed ------------------------------------------------------------


def test_declared_devices_are_devices_the_engine_records():
    """A test's declared device set is validated against these keys before it
    starts, so a device named here that the engine doesn't subscribe to would
    fail the run rather than this."""
    assert ZdriveTestbed.DEVICES == (DEVICE_ODRIVE, DEVICE_CPX400DP, DEVICE_N6974A, DEVICE_TC_DAQ)
    for device in ZdriveTestbed.DEVICES:
        assert device in TELEMETRY_ENDPOINTS


def test_the_testbed_does_not_own_a_dut():
    """zdrive has no DUT façade, as with ydrive - the ODrive is the whole
    hardware interface. If a façade is ever added, its devices are its own to
    declare and the base test case unions them; this testbed must not grow to
    know about it."""
    assert DEVICE_CPX400DP in ZdriveTestbed.DEVICES
    assert "dut" not in ZdriveTestbed.DEVICES


def test_accessors_raise_before_start_rather_than_returning_none():
    testbed = ZdriveTestbed()
    for name in ("command", "telemetry", "sync_telemetry", "supply", "supply_telemetry",
                 "bus", "bus_telemetry"):
        with pytest.raises(RuntimeError, match="before start"):
            getattr(testbed, name)


def _testbed_reading(channels):
    """A ZdriveTestbed whose next ODrive frame is `channels`. Neither position
    accessor touches anything start() sets up, so no drivers are needed."""
    testbed = ZdriveTestbed()
    testbed.get_channels = lambda: channels
    return testbed


def _frame(position):
    return {
        "pos_estimate": position,
        "vel_estimate": 0.0,
        "axis_is_armed": True,
        "posvelmapper_status": 9,
        "commutmapper_status": 0,
    }


def test_a_finite_position_reads_straight_through():
    testbed = _testbed_reading(_frame(-12.5))
    assert testbed.get_pos_estimate() == -12.5
    assert testbed.get_motion().position == -12.5


@pytest.mark.parametrize("unusable", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_position_is_rejected_at_the_read(unusable):
    """A board whose pos_vel_mapper has no valid offset publishes pos_estimate as
    NaN with every other channel healthy. Unrejected, every comparison against it
    is False - a move never judges itself arrived and times out at full length,
    and a NaN taken as the origin propagates into every target derived from it.
    On this axis a target is a distance off the ground."""
    testbed = _testbed_reading(_frame(unusable))
    with pytest.raises(RuntimeError, match="not a position"):
        testbed.get_pos_estimate()


def test_get_motion_is_rejected_too_so_a_move_cannot_loop_on_it():
    """move_to() reads through get_motion(), so guarding only get_pos_estimate()
    would leave the move loop comparing against a NaN for its whole timeout."""
    testbed = _testbed_reading(_frame(float("nan")))
    with pytest.raises(RuntimeError, match="not a position"):
        testbed.get_motion()


NOMINAL, MISSING_INPUT, RELATIVE_MODE = 0, 8, 9
"""ComponentStatus values the mapper channels carry."""


def _rejection_message(commut, posvel):
    frame = _frame(float("nan"))
    frame["commutmapper_status"], frame["posvelmapper_status"] = commut, posvel
    with pytest.raises(RuntimeError) as caught:
        _testbed_reading(frame).get_pos_estimate()
    return str(caught.value)


def test_the_rejection_distinguishes_a_dead_encoder_from_an_uncalibrated_one():
    """Both read NaN with the board otherwise healthy, and they need opposite
    actions - recalibrating against a dead encoder can even appear to succeed. The
    mapper statuses are what tell them apart, so the message has to carry them."""
    dead = _rejection_message(MISSING_INPUT, MISSING_INPUT)
    assert "MISSING_INPUT" in dead        # decoded from the frame, not hardcoded
    assert "magnet" in dead               # points at the sensor, not at calibration

    uncalibrated = _rejection_message(NOMINAL, RELATIVE_MODE)
    assert "RELATIVE_MODE" in uncalibrated
    assert "offset_valid" in uncalibrated  # points at calibration


def test_a_non_numeric_position_is_rejected_like_a_nan():
    """The guard exists for pathological values, so it must not itself raise
    TypeError on one and lose the diagnosis."""
    frame = _frame(float("nan"))
    frame["pos_estimate"] = None
    with pytest.raises(RuntimeError, match="not a position"):
        _testbed_reading(frame).get_pos_estimate()


def test_the_position_guard_cannot_block_the_shutdown():
    """stop() is what engages the brake and drops the 48 V bus. If it read a
    position, an unusable one would raise mid-sequence and strand a live bus."""
    assert "pos_estimate" not in inspect.getsource(ZdriveTestbed.stop)
    assert "get_motion" not in inspect.getsource(ZdriveTestbed.stop)


def test_teardown_drops_the_brake_rail_before_the_motor_bus():
    """The brake is magnet-applied, so dropping its rail first is what makes it
    grab and hold the load before the drive is disarmed and the bus removed.
    Reversing these would leave the load unheld during shutdown - a source
    ordering that is easy to disturb while editing and invisible in review."""
    source = inspect.getsource(ZdriveTestbed.stop)
    brake = source.index("engage the brake")
    disarm = source.index("disarm the ODrive axis")
    motor_bus = source.index("drop the 48 V motor bus")
    assert brake < disarm < motor_bus, "teardown must engage the brake, then disarm, then drop the bus"


def test_teardown_continues_after_a_failing_step():
    """stop() is a power sequence: one unresponsive client must not leave a 48 V
    bus energized."""
    calls = []

    def boom():
        calls.append("boom")
        raise RuntimeError("client is wedged")

    ZdriveTestbed._safe("a step that fails", boom)
    ZdriveTestbed._safe("a step that follows it", lambda: calls.append("ran anyway"))
    assert calls == ["boom", "ran anyway"]


# --- driver log wiring ------------------------------------------------------


def test_each_driver_is_told_where_to_write_its_log():
    """The driver knows nothing about runs; the testbed that starts it is the
    only participant holding both the run's test_id and the engine's output
    dir, so it composes the path and passes it in."""
    from pathlib import Path

    from protocol.paths import DRIVER_LOG_FILENAME

    testbed = ZdriveTestbed(output_dir=Path("/out"), test_id="run-42")
    for device in ZdriveTestbed.DEVICES:
        flag, path = testbed._log_args(device)
        assert flag == "--log-file"
        assert path == f"/out/runs/run-42/{device}/{DRIVER_LOG_FILENAME}"


def test_a_driver_log_lands_beside_that_device_s_telemetry():
    """Same directory, which is the whole point: a decoded fault is readable
    against the frames it happened during."""
    from pathlib import Path

    from protocol.paths import driver_log_path, run_telemetry_path

    log = driver_log_path(Path("/out"), "run-42", DEVICE_ODRIVE)
    telemetry = run_telemetry_path(Path("/out"), "run-42", DEVICE_ODRIVE)
    assert log.parent == telemetry.parent


def test_without_a_run_the_drivers_are_given_no_log_flag():
    """A testbed used outside a run - a demo, a bring-up script - must still
    start, logging to the console only."""
    assert ZdriveTestbed()._log_args(DEVICE_ODRIVE) == []
    assert ZdriveTestbed(output_dir=None, test_id="run-42")._log_args(DEVICE_ODRIVE) == []
    assert ZdriveTestbed(output_dir="/out", test_id=None)._log_args(DEVICE_ODRIVE) == []




# --- the rulebook -----------------------------------------------------------


def test_every_bounded_channel_is_one_something_actually_publishes():
    """A bound on a channel nothing publishes is never evaluated and never
    complains - it sits there reporting a clean pass on every frame. That is worse
    than no bound at all, so the declared surfaces are the check.

    RUN STATE COUNTS, not just device telemetry: the runner merges published state
    into what it evaluates, which is how stopping_distance_bound works at all. The
    check is that the channel appears in DEFAULT_STATE, because a state channel
    that is not seeded there is dropped from the recorded file by the engine's
    header sampling - and a bound on a dropped channel is exactly the silent pass
    this test exists to prevent."""
    published = (set(N6974A_TELEMETRY_CHANNELS) | set(ODRIVE_TELEMETRY_CHANNELS)
                 | set(TC_DAQ_TELEMETRY_CHANNELS) | set(DEFAULT_STATE))
    for bound in ZDRIVE_RULEBOOK.bounds:
        assert bound.channel in published, (
            f"{bound.label} bounds {bound.channel!r}, which neither a zdrive device "
            "nor the run's own state publishes"
        )


def test_the_rulebook_spans_all_three_devices():
    """Which is why a runner has to be started against all three streams. If
    every bound came from one device this would be a needless complication; it
    does not, and a runner given fewer silently evaluates only part."""
    bounded = {b.channel for b in ZDRIVE_RULEBOOK.bounds}
    assert bounded & set(N6974A_TELEMETRY_CHANNELS), "no bound reads the supply"
    assert bounded & set(ODRIVE_TELEMETRY_CHANNELS), "no bound reads the ODrive"
    assert bounded & set(TC_DAQ_TELEMETRY_CHANNELS), "no bound reads the thermocouple DAQ"


def test_only_the_wired_thermocouples_are_bounded():
    """The DAQ streams eight channels and publishes None for one it cannot read.
    A numeric bound on a None is unevaluable, and the runner stops a run it
    cannot evaluate - so bounding an unconnected channel aborts every run on its
    first frame. Only channels 1 and 2 are wired on this stand."""
    from testcases.zdrive.rulebooks.zdrive_rulebook import LIVE_TC_CHANNELS
    assert LIVE_TC_CHANNELS == (1, 2)
    bounded = {b.channel for b in ZDRIVE_RULEBOOK.bounds if b.channel.startswith("temperature_")}
    assert bounded == {"temperature_1_c", "temperature_2_c"}


def test_the_thermal_bounds_are_fatal_at_70c_and_tolerate_a_dropped_sample():
    """A thermocouple spikes from electrical noise as well as heat, and this DAQ
    drops the odd sample - so a violation is debounced and a momentary FAULT is
    given a separate, longer window before it stops the run."""
    thermal = [b for b in ZDRIVE_RULEBOOK.bounds if b.channel.startswith("temperature_")]
    assert thermal
    for bound in thermal:
        assert bound.upper == 70.0
        assert bound.lower is None      # cold is not a fault on this stand
        assert bound.fatal
        assert bound.persistence_s == 5.0
        assert bound.unevaluable_grace_s > bound.persistence_s


def test_the_bus_bounds_agree_with_what_the_testbed_programs():
    """The rulebook keeps its own numbers, so this is what stops them drifting
    apart from the testbed's."""
    assert MIN_BUS_CURRENT_A == MOTOR_BUS.sink_current_limit_a, (
        "the regen bound and the programmed sink limit disagree"
    )
    assert MAX_MOTOR_CURRENT_A == ODRIVE_MOTOR_HARD_MAX_A, (
        "the motor bound and the ODrive's programmed hard limit disagree"
    )


def test_the_overspeed_bound_sits_above_a_stroke_and_below_a_fall():
    """The one bound that catches a dropped load, so it has to clear the top of a
    normal stroke without reaching the speed a released load runs away at.
    Measured peaks over a 242-cycle run reach 23.8 turns/s; a released 1000 lb
    load on this axis reached 92.9."""
    assert MAX_AXIS_SPEED_TURNS_S > 23.8, "would fire at the top of a normal stroke"
    assert MAX_AXIS_SPEED_TURNS_S < 92.9, "a released load would not reach it"


def test_the_overspeed_bound_is_the_only_thing_a_fall_trips():
    """A disarmed axis on a loaded stand stops drawing current before it starts
    moving, so every electrical bound reads healthy through the whole fall. This
    is what stops that reading as a pass."""
    fall = {"vel_estimate": 92.9, "board_vbus_voltage": 48.0, "current": 0.0,
            "motor_foc_iq_measured": 0.0, "voltage": 48.0}
    violated = [
        bound.label
        for bound in ZDRIVE_RULEBOOK.bounds
        if bound.channel in fall
        and (
            (bound.upper is not None and fall[bound.channel] > bound.upper)
            or (bound.lower is not None and fall[bound.channel] < bound.lower)
        )
    ]
    assert violated == ["overspeed_bound"]


def test_the_overvoltage_bound_sits_above_operation_and_below_the_odrive_trip():
    """Too low and every run dies on the ~1% local-sense offset; too high and the
    ODrive acts on its own behalf first, which is a disarmed axis rather than an
    ended run."""
    assert MAX_BUS_VOLTAGE_V > MOTOR_BUS.voltage_v * 1.01, "would fire during normal operation"
    assert MAX_BUS_VOLTAGE_V < 55.0, "the ODrive's own trip would act first"


def test_the_signed_bounds_are_bounded_in_both_directions():
    """Braking is what this stand is for, and braking is the negative direction
    of both channels."""
    two_sided = {"bus_current_bound", "motor_current_bound"}
    for bound in ZDRIVE_RULEBOOK.bounds:
        if bound.label in two_sided:
            assert bound.upper is not None and bound.lower is not None, (
                f"{bound.label} must bound both directions"
            )
            assert bound.lower < 0 < bound.upper


def test_every_bound_is_fatal():
    """Each of these is a safety net rather than a datum: none of them is worth
    recording and continuing past."""
    for bound in ZDRIVE_RULEBOOK.bounds:
        assert bound.fatal, f"{bound.label} is not fatal"


def test_teardown_disconnects_the_motor_bus_backend_too():
    """A supply whose backend is left connected holds its socket, and this
    instrument allows only six connections in total."""
    source = inspect.getsource(ZdriveTestbed.stop)
    assert "disconnect the motor bus backend" in source


# --- the setpoint check -----------------------------------------------------


def _testbed_with_channels(bus_channels, supply_channels=None):
    """A testbed whose telemetry reads are stubbed, so check_rails() can be
    exercised without the hardware."""
    if supply_channels is None:
        supply_channels = {}
        for rail in RAILS:
            supply_channels[f"setpoint_voltage_{rail.output}"] = rail.voltage_v
            supply_channels[f"setpoint_current_{rail.output}"] = rail.current_limit_a
    testbed = ZdriveTestbed()
    testbed.get_bus_channels = lambda: bus_channels
    testbed.get_supply_channels = lambda: supply_channels
    # A stubbed frame never changes, so re-reading it cannot help and would only
    # spend SETPOINT_SETTLE_DELAY_S per attempt.
    testbed.SETPOINT_SETTLE_ATTEMPTS = 1
    return testbed


def _good_bus_channels(**overrides):
    channels = {
        "setpoint_voltage": MOTOR_BUS.voltage_v,
        "current_limit": MOTOR_BUS.current_limit_a,
        "current_limit_negative": MOTOR_BUS.sink_current_limit_a,
        "protection_mode": MOTOR_BUS.protection_mode,
        "priority_mode": MOTOR_BUS.priority_mode,
    }
    channels.update(overrides)
    return channels


def test_check_rails_passes_when_the_stand_holds_its_configuration():
    _testbed_with_channels(_good_bus_channels()).check_rails()


def test_check_rails_catches_a_bus_voltage_that_is_not_what_was_configured():
    """The one that matters most: the N6974A's driver clamps at its own 80 V
    rating, so a wrong bus setpoint reaches the instrument and only this notices."""
    testbed = _testbed_with_channels(_good_bus_channels(setpoint_voltage=80.0))
    with pytest.raises(RuntimeError, match="bus voltage"):
        testbed.check_rails()


def test_check_rails_catches_a_sink_limit_left_at_the_instrument_s_default():
    """-2.55 A is what an N6974A holds when nobody programs the negative limit:
    10% of rating, regardless of the dissipator it recognised. A stand running
    that while believing it sinks 12.75 A would find the bus rising on hard
    stops."""
    testbed = _testbed_with_channels(_good_bus_channels(current_limit_negative=-2.55))
    with pytest.raises(RuntimeError, match="bus sink limit"):
        testbed.check_rails()


def test_check_rails_catches_a_shutdown_mode_that_would_leave_the_bus_charged():
    testbed = _testbed_with_channels(_good_bus_channels(protection_mode="HIGHZ"))
    with pytest.raises(RuntimeError, match="shutdown mode"):
        testbed.check_rails()


def test_check_rails_still_catches_a_wrong_brake_rail():
    wrong = {f"setpoint_voltage_{BRAKE_BUS.output}": 48.0,
             f"setpoint_current_{BRAKE_BUS.output}": BRAKE_BUS.current_limit_a}
    testbed = _testbed_with_channels(_good_bus_channels(), supply_channels=wrong)
    with pytest.raises(RuntimeError, match="zdrive brake voltage"):
        testbed.check_rails()


def test_check_rails_reports_every_wrong_setpoint_at_once():
    """A stand with two things wrong should say so in one message rather than
    being fixed one restart at a time."""
    testbed = _testbed_with_channels(
        _good_bus_channels(setpoint_voltage=80.0, protection_mode="HIGHZ")
    )
    with pytest.raises(RuntimeError) as caught:
        testbed.check_rails()
    assert "bus voltage" in str(caught.value)
    assert "shutdown mode" in str(caught.value)


def test_check_rails_catches_the_wrong_priority_mode():
    """priority_mode decides which pair of settings regulates. In CURR the bus
    voltage setpoint is not what is being held, so every other check above is
    reading numbers that are not in control."""
    testbed = _testbed_with_channels(_good_bus_channels(priority_mode="CURR"))
    with pytest.raises(RuntimeError, match="priority mode"):
        testbed.check_rails()


def test_check_rails_catches_a_narrowed_source_limit():
    """A positive limit below the rating would make the rulebook's 25 A
    bus_current_bound unfireable - the trap ydrive's overcurrent_bound fell
    into."""
    testbed = _testbed_with_channels(_good_bus_channels(current_limit=8.0))
    with pytest.raises(RuntimeError, match="bus current limit"):
        testbed.check_rails()


def test_the_source_limit_is_not_below_the_bound_that_watches_it():
    """The pair has to stay ordered: the supply must be willing to deliver at
    least what the bound fires at, or the bound can never fire."""
    assert MOTOR_BUS.current_limit_a >= MAX_BUS_CURRENT_A


def test_the_stand_declares_no_voltage_ceiling_at_the_instrument():
    """VOLTage:LIMit is the ceiling for CURRENT priority; setting it in voltage
    priority is refused with +315 "must be in current priority mode". So the bus
    voltage ceiling lives in zdrive_rulebook, not on the instrument, and MotorBus
    must not carry a field that cannot be written."""
    assert not hasattr(MOTOR_BUS, "voltage_limit_v")
    assert MAX_BUS_VOLTAGE_V > MOTOR_BUS.voltage_v


def test_the_bus_is_configured_off_and_in_voltage_priority_before_anything_else():
    """The order is load-bearing twice over. The output must be off before the
    priority mode is written, because the driver refuses that switch on a live
    output; and the priority mode must be written before the setpoints, because
    switching it reverts every output setting to its reset value - so the same
    write afterwards would silently undo the four that follow."""
    calls = []

    class RecordingBus:
        def enable_output(self, enabled):
            calls.append(("enable_output", enabled))
        def set_priority_mode(self, mode):
            calls.append(("set_priority_mode", mode))
        def set_voltage(self, volts):
            calls.append(("set_voltage", volts))
        def set_current_limit(self, amps):
            calls.append(("set_current_limit", amps))
        def set_current_limit_negative(self, amps):
            calls.append(("set_current_limit_negative", amps))
        def set_protection_mode(self, mode):
            calls.append(("set_protection_mode", mode))

    testbed = ZdriveTestbed()
    testbed._bus = RecordingBus()
    testbed._configure_bus()

    names = [name for name, _ in calls]
    assert names[0] == "enable_output" and calls[0][1] is False, "the bus was not switched off first"
    assert names[1] == "set_priority_mode", "the priority mode must be written before the setpoints"
    for setting in ("set_voltage", "set_current_limit",
                    "set_current_limit_negative", "set_protection_mode"):
        assert names.index(setting) > names.index("set_priority_mode"), (
            f"{setting} lands before the priority-mode write, which would reset it"
        )


def test_the_bus_is_configured_with_the_values_the_stand_declares():
    """Every write goes through MOTOR_BUS rather than a literal, so the
    declaration is the one place a number changes."""
    written = {}

    class RecordingBus:
        def enable_output(self, enabled): pass
        def set_priority_mode(self, mode): written["priority_mode"] = mode
        def set_voltage(self, volts): written["voltage"] = volts
        def set_current_limit(self, amps): written["current_limit"] = amps
        def set_current_limit_negative(self, amps): written["sink"] = amps
        def set_protection_mode(self, mode): written["protection_mode"] = mode

    testbed = ZdriveTestbed()
    testbed._bus = RecordingBus()
    testbed._configure_bus()

    assert written == {
        "priority_mode": MOTOR_BUS.priority_mode,
        "voltage": MOTOR_BUS.voltage_v,
        "current_limit": MOTOR_BUS.current_limit_a,
        "sink": MOTOR_BUS.sink_current_limit_a,
        "protection_mode": MOTOR_BUS.protection_mode,
    }


def test_configuring_the_bus_never_energizes_it():
    """start() leaves the whole stand de-energized; powering the bus is a test's
    decision, taken in PreTestSetup."""
    enables = []

    class RecordingBus:
        def enable_output(self, enabled): enables.append(enabled)
        def set_priority_mode(self, mode): pass
        def set_voltage(self, volts): pass
        def set_current_limit(self, amps): pass
        def set_current_limit_negative(self, amps): pass
        def set_protection_mode(self, mode): pass

    testbed = ZdriveTestbed()
    testbed._bus = RecordingBus()
    testbed._configure_bus()
    assert enables == [False]


def test_check_rails_re_reads_before_calling_a_setpoint_wrong():
    """A telemetry frame can be older than the write it is being asked about: the
    CPX400DP driver holds setpoints in a cached tier and latest_frame() answers
    with the newest frame already queued, not one published after the write. The
    first read legitimately carries the previous run's values, which is invisible
    whenever those happen to match and a spurious failure whenever they do not."""
    stale = {f"setpoint_voltage_{BRAKE_BUS.output}": 11.29,
             f"setpoint_current_{BRAKE_BUS.output}": 2.309}
    fresh = {f"setpoint_voltage_{BRAKE_BUS.output}": BRAKE_BUS.voltage_v,
             f"setpoint_current_{BRAKE_BUS.output}": BRAKE_BUS.current_limit_a}
    reads = [stale, stale, fresh]

    testbed = ZdriveTestbed()
    testbed.get_bus_channels = lambda: _good_bus_channels()
    testbed.get_supply_channels = lambda: reads.pop(0)
    testbed.SETPOINT_SETTLE_DELAY_S = 0
    testbed.check_rails()
    assert reads == [], "check_rails gave up before the fresh frame arrived"


def test_check_rails_still_fails_when_a_setpoint_never_settles():
    """Retrying must not turn a genuinely wrong setpoint into a pass."""
    wrong = {f"setpoint_voltage_{BRAKE_BUS.output}": 48.0,
             f"setpoint_current_{BRAKE_BUS.output}": BRAKE_BUS.current_limit_a}
    testbed = ZdriveTestbed()
    testbed.get_bus_channels = lambda: _good_bus_channels()
    testbed.get_supply_channels = lambda: wrong
    testbed.SETPOINT_SETTLE_DELAY_S = 0
    with pytest.raises(RuntimeError, match="zdrive brake voltage"):
        testbed.check_rails()
