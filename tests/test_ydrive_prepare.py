"""prepare_for_operation: cold stand to ready-to-arm.

The ODrive latches DC_BUS_UNDER_VOLTAGE whenever its bus is down - which every
board booted on USB alone has - and a latched error is enough for it to refuse
CLOSED_LOOP_CONTROL. These run against fakes: no subprocess, no instrument.
"""
from __future__ import annotations

from typing import Optional

import pytest

from testcases.ydrive.teststeps.teststeps import (
    DEFAULT_CONTROL_MODE,
    prepare_for_operation,
)

INITIALIZING = 1
DC_BUS_UNDER_VOLTAGE = 512
CURRENT_LIMIT_VIOLATION = 4096


class FakeOdriveTestbed:
    """Records what was commanded, and models the one behaviour that matters:
    clear_errors only sticks once the bus is actually up.

    `bus_clears_after` is how many clears it takes - 0 for a stand whose bus is
    already up, higher for one whose output is still ramping, None for a fault
    that never clears."""

    def __init__(self, active_errors: int = 0, bus_clears_after: Optional[int] = 0):
        self.calls = []
        self.command = self
        self.bus_powered = False
        self.active_errors = active_errors
        self.disarm_reason = 0
        self._bus_clears_after = bus_clears_after
        self._clears = 0

    # supply side
    def power_motor_bus(self, enabled):
        self.calls.append("bus:on" if enabled else "bus:off")
        self.bus_powered = enabled

    # ODrive side
    def clear_errors(self):
        self.calls.append("clear_errors")
        self._clears += 1
        never_clears = self._bus_clears_after is None
        if never_clears or not self.bus_powered or self._clears <= self._bus_clears_after:
            # Below the board's under-voltage trip level it re-latches
            # immediately, which is what the retry loop exists to wait out.
            self.active_errors = INITIALIZING | DC_BUS_UNDER_VOLTAGE
            self.disarm_reason = 0
            return
        self.active_errors = 0
        self.disarm_reason = 0

    def set_control_mode(self, mode):
        self.calls.append(f"mode:{mode}")

    def __getattr__(self, name):
        if name.startswith("set_controller_config_"):
            return lambda value: self.calls.append(f"tune:{name}")
        raise AttributeError(name)

    def get_channels(self):
        return {
            "active_errors": self.active_errors,
            "disarm_reason": self.disarm_reason,
            "axis_procedure_result": 0,
            "axis_current_state": 1,
        }


class FakeTestCase:
    """The surface @step and these steps require of a TestCase."""

    test_id = "test-prepare"

    def __init__(self, testbed):
        self.testbed = testbed
        self.state = {}

    def set_state(self, name, value):
        self.state[name] = value

    def wait_for(self, seconds):
        self.testbed.calls.append(f"wait:{seconds}")

    def check_should_continue(self):
        pass


def test_the_bus_comes_up_before_anything_is_cleared():
    """Clearing under a dead bus achieves nothing - the board re-latches
    DC_BUS_UNDER_VOLTAGE on its next control cycle."""
    testbed = FakeOdriveTestbed(active_errors=CURRENT_LIMIT_VIOLATION)
    prepare_for_operation(FakeTestCase(testbed))

    assert testbed.calls[0] == "bus:on"
    assert testbed.calls.index("bus:on") < testbed.calls.index("clear_errors")


def test_a_latched_fault_is_cleared_and_the_stand_left_ready():
    """A CURRENT_LIMIT_VIOLATION latched by a previous run is exactly what stops
    the next one arming."""
    testbed = FakeOdriveTestbed(active_errors=CURRENT_LIMIT_VIOLATION)
    case = FakeTestCase(testbed)

    prepare_for_operation(case)

    assert testbed.active_errors == 0
    assert f"mode:{DEFAULT_CONTROL_MODE}" in testbed.calls
    assert any(c.startswith("tune:") for c in testbed.calls), "tuning was never applied"
    assert case.state["current_step"] == "prepare_for_operation", (
        "a nested @step would have overwritten current_step"
    )


def test_clearing_is_retried_while_the_bus_is_still_ramping():
    """The supply's output ramps, so the first clears legitimately fail. The
    board's own trip level decides when it is up - this step invents no voltage
    threshold of its own."""
    testbed = FakeOdriveTestbed(active_errors=DC_BUS_UNDER_VOLTAGE, bus_clears_after=2)

    prepare_for_operation(FakeTestCase(testbed))

    assert testbed.calls.count("clear_errors") == 3
    assert testbed.active_errors == 0


def test_a_fault_that_will_not_clear_stops_the_run_here():
    """With a decoded reason. An error that will not clear is one the axis will
    refuse to arm with, and failing here beats failing at the first dwell with
    the stand energized."""
    testbed = FakeOdriveTestbed(active_errors=CURRENT_LIMIT_VIOLATION, bus_clears_after=None)
    case = FakeTestCase(testbed)

    with pytest.raises(RuntimeError) as excinfo:
        prepare_for_operation(case, clear_timeout_s=0.05)

    message = str(excinfo.value)
    assert "active_errors" in message
    assert "DC_BUS_UNDER_VOLTAGE" in message, f"the reason is not decoded: {message}"


def test_the_axis_is_not_armed_and_the_brake_is_not_touched():
    """Arming and the brake are release_brake's to sequence, in the one order
    that never leaves the load held by neither."""
    testbed = FakeOdriveTestbed()
    prepare_for_operation(FakeTestCase(testbed))

    assert not any(c.startswith("axis:") for c in testbed.calls)
    assert not any(c.startswith("brake:") for c in testbed.calls)
    assert "bus:off" not in testbed.calls
