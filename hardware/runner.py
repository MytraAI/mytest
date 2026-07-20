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
    """Connect the backend, serve command/telemetry until SIGINT/SIGTERM (or
    either server task fails on its own), then disconnect.

    A server task dying unprompted - e.g. a real backend's stream_samples()
    raising when a physical connection drops - is treated the same as a
    shutdown signal: the other task is cancelled, the backend is
    disconnected, and the original exception is re-raised so the process
    actually exits (loud, visible in the log/exit code) instead of quietly
    continuing with one server silently dead. Mocks never hit this path -
    only a real backend's I/O can fail this way.
    """
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
    stop_task = asyncio.create_task(stop.wait(), name="stop_signal")

    await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)

    stop_task.cancel()
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    await backend.disconnect()

    for task, result in zip(tasks, results):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            logger.error("%s failed, shutting down: %r", task.get_name(), result)
            raise result
