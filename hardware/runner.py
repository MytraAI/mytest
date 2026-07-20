"""Shared process wiring for a hardware driver, independent of which
device it's running. Each device gets its own dedicated entry-point
script (main_daq.py, main_power_supply.py, ...) that just picks a
backend and endpoints and hands them to `run()` here - this is what
keeps that wiring itself generic across device types.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from .backend import HardwareBackend
from .command_server import CommandServer
from .telemetry_server import TelemetryServer

logger = logging.getLogger(__name__)


async def run(backend: HardwareBackend, command_endpoint: str, telemetry_endpoint: str) -> None:
    """Connect the backend, serve command/telemetry until SIGINT/SIGTERM, then disconnect."""
    await backend.connect()

    command_server = CommandServer(backend, command_endpoint)
    telemetry_server = TelemetryServer(backend, telemetry_endpoint)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # signal handlers aren't available on all platforms

    tasks = [
        asyncio.create_task(command_server.run(), name="command_server"),
        asyncio.create_task(telemetry_server.run(), name="telemetry_server"),
    ]

    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await backend.disconnect()
