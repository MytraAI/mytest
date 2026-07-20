"""LiveRulebookRunner: background thread that evaluates a Rulebook's
bounds against live telemetry frames, publishing per-bound/aggregate
pass/fail status and stopping evaluation on a fatal violation.

Started via start(telemetry_client), stopped via stop(). On a fatal
bound's violation, evaluate() raises FatalBoundViolation; _run()
catches it, logs, and stores it on self.fatal_violation so a caller
polling from another thread (e.g. TestCase.check_fatal_violation())
can notice and react at its own next safe point. Python can't force an
already-running thread to stop on its own, and a signal-based watchdog
was considered and rejected - signals only deliver on the interpreter's
main thread, and this codebase already runs TestCase.run() off the
main thread today (telemetry_engine/demo_*_run.py, via
asyncio.to_thread()). A step that never polls (directly, or via
TestCase.wait_for()) won't notice a violation until it returns on its
own - see step.py.

evaluate() uses wall-clock time (time.time()) for persistence_s
debounce - fine live, since a frame's own timestamp is itself stamped
via time.time() on the driver side. Post-hoc evaluation
(telemetry_engine.evaluation.Evaluator) instead passes each frame's own
recorded timestamp explicitly, since replay has no relation to real
time.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from hardware.clients.telemetry_client import TelemetryClient

from ..telemetry_publisher import TelemetryPublisher
from .rulebook import Rulebook, RulebookEvaluator

logger = logging.getLogger(__name__)


def _format_value(value: Any) -> str:
    """Format a channel value for logging - 3 decimals for numbers,
    plain str() otherwise, since Bound.expected can gate on discrete/
    non-numeric channels (e.g. a string state), not just floats."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:.3f}"
    return str(value)


class FatalBoundViolation(Exception):
    """Raised by LiveRulebookRunner.evaluate() when a fatal bound
    violates. Only ever raised on this runner's own background thread
    (see _run()) - never crosses threads on its own. A caller on
    another thread that wants to react to it polls
    LiveRulebookRunner.fatal_violation and re-raises this same
    instance itself, at its own next safe point - see this module's
    docstring."""

    def __init__(self, test_id: str, bound_label: str):
        super().__init__(f"test {test_id}: fatal bound {bound_label} violated")
        self.test_id = test_id
        self.bound_label = bound_label


class LiveRulebookRunner:
    """Evaluates registered Rulebooks against live frames, publishing pass/fail status and firing events on transitions."""

    def __init__(
        self,
        test_id: str,
        rulebooks: List[Rulebook],
        publisher: TelemetryPublisher,
        trigger_event: Optional[Callable[[str], None]] = None,
    ):
        self._test_id = test_id
        self._publisher = publisher
        self._trigger_event = trigger_event
        self._evaluator = RulebookEvaluator()
        for rulebook in rulebooks:
            self._evaluator.register(rulebook)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.fatal_violation: Optional[FatalBoundViolation] = None
        """Set once, from _run() on this runner's own background
        thread, if a fatal bound violates - None otherwise, including
        if start() was never called at all. A caller polling in its
        own loop (e.g. a test step's closed-loop wait) can check this
        each tick and re-raise it to stop what it's doing - see
        testcases/ydrive/teststeps/teststeps.py's cycle_position."""

    def start(self, telemetry_client: TelemetryClient) -> None:
        """Start a background thread evaluating telemetry_client's
        frames against this runner's Rulebook(s), until stop() or a
        fatal bound violates."""
        self._thread = threading.Thread(
            target=self._run, args=(telemetry_client,), name="live-rulebook-runner", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it to exit."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self, telemetry_client: TelemetryClient) -> None:
        for frame in telemetry_client.frames():
            if self._stop.is_set():
                return
            try:
                self.evaluate(dict(frame.channels))
            except FatalBoundViolation as exc:
                logger.error("test %s: fatal breach - stopping evaluation", self._test_id)
                self.fatal_violation = exc
                return

    def evaluate(self, channels: Dict[str, Any]) -> None:
        """Evaluate this frame, publish live per-bound/aggregate status,
        log, and fire events. Raises FatalBoundViolation if a fatal
        bound violated this frame, after publishing/logging for every
        transition this frame (not just the fatal one)."""
        transitions = self._evaluator.evaluate(channels, time.time())
        fatal_transition = None

        for transition in transitions:
            self._publisher.set_state(f"{transition.bound_label}_status", "FAIL" if transition.violated else "PASS")
            if transition.event_name and self._trigger_event is not None:
                self._trigger_event(transition.event_name)

            if transition.violated:
                log_fn = logger.error if transition.fatal else logger.warning
                log_fn(
                    "test %s: %s %s violated (%s=%s)",
                    self._test_id, "FATAL" if transition.fatal else "WARNING",
                    transition.bound_label, transition.channel, _format_value(transition.value),
                )
                if transition.fatal:
                    fatal_transition = transition
            else:
                logger.info(
                    "test %s: %s cleared (%s=%s)",
                    self._test_id, transition.bound_label, transition.channel, _format_value(transition.value),
                )

        if transitions:
            self._publisher.set_state("test_status", "FAIL" if self._evaluator.any_violated() else "PASS")

        if fatal_transition is not None:
            raise FatalBoundViolation(self._test_id, fatal_transition.bound_label)
