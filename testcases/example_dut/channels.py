"""Documents every channel example_dut's tests can see.

HARDWARE_CHANNELS/COMMAND_CHANNELS are thin re-exports of
MockDutBackend's own TELEMETRY_CHANNELS/COMMAND_CHANNELS (see
../../hardware/mock_dut/mock_channels.py) - that module is the actual
source of truth (declared right next to the backend that produces/
accepts those exact strings), re-exported here purely so this one file
still documents everything a test can see, without a second,
independently-maintained copy of the same lists. ExampleDut.start()
imports directly from mock_channels.py, not from here, and positively
confirms both against the live driver process - see its docstring.

DEFAULT_STATE: test-published state channels (via
TestCase.set_state()) that aren't present until some step
first sets them. Seeded with these defaults at test start (see
BaseExampleDutTest.pre_test_setup()), so every channel exists in the
stream from frame 1 instead of appearing incrementally as steps happen
to compute things.

Bound-status channels ({bound.label}_status, plus test_status) aren't
listed here. They're derived automatically from whatever Rulebooks a
test registers (see BaseExampleDutTest.pre_test_setup()), not
hand-enumerated - the Rulebook is already the single source of truth
for bound names.
"""
from __future__ import annotations

from typing import Any, Dict

from hardware.mock_dut.mock_channels import COMMAND_CHANNELS
from hardware.mock_dut.mock_channels import TELEMETRY_CHANNELS as HARDWARE_CHANNELS

DEFAULT_STATE: Dict[str, Any] = {
    "position_target": None,
    "current_step": None,
}
