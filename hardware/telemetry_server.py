"""Telemetry server: pub/sub endpoint for the hardware driver.

Runs a ZeroMQ PUB socket, forwarding whatever the backend's
`stream_samples()` produces as timestamped, sequenced frames.

Two kinds of subscribers connect here per the architecture: the
testcase execution process's Telemetry Client (for in-test logic),
and - via the same feed - the Telemetry Aggregator's raw continuous
path. This server does no evaluation or transformation. It stays
fast and dumb on purpose.
"""
from __future__ import annotations

import logging

import zmq
import zmq.asyncio

from .backend import HardwareBackend
from .protocol import DEFAULT_TELEMETRY_ENDPOINT, TELEMETRY_TOPIC, TelemetryFrame

logger = logging.getLogger(__name__)


class TelemetryServer:
    """Publisher forwarding a HardwareBackend's sample stream as telemetry frames."""

    def __init__(self, backend: HardwareBackend, endpoint: str = DEFAULT_TELEMETRY_ENDPOINT):
        self._backend = backend
        self._endpoint = endpoint
        self._ctx = zmq.asyncio.Context.instance()
        self._socket = self._ctx.socket(zmq.PUB)

    async def run(self) -> None:
        """Bind the socket and publish telemetry frames until cancelled."""
        self._socket.bind(self._endpoint)
        logger.info("telemetry server publishing on %s", self._endpoint)
        seq = 0
        try:
            async for channels in self._backend.stream_samples():
                frame = TelemetryFrame.now(seq=seq, channels=channels)
                await self._socket.send_multipart([TELEMETRY_TOPIC, frame.to_bytes()])
                seq += 1
        finally:
            self._socket.close(linger=0)
