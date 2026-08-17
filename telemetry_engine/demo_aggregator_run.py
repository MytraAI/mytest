"""End-to-end demo of the Telemetry Aggregator.

Runs CycleDutPositionTest with a shortened duration/dwell for
verification. The test starts its own testbed and DUT internally in
PreTestSetup, and this demo prints everything the aggregator's merged
stream produces.

The aggregator subscribes to every known device plus the test-state
stream, so the output interleaves per-device frames (labelled by device)
with the run's own state announcements. The DAQ and power supply are
running as part of the testbed, so their frames appear too even though
this test never watches them - which is the point: recording breadth is
the engine's business, not the test's. Proving the DAQ's stream
specifically still works end to end is what
hardware/demos/demo_end_to_end.py covers.

Run with (from the repo root, Mytest/):
    python -m telemetry_engine.demo_aggregator_run
"""
from __future__ import annotations

import asyncio
import logging

from protocol import asyncio_compat
from protocol.wire import RunStateFrame
from testcases.example_dut.testcases.halt_tests import CycleDutPositionTest

from .aggregator import Aggregator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def print_merged(aggregator: Aggregator) -> None:
    async for item in aggregator.merged_stream():
        if isinstance(item, RunStateFrame):
            print("state: ", item.test_id, item.devices, round(item.t, 3), item.state, flush=True)
        else:
            print(f"{item.device:<13}", item.seq, round(item.t, 3), item.channels, flush=True)


async def main() -> None:
    aggregator = Aggregator()
    printer = asyncio.create_task(print_merged(aggregator), name="demo-print-merged")

    print("--- running CycleDutPositionTest (shortened for verification) ---", flush=True)
    await asyncio.to_thread(CycleDutPositionTest(cycle_duration_s=20.0, dwell_s=6.0, require_engine=False).run)

    await asyncio.sleep(0.5)  # let any in-flight frames drain before shutting down
    printer.cancel()
    await asyncio.gather(printer, return_exceptions=True)


if __name__ == "__main__":
    asyncio_compat.run(main())
