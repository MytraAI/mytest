"""End-to-end demo of the testcase execution process.

Runs BaseExampleDutTest - the base case with no test sequence logic -
through its full three-phase lifecycle. PreTestSetup starts the
testbed and the DUT itself internally. While the test runs, this demo
concurrently prints tagged frames arriving on the Telemetry
Publisher's output, proving the publisher runs alongside
MainExecution and republishes test-tagged DUT telemetry independently
of the test case's own in-sequence telemetry subscription - even with
no actual test logic.

Note: unlike earlier demos, this one does NOT wrap the test case in a
separate `with ExampleTestbed():`. BaseExampleDutTest already starts
its own testbed and DUT in PreTestSetup, so an outer testbed here
would just fight it over the same ports.

Run with (from the repo root, Mytest/): python -m testcases.demo_testcase_run
"""
from __future__ import annotations

import logging
import threading

import zmq

from hardware.protocol import DEFAULT_TAGGED_TELEMETRY_ENDPOINT, TAGGED_TELEMETRY_TOPIC, TaggedTelemetryFrame

from .example_dut.testcases.base_example_dut_test import BaseExampleDutTest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def print_tagged_frames(stop: threading.Event) -> None:
    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, TAGGED_TELEMETRY_TOPIC)
    sub.connect(DEFAULT_TAGGED_TELEMETRY_ENDPOINT)
    poller = zmq.Poller()
    poller.register(sub, zmq.POLLIN)
    try:
        printed = 0
        while not stop.is_set() and printed < 5:
            events = dict(poller.poll(timeout=200))
            if sub not in events:
                continue
            _, raw = sub.recv_multipart()
            frame = TaggedTelemetryFrame.from_bytes(raw)
            print("tagged:", frame.test_id, frame.seq, round(frame.t, 3), frame.channels)
            printed += 1
    finally:
        sub.close(linger=0)


def main() -> None:
    stop_printer = threading.Event()
    printer = threading.Thread(target=print_tagged_frames, args=(stop_printer,), daemon=True)
    printer.start()

    test_case = BaseExampleDutTest()
    test_case.run()

    stop_printer.set()
    printer.join(timeout=5)


if __name__ == "__main__":
    main()
