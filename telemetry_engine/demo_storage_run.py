"""End-to-end demo of CSV telemetry storage.

Runs CycleDutPositionTest (shortened for verification; it starts its
own testbed and DUT internally in PreTestSetup) through CsvStorage,
then reads the resulting CSV back and prints a summary. This proves
points actually landed on disk with the right shape: long format, one
row per channel value.

This test case never touches the DAQ's acquisition, so all rows here
are tagged (DUT) points. There's no untagged/raw-only phase to show,
unlike the older DAQ-based demos.

Run with (from the repo root, Mytest/):
    python -m telemetry_engine.demo_storage_run
"""
from __future__ import annotations

import asyncio
import csv
import logging
from datetime import datetime
from pathlib import Path

from testcases.example_dut.testcases.halt_tests import CycleDutPositionTest

from .aggregator import Aggregator
from .csv_storage import CsvStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

OUTPUT_PATH = Path("telemetry_engine/data") / f"demo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"


async def main() -> None:
    aggregator = Aggregator()
    storage = CsvStorage(OUTPUT_PATH)

    async def consume() -> None:
        async for item in aggregator.merged_stream():
            await storage.write(item)

    consumer = asyncio.create_task(consume(), name="demo-storage-consume")

    print("--- running CycleDutPositionTest (shortened for verification) ---", flush=True)
    await asyncio.to_thread(CycleDutPositionTest(cycle_duration_s=20.0, dwell_s=6.0).run)

    await asyncio.sleep(0.5)  # let any in-flight frames drain before shutting down
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await storage.close()

    print(f"--- reading back {OUTPUT_PATH} ---", flush=True)
    with open(OUTPUT_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    print(f"total rows: {len(rows)}")
    print("first row:", rows[0])
    print("last row: ", rows[-1])


if __name__ == "__main__":
    asyncio.run(main())
