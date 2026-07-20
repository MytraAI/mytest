"""MockPowerSupplyBackend's declared telemetry/command surface - the
source of truth for anything that wants to check this backend's actual
channels (the same role hardware/mock_dut/mock_channels.py plays for
the DUT).

Not yet wired up to any verify_channels()/verify_actions() call - no
test case watches the power supply's own telemetry today (see the
architecture doc's open decisions) - but declared here so that's a
one-line addition whenever a test case actually needs it.

Kept separate from mock_backend.py so a channel/action list can be
read (or imported elsewhere) without pulling in the backend's asyncio
implementation.
"""
from __future__ import annotations

TELEMETRY_CHANNELS = [
    "voltage",  # volts (V)
    "current",  # amps (A)
]

COMMAND_CHANNELS = [
    "set_output",  # sets voltage (V) and current (A) setpoints
    "enable_output",  # boolean on/off - no unit
]
