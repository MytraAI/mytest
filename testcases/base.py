"""Base class for test cases run by the testcase execution process.

Each test case follows a three-phase lifecycle: PreTestSetup,
MainExecution, PostTestTeardown. This is deliberately narrower than the
generic idle/arm/run/abort/complete state machine sketched in the
architecture doc - this component uses the three-phase model
exclusively.

- PreTestSetup connects to the hardware driver, subscribes to
  telemetry, and starts the Telemetry Publisher.
- MainExecution runs the test's own sequence logic. Any pass/fail
  decision that needs to affect the live sequence must be made here,
  using the test case's own telemetry subscription directly. Per the
  architecture doc's "no feedback loop" principle, nothing downstream
  of the Telemetry Publisher can influence this process.
- PostTestTeardown returns the system to a state where another test
  can start.

PostTestTeardown always runs, unconditionally, regardless of how
PreTestSetup or MainExecution ended: normal completion, a fatal
channel breach, or an unexpected exception anywhere. A test case that
sets up more than one device in PreTestSetup can fail partway through
(e.g. the second device's connection fails after the first device
already started acquiring). So PostTestTeardown must be defensive:
check what was actually set up before tearing it down, rather than
assuming every resource it might reference exists.

A teardown step against a device that was never reachable can itself
fail (e.g. a ZeroMQ REQ socket left in an unmatched send/recv state
after a connect timeout). Use `teardown_step()` to run each cleanup
action independently, so one device's cleanup failure can't prevent
another device's cleanup from being attempted, and can't mask whatever
exception is already propagating out of `run()`.

`self.runner` is an Optional[LiveRulebookRunner] every concrete
subclass is expected to construct in its own pre_test_setup() (see
BaseExampleDutTest/BaseYdriveTest) - kept here, not down on each
subclass, purely so wait_for() below can rely on it existing (as None
or a real runner) without every subclass redeclaring the same
attribute. TestCase itself never constructs one.
"""
from __future__ import annotations

import logging
import signal
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from protocol import heartbeat
from protocol.paths import DEFAULT_OUTPUT_DIR
from protocol.verdict import BoundsResult, Lifecycle, Verdict, write_verdict

from .asimov.live_rulebook_runner import FatalBoundViolation, LiveRulebookRunner
from .utils import Stopwatch, spawn_operator_dashboard

logger = logging.getLogger(__name__)


class RecordingLost(Exception):
    """Raised by TestCase.check_recording_alive() when the telemetry
    engine's heartbeat is missing or stale - nothing is recording this
    run any more.

    Not a safety failure: the LiveRulebookRunner keeps evaluating from its
    own subscription, so bound monitoring is unaffected. It's an economic
    one. A test's whole product is its record, and both real-hardware test
    cases here run indefinitely, so continuing would spend hours of real
    mechanical wear producing nothing recoverable. Handled by run()'s
    try/finally exactly like StopRequested."""

    def __init__(self, test_id: str, detail: str):
        super().__init__(f"test {test_id}: telemetry recording lost - {detail}")
        self.test_id = test_id


class StopRequested(Exception):
    """Raised by TestCase.check_stop_requested() when tools/stop_test.py
    has left a marker file requesting this test stop - a deliberate
    operator action, not a safety violation, but propagated through
    run()'s try/finally the same way FatalBoundViolation is.

    Exists because SIGTERM/Popen.terminate() don't reliably reach a
    process's own signal handling on Windows (see AI/Mytest.md's OS
    compatibility section) - this sidesteps OS signals entirely via a
    marker file the test's own poll loop already checks."""

    def __init__(self, test_id: str):
        super().__init__(f"test {test_id}: stop requested")
        self.test_id = test_id


class TestCase(ABC):
    """Abstract three-phase test case: PreTestSetup, MainExecution, PostTestTeardown."""

    def __init__(self, test_id: Optional[str] = None, require_engine: bool = True):
        self.test_id = test_id or uuid.uuid4().hex
        self.runner: Optional[LiveRulebookRunner] = None
        self.require_engine = require_engine
        """Whether this run needs the telemetry engine to be recording:
        refuse to start without it, and abort if it stops mid-run (see
        check_recording_alive()). True for any real run - a test that
        records nothing has no product. Set False only by callers that
        deliberately run with no engine and don't want a record: the demo
        scripts, which exercise plumbing with their own in-process
        aggregator, and unit tests."""
        self._output_dir: Path = DEFAULT_OUTPUT_DIR
        """Where this run's directory goes. Overwritten at run() start
        with the engine's own output dir, read from its heartbeat, so the
        two processes can't disagree even if the engine was started with
        --output-dir."""
        self._tearing_down = False
        """True once post_test_teardown() starts, which switches off
        check_recording_alive(). Teardown steps go through @step like any
        other, so without this a dead engine would abort every individual
        teardown step - leaving the axis un-idled, which is precisely the
        state teardown exists to prevent. By then the verdict is already
        written, so recording liveness has nothing left to protect."""

    @abstractmethod
    def pre_test_setup(self) -> None:
        """Connect to the hardware driver, subscribe to telemetry, start
        the Telemetry Publisher, and start acquisition."""

    @abstractmethod
    def main_execution(self) -> None:
        """Run the test's own sequence logic."""

    @abstractmethod
    def post_test_teardown(self) -> None:
        """Return the system to a state where another test can start.

        Must be defensive - PreTestSetup may have failed partway
        through, so only tear down what was actually set up."""

    def run(self) -> None:
        """Run pre_test_setup -> main_execution, then always post_test_teardown.

        Also converts SIGTERM into the same clean-teardown path Ctrl+C
        already gets for free. Python installs a default handler that
        turns SIGINT into KeyboardInterrupt, which the try/finally below
        already catches correctly - but Python installs no equivalent
        handler for SIGTERM, so `kill <pid>` (the default signal an
        external supervisor/systemd/CI would send to stop an unattended
        run) would otherwise terminate the process immediately,
        skipping post_test_teardown() entirely. For a test case that
        runs until told to stop rather than for a fixed duration (e.g.
        EnduranceCycleTest, ManualTest), that means real hardware gets
        abandoned exactly where it was - the axis never idled, the
        driver process orphaned instead of disconnected - discovered by
        actually sending a real test process SIGTERM and watching
        neither happen.

        Only registered when run() is executing on the main thread:
        signal.signal() raises ValueError from any other thread, and
        this codebase already calls TestCase.run() off the main thread
        in three places (telemetry_engine/demo_*_run.py, via
        asyncio.to_thread(), so a synchronous run() doesn't block those
        demos' own event loop) - see live_rulebook_runner.py's docstring
        for the same main-thread constraint on the fatal-violation
        watchdog. Silently skipped rather than raising there: those
        demos exercise a no-op main_execution() for a few seconds, not
        an unattended, physically-moving run - the scenario this
        actually protects against - so nothing is lost by not
        installing it in that context.

        Also spawns the lightweight operator status page (see
        testcases/utils.py's spawn_operator_dashboard and
        tools/operator_dashboard.py) before pre_test_setup() even
        starts, so it's up to show an error even if setup itself
        crashes. Its status reflects how this method's own try/except
        resolves: "stopped" for a deliberate StopRequested/SystemExit
        (an operator asking to stop is not a failure), "failing" with
        the exception for anything else, "passing" on clean completion.

        On the main thread, once the test is over, this blocks
        (_wait_until_interrupted()) so the dashboard keeps showing that
        final result instead of vanishing the instant the test
        completes - the operator closes it out themselves (Ctrl+C) once
        they've seen it. Skipped when run() executes off the main
        thread (the same three telemetry_engine/demo_*_run.py callers
        the SIGTERM registration above already skips), so those demos
        keep returning promptly rather than hanging forever. Also
        skipped for a deliberate StopRequested/SystemExit - the operator
        already took one action to end the test; making them take a
        second one (another Ctrl+C/SIGTERM/stop_test.py) just to let the
        process actually exit would be a real, confirmed bug, not a
        feature - only "passing"/"failing" (the test reached its own
        conclusion, not an external stop) linger.
        """
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self._handle_sigterm)

        self.require_recording_started()
        self._resolve_output_dir()

        dashboard = spawn_operator_dashboard(self.test_id, getattr(self, "TEST_NAME", "unknown"))
        linger = False
        started_at = time.time()
        lifecycle = Lifecycle.ERRORED  # fallback; every branch below sets the real value
        reason = ""
        exc_was_fatal = False
        verdict: Optional[Verdict] = None

        try:
            try:
                logger.info("test %s: pre_test_setup", self.test_id)
                self.pre_test_setup()
                logger.info("test %s: main_execution", self.test_id)
                self.main_execution()
            except (StopRequested, SystemExit, KeyboardInterrupt) as exc:
                lifecycle, reason = Lifecycle.STOPPED, str(exc)
                if dashboard is not None:
                    dashboard.set_status("stopped")
                    dashboard.set_error(str(exc))
                raise
            except RecordingLost as exc:
                # An error condition, so lifecycle is ERRORED - but
                # deliberately does not linger. Lingering exists so an
                # operator can read the final result off the status page;
                # recording dying mid-run is an unattended-infrastructure
                # failure, and the whole reason to abort was to stop
                # consuming hardware, which a process hanging forever on a
                # Ctrl+C nobody is there to press defeats.
                lifecycle, reason = Lifecycle.ERRORED, str(exc)
                if dashboard is not None:
                    dashboard.set_status("failing")
                    dashboard.set_error(str(exc))
                raise
            except FatalBoundViolation as exc:
                # An exception did propagate out of main_execution, so the
                # lifecycle is ERRORED; that a *bound* caused it is recorded
                # by bounds_result/any_fatal, not by overloading lifecycle.
                lifecycle, reason, exc_was_fatal = Lifecycle.ERRORED, str(exc), True
                if dashboard is not None:
                    dashboard.set_status("failing")
                    dashboard.set_error(repr(exc))
                linger = True
                raise
            except BaseException as exc:
                lifecycle, reason = Lifecycle.ERRORED, repr(exc)
                if dashboard is not None:
                    dashboard.set_status("failing")
                    dashboard.set_error(repr(exc))
                linger = True
                raise
            else:
                lifecycle = Lifecycle.COMPLETED
                if dashboard is not None:
                    dashboard.set_status("passing")
                linger = True
            finally:
                # Author the verdict here, *before* teardown: everything it
                # records is already determined, the record then exists
                # before any teardown delay (so the engine can't mistake a
                # slow teardown for a crashed test), and result_metadata()
                # can still read live hardware that teardown is about to
                # stop.
                verdict = self._author_verdict(started_at, lifecycle, reason, exc_was_fatal)
        finally:
            logger.info("test %s: post_test_teardown", self.test_id)
            self._tearing_down = True  # teardown must run to completion even with no recorder
            self.post_test_teardown()
            if dashboard is not None:
                if verdict is not None and verdict.bounds_result == BoundsResult.FAIL:
                    # A bound violated at some point, so the run failed even if
                    # it completed cleanly - make the dashboard's final state
                    # match the verdict rather than the momentary "passing".
                    dashboard.set_status("failing")
                if linger and threading.current_thread() is threading.main_thread():
                    logger.info(
                        "test %s: complete - status page at %s (Ctrl+C to exit)", self.test_id, dashboard.url
                    )
                    self._wait_until_interrupted()
                dashboard.stop()

    def result_metadata(self) -> Dict[str, Any]:
        """Freeform key/values attached to this run's verdict - tuning
        profile, setpoints, DUT serial, operator, git SHA, whatever
        context explains 'under what conditions did this run happen'.
        Empty by default; a concrete test case overrides it to record its
        own run configuration. Called once per run, from run()'s verdict
        authoring - which happens *before* post_test_teardown(), so an
        override can still read live hardware here."""
        return {}

    def _resolve_output_dir(self) -> None:
        """Point this run at the engine's own output directory, read from
        its heartbeat, so both processes write into the same tree without
        a shared config or a flag that could disagree.

        Skipped entirely when require_engine is False: such a caller has
        opted out of engine-mediated recording and is managing its own
        output location (the demos set _output_dir themselves and act as
        their own recorder), so silently redirecting them at whatever
        engine happens to be running on the machine would send their
        verdict somewhere they aren't looking."""
        if not self.require_engine:
            return
        beat = heartbeat.read_heartbeat()
        if beat is not None:
            self._output_dir = Path(beat.output_dir)

    def _author_verdict(
        self, started_at: float, lifecycle: str, reason: str, exc_was_fatal: bool
    ) -> Optional[Verdict]:
        """Assemble this run's verdict from how run() resolved plus the
        LiveRulebookRunner's bound summary, and write it into this run's
        directory (see protocol/verdict.py).

        `lifecycle` is how the run ended; `bounds_result` comes from the
        runner independently, since they're orthogonal - a stopped run with
        no violations is the *expected success* for both real-hardware test
        cases, and a completed run that violated a bound still failed.

        Best-effort: logs rather than raises, so a write failure can't mask
        the test's own outcome propagating out of run(). Returns the Verdict
        (or None on failure) so run()'s finally can align the dashboard's
        final state with it."""
        try:
            summary = self.runner.summary() if self.runner is not None else None
            violations = summary.violations if summary is not None else []
            bounds_result = summary.bounds_result if summary is not None else BoundsResult.NOT_EVALUATED
            any_fatal = (summary.any_fatal if summary is not None else False) or exc_was_fatal
            verdict = Verdict(
                test_id=self.test_id,
                test_name=getattr(self, "TEST_NAME", "unknown"),
                lifecycle=lifecycle,
                bounds_result=bounds_result,
                started_at=started_at,
                ended_at=time.time(),
                reason=reason,
                any_fatal=any_fatal,
                violations=violations,
                metadata=self.result_metadata(),
            )
            path = write_verdict(verdict, self._output_dir)
            logger.info("test %s: verdict %s written to %s", self.test_id, verdict.outcome, path)
            return verdict
        except Exception:
            logger.exception("test %s: failed to author verdict", self.test_id)
            return None

    def _handle_sigterm(self, signum: int, frame: object) -> None:
        """Registered by run() on the main thread only - raises so
        run()'s try/finally runs post_test_teardown() instead of
        SIGTERM's OS-default immediate termination."""
        raise SystemExit(f"test {self.test_id}: stopped by SIGTERM")

    def _wait_until_interrupted(self) -> None:
        """Blocks (Ctrl+C/SIGTERM, or tools/stop_test.py again, to
        break out) so the operator status page keeps showing the
        test's final result rather than disappearing the moment the
        test completes - see run()'s docstring for why this only ever
        runs on the main thread."""
        try:
            while True:
                time.sleep(1.0)
                self.check_stop_requested()
        except (KeyboardInterrupt, SystemExit, StopRequested):
            pass

    def check_fatal_violation(self) -> None:
        """Raise self.runner's fatal_violation if a fatal Rulebook bound
        has violated - a no-op otherwise (including if self.runner is
        None or never started). Called from wait_for() below (once per
        tick) and from @step's entry/exit (see step.py) - see
        testcases/asimov/live_rulebook_runner.py's docstring for the
        polling model this is part of, and its known gap."""
        if self.runner is not None and self.runner.fatal_violation is not None:
            raise self.runner.fatal_violation

    def _stop_request_path(self) -> Path:
        """Where tools/stop_test.py leaves a marker file to request
        this test stop - Path(tempfile.gettempdir())/mytest-stop-<test_id>,
        resolving identically on Windows/CentOS/macOS since it's just
        Path.exists()/.touch()/.unlink(), no OS-specific code at all.
        See check_stop_requested() and stop_test.py's own docstring."""
        return Path(tempfile.gettempdir()) / f"mytest-stop-{self.test_id}"

    def check_stop_requested(self) -> None:
        """Raise StopRequested if tools/stop_test.py has left a
        marker file for this test_id - a no-op otherwise. Called from
        wait_for() (every tick) and from @step's entry/exit (see
        step.py) - the same call sites check_fatal_violation() already
        uses, so an external stop request is noticed with the same
        promptness a fatal violation already gets, with no new polling
        wired into any test step. Deletes the marker file the moment
        it's seen, before raising, so a stale file can't immediately
        re-trigger a future run that happens to reuse the same test_id."""
        path = self._stop_request_path()
        if path.exists():
            path.unlink(missing_ok=True)
            raise StopRequested(self.test_id)

    def check_recording_alive(self) -> None:
        """Raise RecordingLost if the telemetry engine isn't recording any
        more - a no-op when require_engine is False.

        Polled at the same call sites check_fatal_violation() and
        check_stop_requested() already use, so recording loss is noticed
        just as promptly with no new wiring in any test step - and it
        inherits the same known gap: a long step that never polls won't
        notice until it returns (see live_rulebook_runner.py's docstring).

        Deliberately *not* the same rule as a silent telemetry stream,
        which is fatal because it means losing live safety monitoring while
        hardware may still be moving. Monitoring is unaffected here; what's
        lost is the run's product. See RecordingLost."""
        if not self.require_engine or self._tearing_down:
            return
        beat = heartbeat.read_heartbeat()
        if beat is None:
            raise RecordingLost(self.test_id, "the telemetry engine's heartbeat is gone")
        if not beat.is_fresh():
            raise RecordingLost(self.test_id, f"engine heartbeat is {beat.age_s():.0f}s stale")

    def require_recording_started(self) -> None:
        """Refuse to start if nothing is recording. Called by run() before
        pre_test_setup(), so a run that would produce no record fails
        immediately instead of moving hardware for nothing."""
        if not self.require_engine:
            return
        beat = heartbeat.read_heartbeat()
        if beat is None or not beat.is_fresh():
            raise RecordingLost(
                self.test_id,
                "no telemetry engine is running (start it with `python -m telemetry_engine.main`, "
                "or pass require_engine=False to run without a record)",
            )
        logger.info("test %s: telemetry engine recording to %s (pid %s)", self.test_id, beat.output_dir, beat.pid)

    def wait_for(self, duration_s: float) -> None:
        """Paced wait for duration_s, calling check_fatal_violation(),
        check_stop_requested() and check_recording_alive() each tick
        instead of blocking the full duration regardless of any of them.
        Use this instead of iterating a Stopwatch directly for a plain
        wait with no other condition to check."""
        for _ in Stopwatch(duration_s=duration_s):
            self.check_fatal_violation()
            self.check_stop_requested()
            self.check_recording_alive()

    def teardown_step(self, description: str, action: Callable[[], None]) -> None:
        """Run one teardown action, logging (not raising) on failure so
        the remaining teardown steps still get attempted. Any
        post_test_teardown() override - at any subclass depth - should
        use this for each cleanup action, not just BaseYdriveTest/
        BaseExampleDutTest's own."""
        try:
            action()
        except Exception:
            logger.exception("test %s: teardown step failed: %s", self.test_id, description)
