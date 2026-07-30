"""End-to-end demo of the testcase execution process, for ydrive.

Runs BaseYdriveTest - the base case with no test sequence logic -
through its full three-phase lifecycle, against MockOdriveBackend
(use_mock=True; no real ODrive is required to run this demo). While the
test runs, this demo concurrently prints the run-state frames arriving
on the state publisher's output, proving the run announces itself - its
identity, the devices it claims, and every value its steps publish -
alongside MainExecution and independently of the test case's own
in-sequence telemetry subscription, even with no actual test logic.

Note: unlike hardware/demos/demo_odrive.py, this one does NOT wrap the
test case in a separate `with YdriveTestbed():`. BaseYdriveTest already
starts its own testbed in PreTestSetup, so an outer testbed here would
just fight it over the same ports.

Run with (from the repo root, Mytest/): python -m testcases.demo_ydrive_test_run
"""
from __future__ import annotations

import logging
import threading

import zmq

from protocol.wire import DEFAULT_RUN_STATE_ENDPOINT, RUN_STATE_TOPIC, RunStateFrame

from .ydrive.testcases.base_ydrive_test import BaseYdriveTest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def print_state_frames(stop: threading.Event) -> None:
    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, RUN_STATE_TOPIC)
    sub.connect(DEFAULT_RUN_STATE_ENDPOINT)
    poller = zmq.Poller()
    poller.register(sub, zmq.POLLIN)
    try:
        printed = 0
        while not stop.is_set() and printed < 5:
            events = dict(poller.poll(timeout=200))
            if sub not in events:
                continue
            _, raw = sub.recv_multipart()
            frame = RunStateFrame.from_bytes(raw)
            print("state:", frame.test_id, frame.devices, round(frame.t, 3), frame.state)
            printed += 1
    finally:
        sub.close(linger=0)


def main() -> None:
    stop_printer = threading.Event()
    printer = threading.Thread(target=print_state_frames, args=(stop_printer,), daemon=True)
    printer.start()

    test_case = BaseYdriveTest(use_mock=True, require_engine=False)
    test_case.run()

    stop_printer.set()
    printer.join(timeout=5)


if __name__ == "__main__":
    main()
