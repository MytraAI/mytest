"""End-to-end demo of the Telemetry Aggregator.

Runs CycleDutPositionTest with a shortened duration/dwell for
verification. The test starts its own testbed and DUT internally in
PreTestSetup, and this demo prints everything the aggregator's merged
stream produces.

Note this test case never touches the DAQ's acquisition, so the
aggregator's raw-stream side stays empty here; only the DUT's tagged
stream flows. Proving the DAQ's raw stream specifically still works is
what hardware/demo_end_to_end.py covers. This demo is instead about
proving the aggregator merges whatever a self-contained, testbed-owning
test case produces.

Run with (from the repo root, Mytest/):
    python -m telemetry_engine.demo_aggregator_run
"""
from __future__ import annotations

import asyncio
import logging

from hardware.protocol import TaggedTelemetryFrame
from testcases.example_dut.testcases.halt_tests import CycleDutPositionTest

from .aggregator import Aggregator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def print_merged(aggregator: Aggregator) -> None:
    async for item in aggregator.merged_stream():
        if isinstance(item, TaggedTelemetryFrame):
            print("tagged:", item.test_id, item.seq, round(item.t, 3), item.channels, flush=True)
        else:
            print("raw:   ", item.seq, round(item.t, 3), item.channels, flush=True)


async def main() -> None:
    aggregator = Aggregator()
    printer = asyncio.create_task(print_merged(aggregator), name="demo-print-merged")

    print("--- running CycleDutPositionTest (shortened for verification) ---", flush=True)
    await asyncio.to_thread(CycleDutPositionTest(cycle_duration_s=20.0, dwell_s=6.0).run)

    await asyncio.sleep(0.5)  # let any in-flight frames drain before shutting down
    printer.cancel()
    await asyncio.gather(printer, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
