"""LiveRulebookRunner: wires a Rulebook's live evaluation into a
running test case's side effects: publishing per-bound and aggregate
pass/fail status via a TelemetryPublisher, logging, and (optionally)
firing a hardware event for any bound that has one.

Runs its own background thread - started via start(), stopped via
stop(), the same pattern TelemetryPublisher uses - continuously
consuming a telemetry client's frames and evaluating them against this
runner's Rulebook(s). This is deliberate: a test step's own sequencing
logic (e.g. testcases/example_dut/teststeps/teststeps.py) never
touches a telemetry frame, a channels dict, or evaluation at all. It's
just a plain elapsed-time loop using Stopwatch.wait() to pace itself.

On a fatal bound's violation, evaluate() raises FatalBoundViolation
directly, right where the violation is detected - not via a flag
polled from elsewhere. _run() (this class's own background thread)
catches it, logs, and stops evaluating for the rest of this test.

This exception is deliberately NOT propagated into MainExecution's own
thread. Python exceptions don't cross threads, and there's no safe way
to force an already-running thread to stop - the unsafe ways (async
exception injection, OS signals) were rejected. See the architecture
doc's open question on the resulting risk: MainExecution can keep
running, and keep commanding hardware, after evaluation itself has
already stopped.

This is generic glue, not test-specific logic. Any test case using
Rulebook-based evaluation needs exactly this wiring - evaluate this
frame, publish status, log, fire events, stop on fatal - so it lives
here once instead of being reimplemented inside each test case's
MainExecution.

evaluate() uses wall-clock time (time.time()) for persistence_s
debounce, generated internally rather than taken from the telemetry
frame. Live and wall-clock time are the same thing here: a frame's own
timestamp is itself stamped via time.time() on the hardware driver
side, so the two differ only by microseconds of network latency during
a live run.

This is specific to the live path. Post-hoc evaluation
(telemetry_engine.evaluation.Evaluator) replays archived data, where
wall-clock time during replay has no relationship to the original
test's real duration - so it continues to explicitly pass each frame's
own recorded timestamp into RulebookEvaluator.evaluate() directly,
unaffected by this class.
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
    (see _run()). Deliberately not caught/re-raised anywhere that would
    reach MainExecution's thread - see this module's docstring."""

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
            except FatalBoundViolation:
                logger.error("test %s: fatal breach - stopping evaluation", self._test_id)
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
