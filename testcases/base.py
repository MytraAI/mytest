"""Base class for test cases run by the testcase execution process.

Each test case follows a three-phase lifecycle: PreTestSetup,
MainExecution, PostTestTeardown. This is deliberately narrower than the
generic idle/arm/run/abort/complete state machine sketched in the
architecture doc - this component uses the three-phase model
exclusively.

- PreTestSetup connects to the hardware driver, subscribes to telemetry, and
  wires up rule evaluation. run() has already announced the run on the state
  stream by this point, so the telemetry engine knows where this run's frames
  belong before any driver exists.
- MainExecution runs the test's own sequence logic. Any pass/fail decision
  that affects the live sequence is made here, from the test's own
  telemetry subscription: no *evaluation result* computed downstream of the
  state publisher can influence this process. (The engine's liveness
  heartbeat is not such a result - see check_recording_alive.)
- PostTestTeardown returns the system to a state where another test can
  start.

PostTestTeardown always runs, however the run ended. Since PreTestSetup can
fail partway through several devices, it must check what was actually set up
before tearing it down. Use `teardown_step()` for each cleanup action, so one
device's failure can't prevent another's or mask the exception already
propagating out of `run()`.

`self.runner` is an Optional[LiveRulebookRunner] each concrete subclass
constructs in its own pre_test_setup(). It lives here, not on each subclass,
only so wait_for() can rely on the attribute existing. TestCase itself never
constructs one.
"""
from __future__ import annotations

import logging
import signal
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from protocol import heartbeat
from protocol.paths import DEFAULT_OUTPUT_DIR, new_test_id

HEARTBEAT_POLL_INTERVAL_S = 0.5
"""How often check_recording_alive() actually opens the heartbeat file. Polled far
more often than that, but the answer cannot change meaningfully faster: the
staleness deadline is ten seconds, and a hundred opens a second is what made the
engine's atomic replace collide with it on Windows."""
from protocol.verdict import BoundsResult, Lifecycle, Verdict, write_verdict

from asimov.live_rulebook_runner import FatalBoundViolation, LiveRulebookRunner
from .state_publisher import RunStatePublisher
from .utils import Stopwatch, spawn_operator_dashboard

logger = logging.getLogger(__name__)


class RecordingLost(Exception):
    """Raised by TestCase.check_recording_alive() when the telemetry
    engine's heartbeat is missing or stale - nothing is recording this
    run any more.

    Not a safety failure - the runner keeps evaluating from its own
    subscription, so bound monitoring is unaffected - but an economic one: a
    run's whole product is its record, so continuing spends hardware wear
    producing nothing recoverable. Handled by run()'s try/finally exactly
    like StopRequested."""

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


class DeviceNotRecorded(Exception):
    """Raised before a run starts when it declares a device the telemetry
    engine isn't subscribed to, so nothing would record that device's frames.

    The same principle as declared channels having to exist: a test naming a
    device it expects data from must fail loudly at setup, not produce a run
    directory that is quietly missing one. The engine advertises its
    subscriptions on the heartbeat - see protocol/heartbeat.py."""

    def __init__(self, test_id: str, missing: Sequence[str], recorded: Sequence[str]):
        super().__init__(
            f"test {test_id}: declares device(s) the telemetry engine is not recording: "
            f"{sorted(missing)} - the engine is subscribed to {sorted(recorded)}"
        )
        self.test_id = test_id
        self.missing = sorted(missing)


class TestCase(ABC):
    """Abstract three-phase test case: PreTestSetup, MainExecution, PostTestTeardown."""

    DUT: str = ""
    """Which DUT package under testcases/ this test belongs to - "zdrive",
    "ydrive", "example_dut".

    Declared by each DUT's own base test case and inherited by every test on
    it, including subclasses defined outside the package (a test module's
    one-off subclass of a real test still reports the DUT it came from).
    Deliberately not derived from __module__ at runtime, which would report
    the module a subclass happens to be written in.

    Recorded in the verdict, so a stored run says which stand produced it
    without that having to be inferred from the test's name. The value is the
    package's own directory name, and the same string the registry keys tests
    by ("<dut>.<test>"); tests/test_dut_identifier.py holds those three in
    agreement."""

    DEVICES: Tuple[str, ...] = ()
    """Which devices this test claims, as protocol/wire.py DEVICE_* names.

    Assembled by a concrete base test case from the declarations of the things
    that actually own the driver processes - its testbed's DEVICES and, when
    the DUT has its own electronics, its DUT façade's DEVICES. Neither of those
    two learns about the other, which is what keeps the testbed/DUT split
    intact (see AI/mytest-vs-forge.md §7).

    Two jobs: the telemetry engine records frames from these devices into this
    run's directory and leaves every other device in the continuous per-session
    record, and require_recording_started() refuses to start if the engine
    isn't subscribed to one of them. A class attribute rather than a property
    because it must be knowable before anything is instantiated - the run is
    announced before PreTestSetup."""

    def __init__(self, test_id: Optional[str] = None, require_engine: bool = True):
        self.test_id = test_id or new_test_id(getattr(self, "TEST_NAME", "unknown"))
        """Identifies this run everywhere: on the state stream, in the stop
        file, and as the name of the directory holding its output. Generated
        from this test's name and the current time unless the caller supplies
        one (see protocol/paths.py)."""
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
        self._publisher = RunStatePublisher(
            test_id=self.test_id,
            test_name=getattr(self, "TEST_NAME", "unknown"),
            devices=self.DEVICES,
        )
        """This run's state publisher.

        Constructed here and *started* by run(), deliberately in two steps.
        Constructing it opens no socket and starts no thread - it only holds the
        state dict - so pre_test_setup() can seed state and hand it to the
        evaluator without depending on run() having been called first. Building
        it in run() instead made that ordering load-bearing, and set_state()
        silently did nothing when it wasn't met, which is the failure mode this
        codebase refuses everywhere else.

        A bonus of the split: state set before run() starts publishing is
        carried on the very first frame rather than missed."""
        self._heartbeat_seen_at = time.time()
        """When a fresh engine heartbeat was last actually read. What
        check_recording_alive() measures against, so a single unreadable read -
        which on Windows is just a collision with the engine writing it - costs
        nothing."""
        self._heartbeat_checked_at = 0.0
        """When the heartbeat file was last opened at all, so the check can be
        polled at every tick while only reading occasionally."""
        self._tearing_down = False
        """True once post_test_teardown() starts, which switches off
        check_recording_alive(). Teardown steps go through @step like any
        other, so without this a dead engine would abort every individual
        teardown step - leaving hardware in exactly the state teardown exists
        to return it from. By then the verdict is already written, so
        recording liveness has nothing left to protect."""

    @abstractmethod
    def pre_test_setup(self) -> None:
        """Connect to the hardware driver, subscribe to telemetry, wire up
        rule evaluation, and start acquisition. The run is already announced
        on the state stream by the time this is called."""

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

        Refuses to start unless something is recording (see
        require_recording_started), then authors this run's verdict before
        teardown - everything it records is already determined by then, and
        result_metadata() can still read live hardware.

        Converts SIGTERM into the clean-teardown path SIGINT gets for free:
        Python installs no SIGTERM handler, so `kill <pid>` would otherwise
        skip post_test_teardown() entirely and abandon hardware wherever it
        was. Registered only on the main thread, since signal.signal() raises
        elsewhere and this codebase does call run() off the main thread (the
        demos, via asyncio.to_thread); silently skipped there, which costs
        nothing because those runs aren't unattended or physically moving.

        Spawns the operator status page before pre_test_setup(), so it's up to
        show an error even if setup crashes, and reflects how this method's
        try/except resolves: "stopped" for a deliberate stop, "failing" with
        the exception otherwise, "passing" on a clean completion whose bounds
        also passed.

        On the main thread a completed run then blocks
        (_wait_until_interrupted) so that final result stays on screen until
        the operator closes it. Deliberately skipped for a stop or a lost
        recorder - the operator already acted, or nobody is watching - and off
        the main thread, so the demos return promptly.
        """
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self._handle_sigterm)

        self.require_recording_started()
        self._resolve_output_dir()

        # Announce the run before anything else starts. The engine attributes a
        # device's frames to this run only while this stream is live, so
        # publishing first means no frame can be produced before the engine
        # knows where it belongs.
        # Before the thread starts publishing, so the first frame already carries them
        # and a run cannot open with its derived channels absent.
        self._publisher.set_derivation(self.derived_channels, self.DERIVED_FROM_DEVICES)
        self._publisher.start()

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
            # Stop announcing only once teardown is done: the state stream going
            # quiet is what tells the engine this run is over, and frames
            # produced while teardown is still safing hardware belong to it.
            self.teardown_step("stop state publisher", self._publisher.stop)
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
                dut=self.DUT,
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
        asimov/live_rulebook_runner.py's docstring for the
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

    def operator_ack_path(self) -> Path:
        """Where an operator's "done" for this run lands -
        Path(tempfile.gettempdir())/mytest-ack-<test_id>.

        The same convention as the stop marker above and for the same reasons: a
        file, so it needs no OS signal and works identically on every target
        platform, and polled by the test itself so the wait still checks for a
        fatal bound, a stop request and a lost recorder on every tick. A step
        that blocks on input() would stop all three during the one part of a test
        where somebody has their hands on the hardware.

        Public because tools/operator_ack.py has to compose the same path, the
        way tools/stop_test.py does for the stop marker. Only the convention
        lives here: what a run asks an operator to do, and what it does with the
        answer, is a test step's business - see
        testcases/ydrive/teststeps/teststeps.py's await_operator()."""
        return Path(tempfile.gettempdir()) / f"mytest-ack-{self.test_id}"

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
        lost is the run's product. See RecordingLost.

        THE QUESTION IS "HAS ANYTHING ADVERTISED RECORDING RECENTLY", NOT "DID
        THIS READ SUCCEED". One unreadable read is not an answer: on Windows the
        engine's atomic replace of the heartbeat and this read collide - the
        engine swaps the file once a second, this is polled every 10 ms - and
        read_heartbeat() reports absent for anything it cannot parse or open. A
        run died that way after one cycle, with the engine alive and recording
        throughout. So a failed read is remembered rather than raised on, and the
        run stops only once nothing has been seen for as long as a stale
        heartbeat would be tolerated anyway.

        The read is also rate-limited: at 10 ms it was doing a hundred file opens
        a second to answer a question whose deadline is ten seconds, which is
        what made the collision likely in the first place."""
        if not self.require_engine or self._tearing_down:
            return
        now = time.time()
        if now - self._heartbeat_checked_at < HEARTBEAT_POLL_INTERVAL_S:
            return
        self._heartbeat_checked_at = now

        beat = heartbeat.read_heartbeat()
        if beat is not None and beat.is_fresh():
            self._heartbeat_seen_at = now
            return

        unseen_s = now - self._heartbeat_seen_at
        if unseen_s < heartbeat.DEFAULT_STALE_AFTER_S:
            logger.debug(
                "test %s: no readable engine heartbeat for %.1fs, still inside the %.0fs window",
                self.test_id, unseen_s, heartbeat.DEFAULT_STALE_AFTER_S,
            )
            return
        if beat is None:
            raise RecordingLost(
                self.test_id,
                f"no readable engine heartbeat for {unseen_s:.0f}s",
            )
        raise RecordingLost(self.test_id, f"engine heartbeat is {beat.age_s():.0f}s stale")

    def require_recording_started(self) -> None:
        """Refuse to start if nothing is recording, or if anything this run
        declares isn't being recorded. Called by run() before
        pre_test_setup(), so a run that would produce no record - or an
        incomplete one - fails immediately instead of moving hardware for
        nothing."""
        if not self.require_engine:
            return
        beat = heartbeat.read_heartbeat()
        if beat is None or not beat.is_fresh():
            raise RecordingLost(
                self.test_id,
                "no telemetry engine is running (start it with `python -m telemetry_engine.main`, "
                "or pass require_engine=False to run without a record)",
            )
        missing = [device for device in self.DEVICES if device not in beat.devices]
        if missing:
            raise DeviceNotRecorded(self.test_id, missing, beat.devices)
        logger.info(
            "test %s: telemetry engine recording to %s (pid %s), covering %s",
            self.test_id, beat.output_dir, beat.pid, ", ".join(self.DEVICES) or "(no declared devices)",
        )

    def check_should_continue(self) -> None:
        """Raise if this run should stop: a fatal Rulebook violation, an operator
        stop request, or the telemetry engine having stopped recording.

        The three checks always belong together, so any loop that blocks - a
        paced wait, a step boundary, a test step polling live readings - calls
        this rather than picking a subset. Picking a subset is how a loop ends up
        honouring a fatal bound while ignoring an operator's stop for its whole
        duration."""
        self.check_fatal_violation()
        self.check_stop_requested()
        self.check_recording_alive()

    def wait_for(self, duration_s: float) -> None:
        """Paced wait for duration_s, calling check_should_continue() each tick
        instead of blocking the full duration regardless of it. Use this instead
        of iterating a Stopwatch directly for a plain wait with no other
        condition to check."""
        for _ in Stopwatch(duration_s=duration_s):
            self.check_should_continue()

    def set_state(self, name: str, value: Any) -> None:
        """Publish a named state value - a step name, a rule's status, a
        derived quantity - onto this run's state stream from now on.

        This is the one sanctioned way for test steps and the @step decorator
        to record test-case state: callers never see the publisher. The engine
        merges these into every row it writes for this run's devices, so they
        land in the recorded telemetry alongside real hardware channels, and
        the live evaluator can gate a Bound on them (see
        RunStatePublisher.state_snapshot)."""
        self._publisher.set_state(name, value)

    DERIVED_FROM_DEVICES: Tuple[str, ...] = ()
    """Devices whose frames derived_channels() reads.

    Declared so a missing stream fails at runner.start() instead of in the data. A
    derivation whose device never arrives publishes nothing, and the channel then holds
    the value its channel list seeded it with - present in the recording, numeric, and
    wrong, which is worse than absent."""

    def state_snapshot(self) -> Dict[str, Any]:
        """Everything this run has published, pushed or derived, as of now.

        For a test reading back its own derived channels rather than recomputing them, so
        the value it decides on is the value that was recorded."""
        return self._publisher.state_snapshot()

    def derived_channels(self, latest: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Channels this test computes from telemetry rather than pushes from a code path.

        `latest` is the newest channels from each device, keyed by device name:
        latest["odrive"]["turns_traveled"]. Evaluated on every state tick, so what it
        returns is sampled at the stream's rate instead of latched wherever some code path
        last remembered to call set_state() - which for a live quantity is the difference
        between a measurement and a staircase.

        Runs on the publisher thread. Cheap, no sockets, and no motion: this is reporting.
        A device that has sent nothing yet is simply absent from `latest`."""
        return {}

    def teardown_step(self, description: str, action: Callable[[], None]) -> None:
        """Run one teardown action, logging (not raising) on failure so
        the remaining teardown steps still get attempted. Any
        post_test_teardown() override - at any subclass depth - should
        use this for each cleanup action, not just a base case's own."""
        try:
            action()
        except Exception:
            logger.exception("test %s: teardown step failed: %s", self.test_id, description)
