"""End-to-end demo of wide per-device telemetry storage.

Runs CycleDutPositionTest (shortened for verification; it starts its own
testbed and DUT internally in PreTestSetup) through
WideCsvTelemetryStorage, then reads the resulting files back and prints a
summary. This proves frames actually land on disk in the right shape: one
row per frame, one column per channel, one file per device per run - and
that a run directory ends up holding both the telemetry and the verdict
the test wrote itself.

Runs with require_engine=False: the real telemetry engine isn't up here,
this demo is its own consumer. A real run refuses to start without an
engine recording (see protocol/heartbeat.py), which is exactly the
protection this demo has to opt out of.

Run with (from the repo root, Mytest/):
    python -m telemetry_engine.demo_storage_run
"""
from __future__ import annotations

import asyncio
import csv
import logging
from datetime import datetime
from pathlib import Path

from protocol.paths import runs_dir
from testcases.example_dut.testcases.halt_tests import CycleDutPositionTest

from .aggregator import Aggregator
from .wide_csv_storage import WideCsvTelemetryStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

OUTPUT_DIR = Path("telemetry_engine/data") / f"demo_storage_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


async def main() -> None:
    aggregator = Aggregator()
    session = datetime.now().strftime("%Y%m%d_%H%M%S")
    storage = WideCsvTelemetryStorage(OUTPUT_DIR, session)

    async def consume() -> None:
        async for item in aggregator.merged_stream():
            await storage.write(item)

    consumer = asyncio.create_task(consume(), name="demo-storage-consume")

    print("--- running CycleDutPositionTest (shortened for verification) ---", flush=True)
    test = CycleDutPositionTest(cycle_duration_s=20.0, dwell_s=6.0, require_engine=False)
    test._output_dir = OUTPUT_DIR  # this demo is the recorder, so point the verdict here too
    await asyncio.to_thread(test.run)

    await asyncio.sleep(0.5)  # let any in-flight frames drain before shutting down
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await storage.close()

    for path in sorted(storage.paths()):
        with open(path, newline="") as handle:
            rows = list(csv.DictReader(handle))
        print(f"--- {path} ---", flush=True)
        print(f"rows: {len(rows)}, columns: {len(rows[0]) if rows else 0}")
        if rows:
            print("first row seq/t:", rows[0].get("seq"), rows[0].get("t"))

    for run in sorted(runs_dir(OUTPUT_DIR).glob("*")):
        print(f"--- run directory {run.name} ---", flush=True)
        for item in sorted(run.rglob("*")):
            if item.is_file():
                print(f"  {item.relative_to(run)} ({item.stat().st_size} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
