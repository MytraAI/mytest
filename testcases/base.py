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
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

from .asimov.live_rulebook_runner import LiveRulebookRunner
from .stopwatch import Stopwatch

logger = logging.getLogger(__name__)


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

    def __init__(self, test_id: Optional[str] = None):
        self.test_id = test_id or uuid.uuid4().hex
        self.runner: Optional[LiveRulebookRunner] = None

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
        """
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self._handle_sigterm)

        try:
            logger.info("test %s: pre_test_setup", self.test_id)
            self.pre_test_setup()
            logger.info("test %s: main_execution", self.test_id)
            self.main_execution()
        finally:
            logger.info("test %s: post_test_teardown", self.test_id)
            self.post_test_teardown()

    def _handle_sigterm(self, signum: int, frame: object) -> None:
        """Registered by run() on the main thread only - raises so
        run()'s try/finally runs post_test_teardown() instead of
        SIGTERM's OS-default immediate termination."""
        raise SystemExit(f"test {self.test_id}: stopped by SIGTERM")

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

    def wait_for(self, duration_s: float) -> None:
        """Paced wait for duration_s, calling check_fatal_violation()
        and check_stop_requested() each tick instead of blocking the
        full duration regardless of either. Use this instead of
        iterating a Stopwatch directly for a plain wait with no other
        condition to check."""
        for _ in Stopwatch(duration_s=duration_s):
            self.check_fatal_violation()
            self.check_stop_requested()

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
