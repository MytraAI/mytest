"""Shared process wiring for a hardware driver, independent of which
device it's running. Each device gets its own dedicated entry-point
script (main_daq.py, main_power_supply.py, ...) that just picks a
backend and endpoints and hands them to `run()` here - this is what
keeps that wiring itself generic across device types.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import List

from .backend import HardwareBackend
from .command_server import CommandServer
from .telemetry_server import TelemetryServer

logger = logging.getLogger(__name__)


def _request_stop_on_signal(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """Make Ctrl+C and SIGTERM set `stop`, on every platform.

    asyncio's own signal handlers do not exist on Windows, and the fallback there
    is not "no handling": Python still delivers SIGINT to the main thread, where
    it surfaces as a KeyboardInterrupt that cancels whatever is being awaited.
    That is the difference between a driver shutting down and a driver being
    interrupted - the graceful path sets this event and lets run() finish, and
    only the graceful path was ever reached on POSIX.

    So on Windows the handler is installed with signal.signal() and wakes the
    loop through call_soon_threadsafe, since setting an asyncio.Event from a
    signal handler otherwise leaves the loop asleep until its next event."""
    def request_stop(*_: object) -> None:
        loop.call_soon_threadsafe(stop.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            try:
                signal.signal(sig, request_stop)
            except (OSError, ValueError):
                # A platform that will not take this signal at all. The marker
                # file path (tools/stop_test.py) is what a test relies on, not
                # this - see AI/Mytest.md's OS compatibility section.
                logger.debug("no handler could be installed for %s", sig)


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

    The device name stamped onto published frames, and the sample interval
    that sizes the publisher's high-water mark, are read off the backend
    itself - they're properties of what's being driven, not of whoever
    started it, so no entry point has to pass them.
    """
    await backend.connect()

    command_server = CommandServer(backend, command_endpoint)
    telemetry_server = TelemetryServer(
        backend, telemetry_endpoint, device=backend.device, sample_interval_s=backend.sample_interval_s
    )

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    _request_stop_on_signal(loop, stop)

    tasks = [
        asyncio.create_task(command_server.run(), name="command_server"),
        asyncio.create_task(telemetry_server.run(), name="telemetry_server"),
    ]
    stop_task = asyncio.create_task(stop.wait(), name="stop_signal")

    results: List[BaseException] = []
    try:
        await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
    finally:
        # In a finally, because the wait does not always return: a Ctrl+C that
        # arrives as a KeyboardInterrupt cancels it, and everything after it -
        # including disconnecting the device - was being skipped. On the ODrive
        # that meant an axis left armed; on the supply, a link left for the OS to
        # tear down, on an instrument that accepts one socket at a time.
        stop_task.cancel()
        for task in tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            results = await asyncio.gather(*tasks, return_exceptions=True)
        # Suppressed separately: a disconnect must be attempted even if gathering
        # the cancelled servers is itself interrupted.
        with contextlib.suppress(asyncio.CancelledError):
            await backend.disconnect()

    for task, result in zip(tasks, results):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            logger.error("%s failed, shutting down: %r", task.get_name(), result)
            raise result
