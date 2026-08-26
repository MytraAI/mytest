"""The parts of YdriveTestbed that can be checked without the hardware attached.

start()/stop() launch driver subprocesses and talk to a real ODrive, supply and
DAQ, so they are not exercised here. What is: the setpoint check, which decides
whether a run is allowed to proceed and which fails in a way a live run makes
look like a hardware fault rather than a timing one.
"""
from __future__ import annotations

import pytest

from testbeds.ydrive_testbed.ydrive_testbed import BRAKE_BUS, MOTOR_BUS, RAILS, YdriveTestbed


def _configured_channels() -> dict:
    channels = {}
    for rail in RAILS:
        channels[f"setpoint_voltage_{rail.output}"] = rail.voltage_v
        channels[f"setpoint_current_{rail.output}"] = rail.current_limit_a
    return channels


def test_check_rails_passes_when_the_supply_holds_its_configuration():
    testbed = YdriveTestbed()
    testbed._supply_channels = _configured_channels
    testbed.check_rails()


def test_check_rails_re_reads_before_calling_a_setpoint_wrong():
    """A telemetry frame can be older than the write it is being asked about: the
    driver holds setpoints in a cached tier and latest_frame() answers with the
    newest frame already queued, not one published after the write.

    So the first read legitimately carries the previous run's values. That is
    invisible whenever those happen to match what this stand configures, and a
    spurious failure whenever they do not - which reads as a refused write rather
    than as a stale frame."""
    stale = dict(_configured_channels())
    stale[f"setpoint_voltage_{BRAKE_BUS.output}"] = 11.29
    reads = [stale, stale, _configured_channels()]

    testbed = YdriveTestbed()
    testbed._supply_channels = lambda: reads.pop(0)
    testbed.SETPOINT_SETTLE_DELAY_S = 0
    testbed.check_rails()
    assert reads == [], "check_rails gave up before the fresh frame arrived"


def test_check_rails_still_fails_when_a_setpoint_never_settles():
    """Retrying must not turn a genuinely wrong setpoint into a pass."""
    wrong = dict(_configured_channels())
    wrong[f"setpoint_voltage_{BRAKE_BUS.output}"] = MOTOR_BUS.voltage_v

    testbed = YdriveTestbed()
    testbed._supply_channels = lambda: wrong
    testbed.SETPOINT_SETTLE_DELAY_S = 0
    with pytest.raises(RuntimeError, match=f"{BRAKE_BUS.name} voltage"):
        testbed.check_rails()


def test_check_rails_reports_every_wrong_setpoint_at_once():
    """A stand with two things wrong should say so in one message rather than
    being fixed one restart at a time."""
    wrong = dict(_configured_channels())
    wrong[f"setpoint_voltage_{BRAKE_BUS.output}"] = 48.0
    wrong[f"setpoint_current_{MOTOR_BUS.output}"] = 1.0

    testbed = YdriveTestbed()
    testbed._supply_channels = lambda: wrong
    testbed.SETPOINT_SETTLE_DELAY_S = 0
    with pytest.raises(RuntimeError) as caught:
        testbed.check_rails()
    assert f"{BRAKE_BUS.name} voltage" in str(caught.value)
    assert f"{MOTOR_BUS.name} current limit" in str(caught.value)


def test_the_vision_command_client_outlives_a_camera_scan():
    """select_best_camera opens every camera on the machine and reads frames from
    each, which runs to tens of seconds - an absent index on Windows costs an MSMF
    attempt, a settle, a DSHOW attempt and a last-resort probe.

    Against CommandClient's 5 s default that could never complete, and overrunning
    is not a slow report: a timed-out REQ socket is left permanently broken, so the
    testbed's client has to be rebuilt and the run is over."""
    from hardware.vision_home import camera
    from testbeds.ydrive_testbed.ydrive_testbed import VISION_COMMAND_TIMEOUT_MS

    max_index = 6
    optimistic_s = max_index * (3 * 0.1 + camera.RELEASE_SETTLE_S)

    assert VISION_COMMAND_TIMEOUT_MS / 1000 > 4 * optimistic_s, (
        f"a scan of {max_index} indices costs at least {optimistic_s:.0f}s before any "
        "Windows backend-probing overhead, and this deadline leaves no room for it"
    )
