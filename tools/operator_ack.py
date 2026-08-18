"""Tell a waiting test that the operator has done what it asked.

Some steps stop and wait for a person - moving a load by hand, changing a
fixture. `await_operator()` publishes what it wants as `operator_prompt` and then
polls for the marker file this leaves, so the test keeps checking for a fatal
bound, a stop request and a lost recorder while it waits. That is the whole
reason for a file rather than input(): a blocking read would suspend all three
during the one part of a run where somebody has their hands on the hardware.

    python -m tools.operator_ack
    python -m tools.operator_ack --test-id <test_id>

With no --test-id it discovers the running test from the run-state stream, the
same way tools/stop_test.py does - and prints what the test is waiting for
before acknowledging it, so an operator can see they are answering the prompt
they think they are.

Mirrors TestCase.operator_ack_path()'s convention; that method's docstring is
where the path is defined.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional

import zmq

from protocol.wire import DEFAULT_RUN_STATE_ENDPOINT, RUN_STATE_TOPIC, RunStateFrame

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_DISCOVERY_TIMEOUT_S = 5.0


def discover_running_test(endpoint: str, timeout_s: float) -> RunStateFrame:
    """The currently running test's state frame - its id, and what it is waiting
    for. One frame is enough: only one test runs at a time on a stand."""
    ctx = zmq.Context.instance()
    socket = ctx.socket(zmq.SUB)
    socket.setsockopt(zmq.SUBSCRIBE, RUN_STATE_TOPIC)
    socket.setsockopt(zmq.RCVTIMEO, int(timeout_s * 1000))
    socket.connect(endpoint)
    try:
        _, raw = socket.recv_multipart()
    except zmq.error.Again as exc:
        raise RuntimeError(
            f"no run-state frame seen within {timeout_s:.1f}s on {endpoint} - is a test "
            "actually running? pass --test-id if you already know it"
        ) from exc
    finally:
        socket.close(linger=0)
    return RunStateFrame.from_bytes(raw)


def acknowledge(test_id: str, answers: Optional[Dict[str, str]] = None) -> Path:
    """Leave the marker await_operator() polls for - see
    TestCase.operator_ack_path() for the convention this must match.

    `answers` rides in the file as JSON when a prompt asked for values. An empty
    file is a plain acknowledgement, which is why the waiting step treats an
    unparseable or empty file as "yes" rather than as an error: the two prompts
    share one marker, and only one of them has anything to say."""
    path = Path(tempfile.gettempdir()) / f"mytest-ack-{test_id}"
    path.write_text(json.dumps(answers) if answers else "")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-id", default=None, help="acknowledge this test instead of discovering it")
    parser.add_argument("--state-endpoint", default=DEFAULT_RUN_STATE_ENDPOINT)
    parser.add_argument("--discovery-timeout", type=float, default=DEFAULT_DISCOVERY_TIMEOUT_S)
    parser.add_argument(
        "--answer", action="append", default=[], metavar="NAME=VALUE",
        help="answer a prompt that asked for values; repeatable. For a stand with no display",
    )
    args = parser.parse_args()

    test_id = args.test_id
    if test_id is None:
        frame = discover_running_test(args.state_endpoint, args.discovery_timeout)
        test_id = frame.test_id
        prompt = frame.state.get("operator_prompt")
        if prompt is None:
            logger.warning(
                "test %s is not waiting for an operator right now - acknowledging anyway, which "
                "the next await_operator() step will discard as stale",
                test_id,
            )
        else:
            logger.info("test %s is waiting for: %s", test_id, prompt)

    answers = dict(pair.split("=", 1) for pair in args.answer) if args.answer else None
    path = acknowledge(test_id, answers)
    logger.info("acknowledged test %s (%s)", test_id, path)
    sys.exit(0)
