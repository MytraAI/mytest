"""Telemetry Aggregator.

Subscribes to every device's telemetry stream and to the testcase process's
run-state stream, and multiplexes them into one in-process feed for the
engine to consume.

One socket per device rather than one socket connected to many endpoints.
A single ZeroMQ SUB socket can connect to several publishers and would deliver
all of their frames, which is less code - but then a silent stream is only
detectable as "nothing at all is arriving". Per-socket pumps keep the useful
question answerable: *which* device went quiet while the others kept
publishing. On a long-lived recording service that observability is worth N
sockets.

No correlation or joining happens here. Frames are multiplexed as they
arrive; the engine decides where each one belongs by asking the run recorder,
which learns the open run and its declared devices from the state stream.
This stays a dumb multiplexer, same spirit as the driver's telemetry server
staying "fast and dumb on purpose".
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Mapping, Tuple, Union

import zmq
import zmq.asyncio

from protocol.wire import (
    DEFAULT_TELEMETRY_HWM,
    DEFAULT_RUN_STATE_ENDPOINT,
    TELEMETRY_ENDPOINTS,
    TELEMETRY_TOPIC,
    RUN_STATE_TOPIC,
    TelemetryFrame,
    RunStateFrame,
)

logger = logging.getLogger(__name__)

StreamItem = Union[TelemetryFrame, RunStateFrame]
"""What merged_stream() yields: device frames, and the testcase process's own
state announcements. Defined here rather than in storage.py - it is this
component's output type, and storage never sees either of them (the engine
turns frames into WriteItems first)."""

_STALENESS_TIMEOUT_S = 5.0
"""How long a pump waits for a frame before logging that its stream has
gone silent. Unlike the clients (which raise TelemetryTimeout to fail a
test), the aggregator is a long-lived service: a silent stream here just
means there's nothing to aggregate right now, so each pump logs a
staleness warning and keeps looping, recovering on its own when frames
resume. Bounding the receive is also what lets the pump notice
cancellation promptly on shutdown.

A device that simply isn't running is the normal case for an engine that
subscribes to every known device, so this warns once per silent spell rather
than per poll - see _pump's `stale` latch."""
_STALENESS_TIMEOUT_MS = int(_STALENESS_TIMEOUT_S * 1000)


class Aggregator:
    """Merges every device's telemetry and the run-state stream into one async feed."""

    def __init__(
        self,
        telemetry_endpoints: Mapping[str, str] = TELEMETRY_ENDPOINTS,
        state_endpoint: str = DEFAULT_RUN_STATE_ENDPOINT,
    ):
        self._telemetry_endpoints = dict(telemetry_endpoints)
        self._state_endpoint = state_endpoint
        self._ctx = zmq.asyncio.Context.instance()

    @property
    def devices(self) -> Tuple[str, ...]:
        """Which devices this aggregator is subscribed to, and therefore
        recording. Published on the engine's heartbeat so a test can confirm
        its own declared devices are covered before it starts - see
        protocol/heartbeat.py."""
        return tuple(self._telemetry_endpoints)

    async def merged_stream(self) -> AsyncIterator[StreamItem]:
        """Yield device frames and run-state frames as they arrive, multiplexed."""
        queue: asyncio.Queue[StreamItem] = asyncio.Queue()
        tasks = [
            asyncio.create_task(
                self._pump_telemetry(device, endpoint, queue), name=f"aggregator-pump-{device}"
            )
            for device, endpoint in self._telemetry_endpoints.items()
        ]
        tasks.append(asyncio.create_task(self._pump_state(queue), name="aggregator-pump-state"))
        try:
            while True:
                yield await queue.get()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _pump_telemetry(self, device: str, endpoint: str, queue: "asyncio.Queue[StreamItem]") -> None:
        socket = self._ctx.socket(zmq.SUB)
        socket.setsockopt(zmq.SUBSCRIBE, TELEMETRY_TOPIC)
        socket.setsockopt(zmq.RCVHWM, DEFAULT_TELEMETRY_HWM)
        socket.connect(endpoint)
        logger.info("aggregator subscribed to %s telemetry at %s", device, endpoint)
        await self._pump(socket, device, endpoint, queue, TelemetryFrame.from_bytes)

    async def _pump_state(self, queue: "asyncio.Queue[StreamItem]") -> None:
        socket = self._ctx.socket(zmq.SUB)
        socket.setsockopt(zmq.SUBSCRIBE, RUN_STATE_TOPIC)
        socket.setsockopt(zmq.RCVHWM, DEFAULT_TELEMETRY_HWM)
        socket.connect(self._state_endpoint)
        logger.info("aggregator subscribed to the run-state stream at %s", self._state_endpoint)
        await self._pump(socket, "test state", self._state_endpoint, queue, RunStateFrame.from_bytes)

    async def _pump(self, socket, label, endpoint, queue, decode) -> None:
        """Shared pump loop: poll with a staleness deadline instead of
        blocking forever in recv_multipart(). When a stream goes silent past
        _STALENESS_TIMEOUT_S, log once and keep looping (recovering when
        frames resume) rather than raising. The `stale` latch keeps it to one
        warning per silent spell, and one info line when it recovers."""
        poller = zmq.asyncio.Poller()
        poller.register(socket, zmq.POLLIN)
        stale = False
        try:
            while True:
                events = dict(await poller.poll(timeout=_STALENESS_TIMEOUT_MS))
                if socket not in events:
                    if not stale:
                        logger.warning(
                            "aggregator: %s silent for >%.1fs at %s", label, _STALENESS_TIMEOUT_S, endpoint
                        )
                        stale = True
                    continue
                if stale:
                    logger.info("aggregator: %s resumed at %s", label, endpoint)
                    stale = False
                _, payload = await socket.recv_multipart()
                await queue.put(decode(payload))
        finally:
            socket.close(linger=0)
