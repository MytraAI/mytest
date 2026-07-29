"""Telemetry Publisher: subscribes to the hardware driver's raw
telemetry stream and republishes it tagged with test-case context -
the test id, test name, and any published state - for the future
Telemetry Aggregator to consume.

Runs as a background thread inside the testcase execution process,
started in TestCase.pre_test_setup() and stopped in
TestCase.post_test_teardown() (see base.py). It opens its own raw
subscription rather than sharing one with the test case's own
in-sequence logic, so the two never contend over the same socket.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

import zmq

from protocol.wire import (
    DEFAULT_TELEMETRY_HWM,
    DEFAULT_TAGGED_TELEMETRY_ENDPOINT,
    DEFAULT_TELEMETRY_ENDPOINT,
    TAGGED_TELEMETRY_TOPIC,
    TELEMETRY_TOPIC,
    TaggedTelemetryFrame,
    TelemetryFrame,
)

logger = logging.getLogger(__name__)

_POLL_TIMEOUT_MS = 200


class TelemetryPublisher:
    """Background-thread publisher that tags raw telemetry frames with test-case context and republishes them."""

    def __init__(
        self,
        test_id: str,
        test_name: str,
        raw_endpoint: str = DEFAULT_TELEMETRY_ENDPOINT,
        tagged_endpoint: str = DEFAULT_TAGGED_TELEMETRY_ENDPOINT,
    ):
        self._test_id = test_id
        self._test_name = test_name
        self._raw_endpoint = raw_endpoint
        self._tagged_endpoint = tagged_endpoint
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()
        self._state: Dict[str, Any] = {}

    def set_state(self, name: str, value: Any) -> None:
        """Publish a named state value (e.g. a gating flag) merged into
        every tagged frame from now on, alongside real hardware
        channels, until overwritten by another set_state() call."""
        with self._state_lock:
            self._state[name] = value

    def _state_snapshot(self) -> Dict[str, Any]:
        with self._state_lock:
            return dict(self._state)

    def start(self) -> None:
        """Start the background thread."""
        self._thread = threading.Thread(target=self._run, name="telemetry-publisher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it to exit."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        """Background-thread loop: drain the raw stream, republish tagged frames."""
        ctx = zmq.Context.instance()
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.SUBSCRIBE, TELEMETRY_TOPIC)
        sub.setsockopt(zmq.RCVHWM, DEFAULT_TELEMETRY_HWM)
        sub.connect(self._raw_endpoint)

        pub = ctx.socket(zmq.PUB)
        pub.setsockopt(zmq.SNDHWM, DEFAULT_TELEMETRY_HWM)
        pub.bind(self._tagged_endpoint)

        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)

        logger.info(
            "telemetry publisher for test %s (%s): subscribing %s, publishing %s",
            self._test_id, self._test_name, self._raw_endpoint, self._tagged_endpoint,
        )
        try:
            while not self._stop.is_set():
                events = dict(poller.poll(timeout=_POLL_TIMEOUT_MS))
                if sub not in events:
                    continue
                _, raw = sub.recv_multipart()
                frame = TelemetryFrame.from_bytes(raw)
                tagged = TaggedTelemetryFrame.from_telemetry_frame(
                    frame, test_id=self._test_id, test_name=self._test_name, extra_channels=self._state_snapshot()
                )
                pub.send_multipart([TAGGED_TELEMETRY_TOPIC, tagged.to_bytes()])
        finally:
            sub.close(linger=0)
            pub.close(linger=0)
