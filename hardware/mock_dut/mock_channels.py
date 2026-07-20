"""MockDutBackend's declared telemetry/command surface - the source of
truth ExampleDut.start() checks against via TelemetryClient
.verify_channels()/CommandClient.verify_actions(), and
testcases/example_dut/channels.py re-exports for test-author reference.

Kept separate from mock_backend.py so a channel/action list can be
read (or imported elsewhere) without pulling in the backend's asyncio
servo implementation.
"""
from __future__ import annotations

TELEMETRY_CHANNELS = [
    "position",  # abstract position units - no defined physical unit (mock_backend.py is not a physically rigorous model)
    "velocity",  # position units per second (mock_backend.py: position += velocity * dt)
    "current",  # arbitrary simulated units - not modeled as a real current, just a proxy for control effort
]

COMMAND_CHANNELS = [
    "set_position_input",  # commanded position setpoint, same units as the position channel
    "set_position_gain",  # dimensionless tuning gain, no physical unit
    "set_velocity_gain",  # dimensionless tuning gain, no physical unit
    "set_velocity_integrator",  # dimensionless tuning gain, no physical unit
]
