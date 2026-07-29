"""Telemetry Aggregator.

Merges the hardware driver's raw telemetry stream with the testcase
execution process's tagged stream into one in-process feed for
evaluation/storage to consume.

No correlation/joining by seq happens here. Raw and tagged frames are
multiplexed as they arrive, not paired up. A tagged frame always
arrives after its raw counterpart, since it has to hop one extra leg
(driver -> publisher -> aggregator, vs. driver -> aggregator directly
for raw frames). Joining them here would mean buffering raw frames
against a timeout - real design work that nothing needs solved yet,
since no consumer of joined data exists. This stays a dumb multiplexer,
same spirit as the driver's telemetry server staying "fast and dumb on
purpose".
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import zmq
import zmq.asyncio

from protocol.wire import (
    DEFAULT_TELEMETRY_HWM,
    DEFAULT_TAGGED_TELEMETRY_ENDPOINT,
    DEFAULT_TELEMETRY_ENDPOINT,
    TAGGED_TELEMETRY_TOPIC,
    TELEMETRY_TOPIC,
    TaggedTelemetryFrame,
    TelemetryFrame,
)

from .storage import MergedItem

logger = logging.getLogger(__name__)

_STALENESS_TIMEOUT_S = 5.0
"""How long a pump waits for a frame before logging that its stream has
gone silent. Unlike the clients (which raise TelemetryTimeout to fail a
test), the aggregator is a long-lived service: a silent stream here just
means there's nothing to aggregate right now, so each pump logs a
staleness warning and keeps looping, recovering on its own when frames
resume. Bounding the receive is also what lets the pump notice
cancellation promptly on shutdown."""
_STALENESS_TIMEOUT_MS = int(_STALENESS_TIMEOUT_S * 1000)


class Aggregator:
    """Merges the raw and tagged telemetry streams into one async feed."""

    def __init__(
        self,
        raw_endpoint: str = DEFAULT_TELEMETRY_ENDPOINT,
        tagged_endpoint: str = DEFAULT_TAGGED_TELEMETRY_ENDPOINT,
    ):
        self._raw_endpoint = raw_endpoint
        self._tagged_endpoint = tagged_endpoint
        self._ctx = zmq.asyncio.Context.instance()

    async def merged_stream(self) -> AsyncIterator[MergedItem]:
        """Yield raw and tagged frames as they arrive, multiplexed, unjoined."""
        queue: asyncio.Queue[MergedItem] = asyncio.Queue()
        raw_task = asyncio.create_task(self._pump_raw(queue), name="aggregator-pump-raw")
        tagged_task = asyncio.create_task(self._pump_tagged(queue), name="aggregator-pump-tagged")
        try:
            while True:
                yield await queue.get()
        finally:
            raw_task.cancel()
            tagged_task.cancel()
            await asyncio.gather(raw_task, tagged_task, return_exceptions=True)

    async def _pump_raw(self, queue: "asyncio.Queue[MergedItem]") -> None:
        socket = self._ctx.socket(zmq.SUB)
        socket.setsockopt(zmq.SUBSCRIBE, TELEMETRY_TOPIC)
        socket.setsockopt(zmq.RCVHWM, DEFAULT_TELEMETRY_HWM)
        socket.connect(self._raw_endpoint)
        logger.info("aggregator subscribed to raw stream at %s", self._raw_endpoint)
        await self._pump(socket, "raw", self._raw_endpoint, queue, TelemetryFrame.from_bytes)

    async def _pump_tagged(self, queue: "asyncio.Queue[MergedItem]") -> None:
        socket = self._ctx.socket(zmq.SUB)
        socket.setsockopt(zmq.SUBSCRIBE, TAGGED_TELEMETRY_TOPIC)
        socket.setsockopt(zmq.RCVHWM, DEFAULT_TELEMETRY_HWM)
        socket.connect(self._tagged_endpoint)
        logger.info("aggregator subscribed to tagged stream at %s", self._tagged_endpoint)
        await self._pump(socket, "tagged", self._tagged_endpoint, queue, TaggedTelemetryFrame.from_bytes)

    async def _pump(self, socket, label, endpoint, queue, decode) -> None:
        """Shared pump loop for both streams: poll with a staleness
        deadline instead of blocking forever in recv_multipart(). When a
        stream goes silent past _STALENESS_TIMEOUT_S, log once and keep
        looping (recovering when frames resume) rather than raising -
        see _STALENESS_TIMEOUT_S. The `stale` latch keeps it to one
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
                            "aggregator: %s stream silent for >%.1fs at %s",
                            label, _STALENESS_TIMEOUT_S, endpoint,
                        )
                        stale = True
                    continue
                if stale:
                    logger.info("aggregator: %s stream resumed at %s", label, endpoint)
                    stale = False
                _, payload = await socket.recv_multipart()
                await queue.put(decode(payload))
        finally:
            socket.close(linger=0)
