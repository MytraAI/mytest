"""End-to-end demo of Rulebook evaluation.

Runs CycleDutPositionTest (shortened for verification; it starts its
own testbed and DUT internally in PreTestSetup) through the aggregator
-> Evaluator -> CsvResultStorage pipeline, registering
CYCLE_DUT_POSITION_RULEBOOK. It then reads the resulting results CSV
back and prints every violation/clear transition, proving bounds
actually fire (and clear) correctly.

Run with (from the repo root, Mytest/):
    python -m telemetry_engine.demo_evaluation_run
"""
from __future__ import annotations

import asyncio
import csv
import logging
from datetime import datetime
from pathlib import Path

from testcases.example_dut.rulebooks.cycle_dut_position_rulebook import CYCLE_DUT_POSITION_RULEBOOK
from testcases.example_dut.testcases.halt_tests import CycleDutPositionTest

from .aggregator import Aggregator
from .csv_result_storage import CsvResultStorage
from .evaluation import Evaluator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

OUTPUT_PATH = Path("telemetry_engine/data") / f"demo_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"


async def main() -> None:
    aggregator = Aggregator()
    evaluator = Evaluator()
    evaluator.register(CYCLE_DUT_POSITION_RULEBOOK)
    results = CsvResultStorage(OUTPUT_PATH)

    async def consume() -> None:
        async for item in aggregator.merged_stream():
            for event in evaluator.evaluate(item):
                print(f"{event.transition}: {event.bound_label} ({event.channel}={event.value:.3f})", flush=True)
                await results.write(event)

    consumer = asyncio.create_task(consume(), name="demo-evaluation-consume")

    print("--- running CycleDutPositionTest (shortened for verification) ---", flush=True)
    await asyncio.to_thread(CycleDutPositionTest(cycle_duration_s=20.0, dwell_s=6.0).run)

    await asyncio.sleep(0.5)  # let any in-flight frames drain before shutting down
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await results.close()

    print(f"--- reading back {OUTPUT_PATH} ---", flush=True)
    with open(OUTPUT_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    print(f"total events: {len(rows)}")
    for row in rows:
        print(row)


if __name__ == "__main__":
    asyncio.run(main())
