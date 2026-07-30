"""Cross-platform, OS-signal-independent way to stop a running test
case - see TestCase.check_stop_requested() (testcases/base.py) and
AI/Mytest.md's OS compatibility section for why this exists: on
Windows, neither Popen.terminate() nor os.kill(pid, SIGTERM) reach a
process's own signal handling (both map to an unconditional
TerminateProcess()), so the SIGTERM-based stop TestCase.run() already
supports doesn't help there. This tool sidesteps OS signals entirely -
it just leaves a marker file the target test's own poll loop already
checks every ~10ms (see check_stop_requested()).

This cannot stop a test that's been killed via Task Manager, a bare
`taskkill /F`, or SIGKILL - those bypass all in-process code on any OS,
by design (the same way SIGKILL can't be caught on POSIX either). This
tool only helps if whoever wants to stop a test actually uses it,
instead of a raw kill - that's a real limitation to know about, not
something more code here can fix.

With no --test-id, discovers whichever test is currently running via
the run-state stream (there's only ever one - see protocol/wire.py's
DEFAULT_RUN_STATE_ENDPOINT) rather than requiring the operator to
already know its auto-generated test_id. That stream exists for the
whole life of a run and carries nothing but the run's identity and
state, so discovery is one small message rather than filtering a
telemetry firehose.

Lives in tools/, not testcases/, alongside run_test.py and
manual_gui.py - the operator-facing entry points, as opposed to
testcases/ itself, which holds the test framework and its per-DUT
content.

Run with (from the repo root, in a separate terminal from the running test):
    python -m tools.stop_test
    python -m tools.stop_test --test-id <test_id>
"""
from __future__ import annotations

import argparse
import logging
import tempfile
from pathlib import Path

import zmq

from protocol.wire import DEFAULT_RUN_STATE_ENDPOINT, RUN_STATE_TOPIC, RunStateFrame

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_DISCOVERY_TIMEOUT_S = 5.0


def discover_running_test_id(endpoint: str, timeout_s: float) -> str:
    """Blocks for one run-state frame and returns its test_id - there's
    only ever one test running at a time on this test stand, so whichever
    test_id shows up first is the one to stop."""
    ctx = zmq.Context.instance()
    socket = ctx.socket(zmq.SUB)
    socket.setsockopt(zmq.SUBSCRIBE, RUN_STATE_TOPIC)
    socket.setsockopt(zmq.RCVTIMEO, int(timeout_s * 1000))
    socket.connect(endpoint)
    try:
        _, raw = socket.recv_multipart()
    except zmq.error.Again as exc:
        raise RuntimeError(
            f"no run-state frame seen within {timeout_s:.1f}s on {endpoint} - "
            "is a test case actually running? pass --test-id explicitly if you already know it"
        ) from exc
    finally:
        socket.close(linger=0)
    return RunStateFrame.from_bytes(raw).test_id


def request_stop(test_id: str) -> Path:
    """Leaves the marker file TestCase.check_stop_requested() polls for
    - see that method's docstring for the exact path convention this
    must match exactly."""
    path = Path(tempfile.gettempdir()) / f"mytest-stop-{test_id}"
    path.touch()
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-id", default=None, help="stop this specific test_id instead of auto-discovering it")
    parser.add_argument(
        "--state-endpoint",
        default=DEFAULT_RUN_STATE_ENDPOINT,
        help="run-state endpoint to discover the running test_id from, if --test-id isn't given",
    )
    parser.add_argument("--discovery-timeout", type=float, default=DEFAULT_DISCOVERY_TIMEOUT_S)
    args = parser.parse_args()

    test_id = args.test_id
    if test_id is None:
        logger.info("no --test-id given - discovering the running test from %s", args.state_endpoint)
        test_id = discover_running_test_id(args.state_endpoint, args.discovery_timeout)

    path = request_stop(test_id)
    logger.info("stop requested for test %s (%s)", test_id, path)


if __name__ == "__main__":
    main()
