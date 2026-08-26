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

**Two kinds of channel, and the difference matters.** set_state() LATCHES: a
value pushed from a code path sits on every frame until something pushes again.
That is right for an event - a step name, a brake engagement, an operator's
answer - and wrong for a live quantity, which then reads as a staircase whose
steps are wherever the code happened to push. So a test can also register a
DERIVATION, evaluated on every tick against the newest frame of each device,
which makes such a channel sampled rather than pushed. See set_derivation().

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
import time
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

import zmq

from protocol.wire import (
    DEFAULT_TELEMETRY_HWM,
    DEFAULT_RUN_STATE_ENDPOINT,
    STATE_PUBLISH_INTERVAL_S,
    RUN_STATE_TOPIC,
    RunStateFrame,
)

logger = logging.getLogger(__name__)

DERIVATION_FRAME_TIMEOUT_S = 10.0
"""How long await_derivation_frames() waits for a first frame from each device a
derivation reads. Generous: it is crossed once per run, right after the readers start,
and a driver that has not published in ten seconds is not slow."""

DERIVATION_FRAME_POLL_S = 0.05


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
        self._latest_frames: Dict[str, Dict[str, Any]] = {}
        """Newest channels seen from each device, fed by whoever is already consuming
        those streams in this process - see record_frame()."""
        self._derivation: Optional[Callable[[Dict[str, Dict[str, Any]]], Dict[str, Any]]] = None
        self._derivation_devices: Tuple[str, ...] = ()

    def set_state(self, name: str, value: Any) -> None:
        """Publish a named state value (e.g. a step name, a rule's status, a
        derived quantity) on every frame from now on, until overwritten."""
        with self._state_lock:
            self._state[name] = value

    def set_derivation(
        self,
        derivation: Callable[[Dict[str, Dict[str, Any]]], Dict[str, Any]],
        from_devices: Sequence[str] = (),
    ) -> None:
        """Register the test's derived channels: a function of the newest frame of each
        device, keyed by device name, returning channel values to publish.

        `from_devices` is which devices it reads, refused here if the run does not claim
        them - a derivation waiting on a device that is not part of this run publishes
        nothing, and its channels then hold whatever their channel list seeded them with:
        present in the recording, numeric, and wrong.

        Called on the publisher thread on every tick, so it must be cheap and it must not
        touch a socket - ZeroMQ sockets belong to the thread that made them, and the
        telemetry subscribers here belong to the test's own thread. It gets a plain dict of
        already-received channels for exactly that reason.

        A function of ALL the devices rather than one, because the interesting derived
        quantities cross them: bus power is the supply's volts times its amps, and brake
        energy is a rail current against an axis velocity. A per-stream callback cannot
        express either."""
        unclaimed = sorted(set(from_devices) - set(self._devices))
        if unclaimed:
            raise ValueError(
                f"derived channels read {unclaimed}, which this run does not claim "
                f"(devices: {sorted(self._devices)}) - nothing would ever feed them"
            )
        self._derivation = derivation
        self._derivation_devices = tuple(from_devices)

    def record_frame(self, device: str, channels: Dict[str, Any]) -> None:
        """Hand the newest channels from `device` to the derivation.

        Called by whatever thread is already reading that stream - today
        LiveRulebookRunner's, one per stream - so no subscriber exists solely for this and
        nothing crosses the wire. A plain dict assignment under the state lock: the
        derivation reads it from the publisher thread."""
        with self._state_lock:
            self._latest_frames[device] = channels

    def await_derivation_frames(self, timeout_s: float = DERIVATION_FRAME_TIMEOUT_S) -> None:
        """Block until a frame has arrived from every device the derivation reads, or raise.

        The static check in set_derivation() catches a device this run never claimed. This
        catches the other half: a stream that exists but was never handed to whatever calls
        record_frame(), which no amount of declaring can detect until the frames do or do
        not turn up. Called once, after those readers are started."""
        if not self._derivation_devices:
            return
        deadline = time.monotonic() + timeout_s
        while True:
            with self._state_lock:
                missing = sorted(set(self._derivation_devices) - set(self._latest_frames))
            if not missing:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"derived channels read {missing}, and no frame arrived from "
                    f"{'them' if len(missing) > 1 else 'it'} within {timeout_s}s - the "
                    f"stream exists but nothing in this process is reading it"
                )
            time.sleep(DERIVATION_FRAME_POLL_S)

    def _derived_state(self) -> Dict[str, Any]:
        """This tick's derived channels, or nothing if no derivation is registered.

        Deliberately never raises. A derivation reading a channel a device has not sent
        yet - or a device whose stream has not arrived - would otherwise kill the publisher
        thread, and with it the engine's only evidence that the run is still open."""
        if self._derivation is None:
            return {}
        with self._state_lock:
            latest = dict(self._latest_frames)
        try:
            return self._derivation(latest)
        except Exception:
            logger.exception("test %s: derived channels failed, publishing without them",
                             self._test_id)
            return {}

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
                # Into the state itself, not just this frame, so state_snapshot() carries
                # them too - which is what lets a Bound gate on a derived channel the same
                # way it can on a pushed one.
                for name, value in self._derived_state().items():
                    self.set_state(name, value)
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
