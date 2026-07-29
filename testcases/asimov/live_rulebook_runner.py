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
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from hardware.clients.telemetry_client import TelemetryClient, TelemetryTimeout
from protocol.verdict import BoundsResult, Violation

from ..telemetry_publisher import TelemetryPublisher
from .rulebook import Rulebook, RulebookEvaluator, UnevaluableBoundError

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


@dataclass
class RunSummary:
    """The whole-run bound outcome for the test's verdict (see
    TestCase.run()).

    `violations` is the complete transition timeline - every violate and
    every clear, in the order they happened, each carrying its frame's own
    seq/t so it can be lined up against (and replayed from) the stored
    telemetry. This is what lands in the verdict verbatim; there is no
    separate violations store.

    `bounds_result` applies the project's rule: *any* violation fails the
    run, fatal or not. `fatal` governs only whether the test aborts, never
    whether it passed - so a Bound is always a pass/fail criterion, never a
    purely informational monitor. Distinct from the evaluator's
    any_violated(), which is momentary (currently-violated).

    `evaluated_frames` is why NOT_EVALUATED exists: a runner that was
    constructed but never started (BaseYdriveTest deliberately leaves
    start() to its subclasses) produces an empty timeline, which must not
    read as a clean pass. Zero frames evaluated means the run had no
    monitoring at all, and the verdict says so.
    """

    violations: List[Violation] = field(default_factory=list)
    any_fatal: bool = False
    evaluated_frames: int = 0
    unevaluable: Optional[str] = None
    """Set if a bound became impossible to evaluate mid-run (see
    UnevaluableBoundError) - e.g. its channel started reporting a value
    that can't be compared against its limits. Forces NOT_EVALUATED rather
    than PASS: some frames did evaluate cleanly, but supervision then broke,
    and reporting "the DUT behaved" would claim knowledge the run doesn't
    have. The reason is carried so the verdict can say what broke."""

    @property
    def bounds_result(self) -> str:
        if self.evaluated_frames == 0 or self.unevaluable is not None:
            return BoundsResult.NOT_EVALUATED
        violated = any(v.transition == "violated" for v in self.violations)
        return BoundsResult.FAIL if violated else BoundsResult.PASS


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
        self._summary_lock = threading.Lock()
        self._violations: List[Violation] = []
        """Every bound transition this run, in order - the timeline that
        lands in the verdict verbatim. Appended in evaluate() on the
        background thread, read via summary() on the main thread once the
        runner is stopped - guarded by _summary_lock. See RunSummary."""
        self._evaluated_frames = 0
        """Frames actually evaluated, guarded by the same lock. Zero means
        this runner never ran, which the verdict records as
        NOT_EVALUATED rather than a clean pass - see RunSummary."""
        self._unevaluable: Optional[str] = None
        """Why a bound stopped being evaluable, if one did - guarded by the
        same lock, and what makes bounds_result NOT_EVALUATED instead of
        PASS. See RunSummary."""
        self.fatal_violation: Optional[Exception] = None
        """Set once, from _run() on this runner's own background thread,
        if a fatal bound violates (FatalBoundViolation) OR the telemetry
        stream goes silent (TelemetryTimeout) - None otherwise, including
        if start() was never called at all. Either is fatal: a fatal
        bound means the hardware breached a hard limit, a silent stream
        means we've lost live monitoring while the hardware may still be
        moving. A caller polling in its own loop (e.g. a test step's
        closed-loop wait) checks this each tick and re-raises it to stop
        what it's doing - see testcases/ydrive/teststeps/teststeps.py's
        cycle_position and TestCase.check_fatal_violation()."""

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
        try:
            for frame in telemetry_client.frames():
                if self._stop.is_set():
                    return
                try:
                    self.evaluate(dict(frame.channels), seq=frame.seq, frame_t=frame.t)
                except FatalBoundViolation as exc:
                    logger.error("test %s: fatal breach - stopping evaluation", self._test_id)
                    self.fatal_violation = exc
                    return
                except UnevaluableBoundError as exc:
                    with self._summary_lock:
                        self._unevaluable = str(exc)
                    # A bound we cannot evaluate is not a bound that passed.
                    # Treated exactly like a silent telemetry stream: we've
                    # lost supervision while the hardware may still be
                    # moving, so the test stops rather than running blind.
                    # Without this the exception would escape this thread and
                    # kill it, leaving fatal_violation unset and the test
                    # running with no monitoring at all - see
                    # UnevaluableBoundError.
                    logger.error("test %s: %s - stopping evaluation", self._test_id, exc)
                    self.fatal_violation = exc
                    return
        except TelemetryTimeout as exc:
            # The telemetry stream went silent (dead driver/publisher).
            # If we're already stopping, a quiet stream during teardown
            # isn't a failure - just exit. Otherwise it's fatal: we've
            # lost live safety monitoring mid-test, so store it the same
            # way a fatal bound is stored, for the test's own
            # check_fatal_violation() poll to raise and drive teardown.
            if self._stop.is_set():
                return
            logger.error("test %s: telemetry stream went silent - treating as fatal", self._test_id)
            self.fatal_violation = exc

    def evaluate(self, channels: Dict[str, Any], seq: int = -1, frame_t: Optional[float] = None) -> None:
        """Evaluate this frame, publish live per-bound/aggregate status,
        log, fire events, and record every transition on the run's
        timeline. Raises FatalBoundViolation if a fatal bound violated
        this frame, after publishing/logging/recording for every
        transition this frame (not just the fatal one).

        `seq`/`frame_t` are the frame's own identifiers, recorded on each
        Violation so the timeline can be aligned with - and replayed
        against - the stored telemetry. They default to unset for callers
        (tests) that only care about the pass/fail logic. Note debounce
        still uses wall-clock time here, not frame_t: see this module's
        docstring."""
        transitions = self._evaluator.evaluate(channels, time.time())
        fatal_transition = None

        with self._summary_lock:
            self._evaluated_frames += 1

        for transition in transitions:
            self._publisher.set_state(f"{transition.bound_label}_status", "FAIL" if transition.violated else "PASS")
            if transition.event_name and self._trigger_event is not None:
                self._trigger_event(transition.event_name)

            with self._summary_lock:
                self._violations.append(
                    Violation(
                        bound_label=transition.bound_label,
                        rulebook_name=transition.rulebook_name,
                        channel=transition.channel,
                        value=transition.value,
                        fatal=transition.fatal,
                        transition="violated" if transition.violated else "cleared",
                        seq=seq,
                        t=time.time() if frame_t is None else frame_t,
                    )
                )

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

    def summary(self) -> RunSummary:
        """The whole-run bound outcome for the test's verdict - the full
        transition timeline, whether any of it was fatal, and how many
        frames were actually evaluated. Meant to be called from the main
        thread after stop(); safe to call anytime (guarded by
        _summary_lock), returning the state accumulated so far. A runner
        that never started returns evaluated_frames=0, which the verdict
        records as NOT_EVALUATED rather than a pass - see RunSummary and
        TestCase.run()."""
        with self._summary_lock:
            violations = list(self._violations)
            evaluated_frames = self._evaluated_frames
            unevaluable = self._unevaluable
        any_fatal = any(v.fatal and v.transition == "violated" for v in violations)
        return RunSummary(
            violations=violations,
            any_fatal=any_fatal,
            evaluated_frames=evaluated_frames,
            unevaluable=unevaluable,
        )
