"""The zdrive stand's rail configuration, and the parts of ZdriveTestbed that
can be checked without the hardware attached.

start()/stop() launch driver subprocesses and talk to a real ODrive and a real
supply, so they are not exercised here. What is: the rail declarations, the
envelope arithmetic that decides whether a configured current limit is even
reachable, the teardown ordering that the brake's magnet-applied behaviour
depends on, and the setpoint check - all of which are ordinary logic and all of
which would be silently wrong in ways a live run might not reveal.
"""
from __future__ import annotations

import inspect

import pytest

from protocol.wire import DEVICE_CPX400DP, DEVICE_ODRIVE, TELEMETRY_ENDPOINTS
from hardware.cpx400dp.rails import (
    MAX_CURRENT_A,
    MAX_VOLTAGE_V,
    POWER_ENVELOPE_W,
    deliverable_current_a,
)
from testbeds.zdrive_testbed.zdrive_testbed import BRAKE_BUS, MOTOR_BUS, RAILS, ZdriveTestbed


# --- what the stand is wired as ---------------------------------------------


def test_rails_match_the_stand_as_described():
    assert MOTOR_BUS.output == 2 and MOTOR_BUS.voltage_v == 48.0 and MOTOR_BUS.current_limit_a == 16.0
    assert BRAKE_BUS.output == 1 and BRAKE_BUS.voltage_v == 24.0 and BRAKE_BUS.current_limit_a == 5.0


def test_the_two_rails_are_on_different_outputs():
    """Both rails naming one output would silently mean one of them is never
    configured, while every read still answered."""
    assert len({rail.output for rail in RAILS}) == len(RAILS)
    assert {rail.output for rail in RAILS} == {1, 2}


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


def test_the_motor_rail_s_limit_is_knowingly_unreachable():
    """Not an accident, and not a bug to be quietly corrected: the 16 A limit
    was specified deliberately. What matters is that the code knows it is
    unreachable, so start() can warn and a rulebook can be pointed at
    in_power_limit_2 rather than at current_2. If someone lowers the limit to
    8.5 A or below, this test is the place that should change with it."""
    assert not MOTOR_BUS.is_within_envelope
    assert MOTOR_BUS.current_limit_a > deliverable_current_a(MOTOR_BUS.voltage_v)
    assert MOTOR_BUS.power_w > POWER_ENVELOPE_W


# --- the testbed ------------------------------------------------------------


def test_declared_devices_are_devices_the_engine_records():
    """A test's declared device set is validated against these keys before it
    starts, so a device named here that the engine doesn't subscribe to would
    fail the run rather than this."""
    assert ZdriveTestbed.DEVICES == (DEVICE_ODRIVE, DEVICE_CPX400DP)
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
    for name in ("command", "telemetry", "sync_telemetry", "supply", "supply_telemetry"):
        with pytest.raises(RuntimeError, match="before start"):
            getattr(testbed, name)


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


