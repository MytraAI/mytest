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

import threading
from typing import Iterable, Iterator, List

import zmq

from ..backend import MissingChannelError
from protocol.wire import DEFAULT_TELEMETRY_ENDPOINT, DEFAULT_TELEMETRY_HWM, TELEMETRY_TOPIC, TelemetryFrame


class TelemetryTimeout(TimeoutError):
    """Raised by TelemetryClient.frames()/verify_channels() when no
    frame arrives within timeout_s of the previous one (or of the call,
    for the first frame) - a dead driver or stopped publisher. Subclasses
    builtin TimeoutError so existing `except TimeoutError` handlers (e.g.
    a closed-loop move's own arrival deadline) still catch it, while callers
    that care can tell a silent-stream timeout apart from other errors."""


class ConcurrentTelemetryRead(RuntimeError):
    """Raised when two threads read one TelemetryClient.

    ONE CONSUMER PER CLIENT. A ZeroMQ SUB socket is not thread-safe, and two
    threads in recv_multipart() at once interleave: each takes part of a
    two-part message and both are left holding a fragment. That surfaced as
    `ValueError: not enough values to unpack (expected 2, got 1)` from a caller
    that had nothing to do with either thread.

    The crash is the lesser half. A subscription delivers each frame ONCE, so
    two readers of one client divide the stream between them rather than each
    seeing all of it - and when one of them is LiveRulebookRunner, the fatal
    bounds are evaluated on whichever frames the other reader did not take
    first, silently. Both halves go away by giving each consumer its own client
    on the same endpoint, which is what the testbeds do."""


class TelemetryClient:
    """Subscriber for the hardware driver's raw telemetry stream.

    NOT SHARED BETWEEN THREADS - see ConcurrentTelemetryRead. A testbed that
    hands one client to a runner and reads another itself opens two clients on
    the same endpoint."""

    def __init__(self, endpoint: str = DEFAULT_TELEMETRY_ENDPOINT, timeout_s: float = 5.0):
        self._ctx = zmq.Context.instance()
        self._socket = self._ctx.socket(zmq.SUB)
        self._socket.setsockopt(zmq.SUBSCRIBE, TELEMETRY_TOPIC)
        self._socket.setsockopt(zmq.RCVHWM, DEFAULT_TELEMETRY_HWM)
        self._socket.connect(endpoint)
        self._timeout_s = timeout_s
        self._poller = zmq.Poller()
        self._poller.register(self._socket, zmq.POLLIN)
        self._reader = threading.Lock()
        """Held across each receive, never waited on: it is a detector, not a
        guard. Serializing two readers would stop the crash and leave them
        splitting the stream, which is the worse half of the bug - so a second
        thread is told what it has done instead of being made to queue."""

    def _recv_frame(self) -> bytes:
        """The payload of one message already reported ready by the poller.

        The topic frame is dropped: it is the subscription filter, not data.
        Both halves are checked rather than unpacked, so a message that is not
        the expected [topic, payload] pair says what arrived - a bare unpack
        reports only that a tuple was the wrong size, from a stack frame that
        does not mention telemetry at all."""
        if not self._reader.acquire(blocking=False):
            raise ConcurrentTelemetryRead(
                "this TelemetryClient is already being read by another thread - "
                "give each consumer its own client on the same endpoint"
            )
        try:
            parts: List[bytes] = self._socket.recv_multipart()
        finally:
            self._reader.release()
        if len(parts) != 2:
            raise ConcurrentTelemetryRead(
                f"expected a [topic, payload] telemetry message, got {len(parts)} "
                "part(s) - a torn message, which means this client is being read "
                "by more than one thread"
            )
        return parts[1]

    def frames(self) -> Iterator[TelemetryFrame]:
        """Blocking generator of telemetry frames. Iterate with a
        for-loop; break out of the loop (or call close()) to stop.

        Yields every frame exactly once, oldest first, discarding nothing -
        which is what latest_frame() trades away for freshness, and what a
        consumer aggregating over a window would need. Its cost is the one
        that motivated latest_frame(): a subscriber read later than it was
        created hands back its backlog first, so this answers a question about
        the present with a frame that may be seconds old.

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
            yield TelemetryFrame.from_bytes(self._recv_frame())

    def discard_backlog(self) -> int:
        """Drop every frame already queued, and report how many.

        For a consumer whose judgements are about the present, when its
        subscription is older than its first read: a client created at testbed
        start and first read once setup is done holds frames describing a stand
        that was still being set up. Never blocks - a queue with nothing in it
        discards nothing.

        Discarding here costs the record nothing. The telemetry engine
        subscribes to the same publisher directly and keeps its own copy of
        every frame."""
        dropped = 0
        while self._poller.poll(0):
            self._recv_frame()
            dropped += 1
        return dropped

    def latest_frame(self) -> TelemetryFrame:
        """Return the newest frame available, discarding any queued behind it.

        For a point read - "what is the axis doing right now" - where frames()
        would hand back the oldest queued frame instead. A subscriber that was
        created early and is read late has a backlog proportional to that gap
        (RCVHWM is 500 frames), so frames() can answer a question about the
        present with a reading seconds old, and a single read-and-compare against
        it is wrong rather than merely late.

        Blocks for one frame if none is queued, so it paces a polling loop the
        same way frames() does, and raises TelemetryTimeout on the same
        staleness deadline.

        THE WRONG PRIMITIVE FOR ANYTHING THAT AGGREGATES OVER TIME, and the
        discarding above is why. It is exactly right for "what is the value
        now": one answer, the freshest one. But a caller asking "what was the
        lowest value over ten seconds" needs every frame in that window, and
        this throws away whatever queued while the caller was busy - so the
        extreme it was looking for can be dropped without a trace. The loss is
        silent: the loop still returns a plausible number from a plausible
        count of samples.

        In practice a loop that does nothing but call this keeps up easily -
        the ODrive publishes about every 79 ms - so the hazard is a loop that
        does something else too, or waits on a second stream and therefore runs
        at the slower one's rate. Two consecutive calls can never return the
        same frame, since this consumes; frames() is what misses none of them.
        asimov's take_measurement_over_time() samples through this deliberately
        and records how many samples backed its answer - see AI/Mytest.md's
        open decision on a transport that would not consume on read."""
        frame = next(self.frames())
        while self._poller.poll(0):
            frame = TelemetryFrame.from_bytes(self._recv_frame())
        return frame

    def verify_channels(self, expected: Iterable[str]) -> None:
        """Block for one live frame and raise MissingChannelError if
        any of `expected` isn't among its channels."""
        frame = next(self.frames())
        missing = set(expected) - set(frame.channels)
        if missing:
            raise MissingChannelError(f"missing telemetry channels: {sorted(missing)}")

    def close(self) -> None:
        self._socket.close(linger=0)
