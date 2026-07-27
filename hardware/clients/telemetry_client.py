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

Timeout/watchdog behavior: frames() bounds how long it will wait for
each frame via timeout_s (a staleness deadline). A dead hardware driver
or a stopped publisher would otherwise block any consumer forever - and
this is the most-consumed hang point in the framework (the
LiveRulebookRunner's evaluation loop, verify_channels() during setup,
and get_pos_estimate()/get_vel_estimate() inside a closed-loop move all
block here). If no frame arrives within timeout_s of the previous one
(or of the call, for the first frame), frames() raises TelemetryTimeout
instead of blocking. The deadline is a single poll per frame, so a
healthy stream - frames far more frequent than timeout_s - is never
affected; the deadline only fires when the stream genuinely goes silent.
"""
from __future__ import annotations

from typing import Iterable, Iterator

import zmq

from ..backend import MissingChannelError
from ..protocol import DEFAULT_TELEMETRY_ENDPOINT, TELEMETRY_TOPIC, TelemetryFrame


class TelemetryTimeout(TimeoutError):
    """Raised by TelemetryClient.frames()/verify_channels() when no
    frame arrives within timeout_s of the previous one (or of the call,
    for the first frame) - a dead driver or stopped publisher. Subclasses
    builtin TimeoutError so existing `except TimeoutError` handlers (e.g.
    ydrive move_to's own arrival deadline) still catch it, while callers
    that care can tell a silent-stream timeout apart from other errors."""


class TelemetryClient:
    """Subscriber for the hardware driver's raw telemetry stream."""

    def __init__(self, endpoint: str = DEFAULT_TELEMETRY_ENDPOINT, timeout_s: float = 5.0):
        self._ctx = zmq.Context.instance()
        self._socket = self._ctx.socket(zmq.SUB)
        self._socket.setsockopt(zmq.SUBSCRIBE, TELEMETRY_TOPIC)
        self._socket.connect(endpoint)
        self._timeout_s = timeout_s
        self._poller = zmq.Poller()
        self._poller.register(self._socket, zmq.POLLIN)

    def frames(self) -> Iterator[TelemetryFrame]:
        """Blocking generator of telemetry frames. Iterate with a
        for-loop; break out of the loop (or call close()) to stop.

        Each frame must arrive within timeout_s of the previous one (or
        of the call, for the first); otherwise raises TelemetryTimeout
        rather than blocking forever on a dead stream - see the module
        docstring. recv_multipart() below can't block: it only runs once
        poll() has already reported the whole message is ready."""
        timeout_ms = int(self._timeout_s * 1000)
        while True:
            if not self._poller.poll(timeout_ms):
                raise TelemetryTimeout(
                    f"no telemetry frame within {self._timeout_s:.1f}s - "
                    "hardware driver or publisher may have stopped"
                )
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
