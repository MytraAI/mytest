"""Simulated DAQ backend for local development and testing.

Generates synthetic channel data (sine waves + noise) once acquisition
starts. Swap this out for a real DewesoftX adapter on the actual test
stand by implementing the same HardwareBackend interface - the command
and telemetry servers don't need to change at all.

See mock_channels.py for this backend's declared telemetry/command
surface (TELEMETRY_CHANNELS/COMMAND_CHANNELS).
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from typing import Any, AsyncIterator, List, Optional

from ..backend import HardwareBackend, HardwareError
from .mock_channels import COMMAND_CHANNELS, TELEMETRY_CHANNELS

SAMPLE_INTERVAL_S = 0.02  # 50 Hz - tune to taste for local testing


class MockDaqBackend(HardwareBackend):
    """Simulated DAQ backend - sine waves + noise, no real hardware needed."""

    def __init__(self) -> None:
        self._connected = False
        self._acquiring = False
        self._test_id: Optional[str] = None
        self._setup: Optional[str] = None
        self._start_time = 0.0

    async def connect(self) -> None:
        await asyncio.sleep(0.05)  # pretend there's a handshake
        self._connected = True

    async def disconnect(self) -> None:
        self._acquiring = False
        self._connected = False

    async def get_status(self) -> dict:
        return {
            "connected": self._connected,
            "acquiring": self._acquiring,
            "test_id": self._test_id,
            "setup": self._setup,
        }

    async def execute(self, action: str, **params: Any) -> Any:
        self._require_connected()
        if action == "get_channel_list":
            return list(TELEMETRY_CHANNELS)
        if action == "load_setup":
            self._setup = params["setup_name"]
            return None
        if action == "start_acquisition":
            if self._acquiring:
                raise HardwareError("acquisition already running")
            self._test_id = params["test_id"]
            self._acquiring = True
            self._start_time = time.monotonic()
            return None
        if action == "stop_acquisition":
            self._acquiring = False
            self._test_id = None
            return None
        if action == "set_digital_output":
            # No-op in simulation; a real backend would call into
            # DewesoftX's Control Out module here.
            return None
        if action == "trigger_event":
            # No-op in simulation; a real backend would write an event
            # marker into the DewesoftX timeline.
            return None
        raise HardwareError(f"unknown action: {action}")

    async def list_actions(self) -> List[str]:
        return list(COMMAND_CHANNELS)

    def _require_connected(self) -> None:
        if not self._connected:
            raise HardwareError("backend not connected")

    async def stream_samples(self) -> AsyncIterator[dict]:
        while True:
            if not self._acquiring:
                await asyncio.sleep(SAMPLE_INTERVAL_S)
                continue
            t = time.monotonic() - self._start_time
            yield {
                "chan_temp": 20.0 + 2 * math.sin(t / 3) + random.gauss(0, 0.05),
                "chan_pressure": 100.0 + 5 * math.sin(t / 5 + 1) + random.gauss(0, 0.2),
                "chan_vibration": random.gauss(0, 0.3),
            }
            await asyncio.sleep(SAMPLE_INTERVAL_S)
