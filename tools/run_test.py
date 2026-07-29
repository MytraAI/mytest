"""Generic entry point for running any registered TestCase by name -
see testcases/registry.py for how a test gets registered and
REGISTERED_TESTS for what's available. Not a demo: runs the selected
test for real by default. --mock is forwarded to every factory
uniformly; a test that has no real/mock distinction (e.g. anything
under example_dut) simply ignores it.

Lives in tools/, not testcases/, alongside stop_test.py and
manual_gui.py - the operator-facing entry points, as opposed to
testcases/ itself, which holds the test framework and its per-DUT
content.

Run with (from the repo root):
    python -m telemetry_engine.main          # first, in its own terminal
    python -m tools.run_test --test ydrive.manual
    python -m tools.run_test --test ydrive.endurance_cycle --mock

The telemetry engine has to be running: a test refuses to start if nothing
is recording, and aborts if recording stops mid-run, since a run's whole
product is its record (see TestCase.check_recording_alive and
protocol/heartbeat.py). Both surface here as RecordingLost, reported as a
plain error line with exit code 1 rather than a traceback - it's a real
failure, but an operator-actionable one whose message says what to do.

A deliberate stop (the operator dashboard's "Stop test" button,
tools/stop_test.py, or a plain SIGTERM) surfaces here as
StopRequested/SystemExit - TestCase.run() re-raises it after a clean
teardown purely so a caller can tell a stop happened, not so this
script should treat it as a failure. Caught below and reported as a
plain log line with a clean exit, instead of the traceback and exit
code 1 an uncaught StopRequested/SystemExit would otherwise produce
for what was actually a successful, intentional stop.
"""
from __future__ import annotations

import argparse
import logging
import sys

from testcases.base import RecordingLost, StopRequested
from testcases.registry import REGISTERED_TESTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test", required=True, choices=sorted(REGISTERED_TESTS), help="registered test key, e.g. ydrive.manual"
    )
    parser.add_argument("--mock", action="store_true", help="run against a mock backend, for tests that support it")
    parser.add_argument("--test-id", default=None, help="override the auto-generated test_id")
    args = parser.parse_args()

    test_case = REGISTERED_TESTS[args.test](args.test_id, args.mock)
    logger.info("running %s (test_id=%s)", args.test, test_case.test_id)
    try:
        test_case.run()
    except (StopRequested, SystemExit) as exc:
        # A deliberate operator stop (Stop test button, tools/stop_test.py,
        # or SIGTERM) - TestCase.run() already re-raises this after a clean
        # teardown, purely so a caller knows a stop happened rather than a
        # normal completion. That's not a failure, so report it as a plain
        # message and a clean exit rather than a traceback and exit code 1.
        logger.info("test %s: stopped - %s", test_case.test_id, exc)
        sys.exit(0)
    except RecordingLost as exc:
        # Nothing is recording, so the run either never started or was
        # aborted mid-way (see TestCase.check_recording_alive). This is a
        # real failure - exit 1 - but it's an operator-actionable one with
        # a self-explanatory message, so a traceback adds only noise.
        logger.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
