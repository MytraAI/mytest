"""Simulated power supply backend for local development and testing.

Proves the generalized HardwareBackend interface fits a device that
looks nothing like a DAQ: no acquisition/setup concept, just an output
setpoint and an enable flag. The command and telemetry server code is
unchanged from the DAQ backend - only this file, and a matching
PowerSupplyCommandClient, are new.

See mock_channels.py for this backend's declared telemetry/command
surface (TELEMETRY_CHANNELS/COMMAND_CHANNELS).
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, AsyncIterator, List

from protocol.wire import DEVICE_POWER_SUPPLY

from ..backend import HardwareBackend, HardwareError
from .mock_channels import COMMAND_CHANNELS

SAMPLE_INTERVAL_S = 0.02  # 50 Hz - tune to taste for local testing


class MockPowerSupplyBackend(HardwareBackend):
    """Simulated power supply - setpoint + noise readback, no real hardware needed."""

    device = DEVICE_POWER_SUPPLY
    sample_interval_s = SAMPLE_INTERVAL_S

    def __init__(self) -> None:
        self._connected = False
        self._output_enabled = False
        self._setpoint_voltage = 0.0
        self._setpoint_current = 0.0

    async def connect(self) -> None:
        await asyncio.sleep(0.05)  # pretend there's a handshake
        self._connected = True

    async def disconnect(self) -> None:
        self._output_enabled = False
        self._connected = False

    async def get_status(self) -> dict:
        return {
            "connected": self._connected,
            "output_enabled": self._output_enabled,
            "setpoint_voltage": self._setpoint_voltage,
            "setpoint_current": self._setpoint_current,
        }

    async def execute(self, action: str, **params: Any) -> Any:
        self._require_connected()
        if action == "set_output":
            self._setpoint_voltage = params["voltage"]
            self._setpoint_current = params["current"]
            return None
        if action == "enable_output":
            self._output_enabled = params["enabled"]
            return None
        raise HardwareError(f"unknown action: {action}")

    async def list_actions(self) -> List[str]:
        return list(COMMAND_CHANNELS)

    async def stream_samples(self) -> AsyncIterator[dict]:
        while True:
            if not self._output_enabled:
                yield {"voltage": 0.0, "current": 0.0}
            else:
                yield {
                    "voltage": self._setpoint_voltage + random.gauss(0, 0.02),
                    "current": self._setpoint_current + random.gauss(0, 0.01),
                }
            await asyncio.sleep(SAMPLE_INTERVAL_S)
