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

from hardware.protocol import (
    DEFAULT_TAGGED_TELEMETRY_ENDPOINT,
    DEFAULT_TELEMETRY_ENDPOINT,
    TAGGED_TELEMETRY_TOPIC,
    TELEMETRY_TOPIC,
    TaggedTelemetryFrame,
    TelemetryFrame,
)

from .storage import MergedItem

logger = logging.getLogger(__name__)


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
        socket.connect(self._raw_endpoint)
        logger.info("aggregator subscribed to raw stream at %s", self._raw_endpoint)
        try:
            while True:
                _, raw = await socket.recv_multipart()
                await queue.put(TelemetryFrame.from_bytes(raw))
        finally:
            socket.close(linger=0)

    async def _pump_tagged(self, queue: "asyncio.Queue[MergedItem]") -> None:
        socket = self._ctx.socket(zmq.SUB)
        socket.setsockopt(zmq.SUBSCRIBE, TAGGED_TELEMETRY_TOPIC)
        socket.connect(self._tagged_endpoint)
        logger.info("aggregator subscribed to tagged stream at %s", self._tagged_endpoint)
        try:
            while True:
                _, payload = await socket.recv_multipart()
                await queue.put(TaggedTelemetryFrame.from_bytes(payload))
        finally:
            socket.close(linger=0)
