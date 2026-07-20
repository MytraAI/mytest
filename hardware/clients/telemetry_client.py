"""Minimal Telemetry Client/subscriber. Used two ways per the
architecture: inside the testcase execution process (for in-test
logic), and as the raw continuous path into the Telemetry Aggregator.
Both are the same subscriber pattern - just consumed differently by
the caller.

verify_channels() is the positive-confirmation half of channel
declaration: it blocks for one live frame and raises
MissingChannelError if any expected channel isn't actually in it,
rather than trusting a hand-maintained list that could drift from what
the driver actually streams.
"""
from __future__ import annotations

from typing import Iterable, Iterator

import zmq

from ..backend import MissingChannelError
from ..protocol import DEFAULT_TELEMETRY_ENDPOINT, TELEMETRY_TOPIC, TelemetryFrame


class TelemetryClient:
    """Subscriber for the hardware driver's raw telemetry stream."""

    def __init__(self, endpoint: str = DEFAULT_TELEMETRY_ENDPOINT):
        self._ctx = zmq.Context.instance()
        self._socket = self._ctx.socket(zmq.SUB)
        self._socket.setsockopt(zmq.SUBSCRIBE, TELEMETRY_TOPIC)
        self._socket.connect(endpoint)

    def frames(self) -> Iterator[TelemetryFrame]:
        """Blocking generator of telemetry frames. Iterate with a
        for-loop; break out of the loop (or call close()) to stop."""
        while True:
            _, raw = self._socket.recv_multipart()
            yield TelemetryFrame.from_bytes(raw)

    def verify_channels(self, expected: Iterable[str]) -> None:
        """Block for one live frame and raise MissingChannelError if
        any of `expected` isn't among its channels."""
        frame = next(self.frames())
        missing = set(expected) - set(frame.channels)
        if missing:
            raise MissingChannelError(f"missing telemetry channels: {sorted(missing)}")

    def close(self) -> None:
        self._socket.close(linger=0)
