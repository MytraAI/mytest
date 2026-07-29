"""Simulated DUT backend for local development and testing.

Models a simple cascaded position/velocity servo:
- A position error feeds a velocity command, scaled by position_gain.
- The actual velocity approaches that command at a rate set by
  velocity_gain.
- Position error is separately accumulated over time into a running
  integral (self._integral); velocity_integrator is the gain applied to
  that accumulator when folding it into the velocity command, to reduce
  steady-state error. The two are easy to conflate - velocity_integrator
  is a tuning knob, self._integral is the thing it scales.

This is the same shape as a real position/velocity/current control
loop, but it's a plain first-order approximation, not a physically
rigorous model. `current` is modeled as roughly proportional to
control effort plus a small friction-like term, so it responds
sensibly to the gains without claiming to be an accurate motor model.

Telemetry streams continuously once connected, with no acquisition
start/stop concept (same as the power supply): a DUT's
position/velocity/current are always meaningful once it's powered and
connected, not something that gets "acquired" on and off.

See mock_channels.py for this backend's declared telemetry/command
surface (TELEMETRY_CHANNELS/COMMAND_CHANNELS) - the source of truth
list_actions() below answers from, and what ExampleDut.start() checks
against.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, AsyncIterator, List

from protocol.wire import DEVICE_DUT

from ..backend import HardwareBackend, HardwareError
from .mock_channels import COMMAND_CHANNELS

SAMPLE_INTERVAL_S = 0.02  # 50 Hz - tune to taste for local testing


class MockDutBackend(HardwareBackend):
    """Simulated DUT - a first-order position/velocity/current servo approximation, no real hardware needed."""

    device = DEVICE_DUT
    sample_interval_s = SAMPLE_INTERVAL_S

    def __init__(self) -> None:
        self._connected = False
        self._position_input = 0.0
        self._position_gain = 0.0
        self._velocity_gain = 0.0
        self._velocity_integrator = 0.0
        self._position = 0.0
        self._velocity = 0.0
        self._integral = 0.0

    async def connect(self) -> None:
        await asyncio.sleep(0.05)  # pretend there's a handshake
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def get_status(self) -> dict:
        return {
            "connected": self._connected,
            "position_input": self._position_input,
            "position_gain": self._position_gain,
            "velocity_gain": self._velocity_gain,
            "velocity_integrator": self._velocity_integrator,
        }

    async def execute(self, action: str, **params: Any) -> Any:
        self._require_connected()
        if action == "set_position_input":
            self._position_input = params["value"]
            return None
        if action == "set_position_gain":
            self._position_gain = params["value"]
            return None
        if action == "set_velocity_gain":
            self._velocity_gain = params["value"]
            return None
        if action == "set_velocity_integrator":
            self._velocity_integrator = params["value"]
            return None
        raise HardwareError(f"unknown action: {action}")

    async def list_actions(self) -> List[str]:
        return list(COMMAND_CHANNELS)

    async def stream_samples(self) -> AsyncIterator[dict]:
        while True:
            error = self._position_input - self._position
            self._integral += error * SAMPLE_INTERVAL_S
            velocity_command = error * self._position_gain + self._velocity_integrator * self._integral

            rate = min(self._velocity_gain * SAMPLE_INTERVAL_S, 1.0)
            self._velocity += (velocity_command - self._velocity) * rate
            self._position += self._velocity * SAMPLE_INTERVAL_S

            current = abs(velocity_command - self._velocity) * 0.5 + abs(self._velocity) * 0.1
            current += abs(random.gauss(0, 0.02))

            yield {
                "position": self._position,
                "velocity": self._velocity,
                "current": current,
            }
            await asyncio.sleep(SAMPLE_INTERVAL_S)
