"""The xdeploy stand's shape and its rulebook, plus the parts of XdeployTestbed
that can be checked without the hardware attached.

start()/stop() launch driver subprocesses and talk to a real ODrive and a real
thermocouple DAQ, so they are not exercised here. What is: that the stand
declares the two devices it owns and touches nothing else, that it writes no
persistent configuration to a board nobody has measured, that its teardown is
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
from testbeds.xdeploy_testbed.xdeploy_testbed import XdeployTestbed
from testcases.xdeploy.channels import DEFAULT_STATE
from testcases.xdeploy.rulebooks.xdeploy_rulebook import (
    LIVE_TC_CHANNELS,
    MAX_TEMPERATURE_C,
    MIN_BUS_VOLTAGE_V,
    XDEPLOY_RULEBOOK,
)
from testcases.xdeploy.testcases.base_xdeploy_test import BaseXdeployTest
from testcases.xdeploy.testcases.testcases import ManualTest


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


def test_start_writes_no_persistent_configuration_to_the_board():
    """The ODrive's current, bus and trip limits are persistent device state and
    this stand has not been measured, so start() deliberately writes none of
    them. A borrowed number here would be inherited by every later run and would
    read as measured. Source-checked because the write would be one line, and
    invisible in review."""
    source = inspect.getsource(XdeployTestbed.start)
    assert "set_motor_config" not in source
    assert "set_axis_config" not in source
    assert "set_board_config" not in source


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
