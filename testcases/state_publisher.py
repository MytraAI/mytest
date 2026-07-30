"""RunStatePublisher: broadcasts what the test is doing, and nothing else.

Runs as a background thread inside the testcase execution process for the
whole life of a run - started by TestCase.run() *before* PreTestSetup, stopped
after PostTestTeardown - and publishes one small RunStateFrame every
STATE_PUBLISH_INTERVAL_S carrying:

  - the run's identity (test_id, test_name),
  - the devices this run claims, so the engine knows whose frames belong to it,
  - every value the test has published via set_state().

**It does not touch telemetry.** An earlier design had this component
subscribe to a device's raw stream, merge test context into each frame, and
republish the lot - which meant every frame of every device travelled through
this process a second time, and meant only the single device it subscribed to
could ever be attributed to a run. The engine now subscribes to each driver
directly and merges this state into the rows it writes, so the relay hop is
gone: this publishes a few hundred bytes at 20 Hz instead of megabytes per
minute, and a test with any number of devices needs no extra wiring.

**Publishing is unconditional, not change-triggered.** A fixed tick makes one
mechanism do three jobs: propagate a change (within one tick), keep the run
alive in the engine's view, and heal ZeroMQ's slow-joiner drop - a subscriber
that missed the first message gets the next one 50 ms later rather than waiting
for the test to change something. Change detection would add state and buy
nothing.

Consumers: the telemetry engine (attribution and the state merged into rows),
the operator status page (live pass/fail), tools/stop_test.py (discovering the
running test's id), and tools/manual_gui.py (test context). All of them wanted
only state - which is what made removing the telemetry relay possible.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional, Sequence

import zmq

from protocol.wire import (
    DEFAULT_TELEMETRY_HWM,
    DEFAULT_RUN_STATE_ENDPOINT,
    STATE_PUBLISH_INTERVAL_S,
    RUN_STATE_TOPIC,
    RunStateFrame,
)

logger = logging.getLogger(__name__)


class RunStatePublisher:
    """Background-thread publisher of the run's identity, devices and state."""

    def __init__(
        self,
        test_id: str,
        test_name: str,
        devices: Sequence[str] = (),
        endpoint: str = DEFAULT_RUN_STATE_ENDPOINT,
        interval_s: float = STATE_PUBLISH_INTERVAL_S,
    ):
        self._test_id = test_id
        self._test_name = test_name
        self._devices = tuple(devices)
        self._endpoint = endpoint
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()
        self._state: Dict[str, Any] = {}

    def set_state(self, name: str, value: Any) -> None:
        """Publish a named state value (e.g. a step name, a rule's status, a
        derived quantity) on every frame from now on, until overwritten."""
        with self._state_lock:
            self._state[name] = value

    def state_snapshot(self) -> Dict[str, Any]:
        """The current state, for a caller in this process that needs it now.

        The live rules evaluator reads this to evaluate bounds against the
        union of a device's channels and the test's own state - which is what
        lets a Bound gate on a published state channel. It has to come from
        here rather than off the wire, because the evaluator consumes a
        device's raw stream and state exists only in this process and
        downstream of it. In-process, so it costs nothing and loses nothing."""
        with self._state_lock:
            return dict(self._state)

    def start(self) -> None:
        """Start the background thread, publishing immediately so the run is
        announced before any device driver exists."""
        self._thread = threading.Thread(target=self._run, name="test-state-publisher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it to exit.

        Once this returns the stream is quiet, which is how the engine learns
        the run is over - see telemetry_engine/run_recorder.py."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        ctx = zmq.Context.instance()
        pub = ctx.socket(zmq.PUB)
        pub.setsockopt(zmq.SNDHWM, DEFAULT_TELEMETRY_HWM)
        pub.bind(self._endpoint)

        logger.info(
            "test state publisher for test %s (%s): publishing %s at %.0f Hz, devices: %s",
            self._test_id,
            self._test_name,
            self._endpoint,
            1.0 / self._interval_s if self._interval_s > 0 else 0.0,
            ", ".join(self._devices) or "(none)",
        )
        try:
            while True:
                frame = RunStateFrame.now(
                    test_id=self._test_id,
                    test_name=self._test_name,
                    devices=self._devices,
                    state=self.state_snapshot(),
                )
                pub.send_multipart([RUN_STATE_TOPIC, frame.to_bytes()])
                if self._stop.wait(timeout=self._interval_s):
                    return
        finally:
            pub.close(linger=0)
