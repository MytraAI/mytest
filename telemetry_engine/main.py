"""Entry point for the telemetry engine process.

This is the aggregator -> evaluation + storage stage, per the
architecture doc's process #3.

Runs the Aggregator's merged stream through both TelemetryStorage (raw
points) and the Evaluator (Rulebook bound checks, which emits a
ViolationEvent to ResultStorage on each pass/fail transition). Both
storage interfaces sit behind minimal ABCs, so a real time-series
database and a real report/relational store can replace the CSV
implementations later without touching this file or the aggregator.

REGISTERED_RULEBOOKS below is a manual list for now: as new DUTs and
their test cases/Rulebooks are added under testcases/, add their
Rulebook here too.

Run it directly with:

    python -m telemetry_engine.main
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from datetime import datetime
from pathlib import Path
from typing import List

from testcases.asimov.rulebook import Rulebook
from testcases.example_dut.rulebooks.cycle_dut_position_rulebook import CYCLE_DUT_POSITION_RULEBOOK

from .aggregator import Aggregator
from .csv_result_storage import CsvResultStorage
from .csv_storage import CsvStorage
from .evaluation import Evaluator
from .result_storage import ResultStorage
from .storage import TelemetryStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path("telemetry_engine/data")

REGISTERED_RULEBOOKS: List[Rulebook] = [CYCLE_DUT_POSITION_RULEBOOK]


async def _consume(
    aggregator: Aggregator, telemetry_storage: TelemetryStorage, evaluator: Evaluator, result_storage: ResultStorage
) -> None:
    async for item in aggregator.merged_stream():
        await telemetry_storage.write(item)
        for event in evaluator.evaluate(item):
            logger.info(
                "test %s (%s): %s %s (%s=%.3f)",
                event.test_id, event.test_name, event.bound_label, event.transition, event.channel, event.value,
            )
            await result_storage.write(event)


async def main(output_dir: Path = DEFAULT_OUTPUT_DIR) -> None:
    aggregator = Aggregator()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    telemetry_storage = CsvStorage(output_dir / f"telemetry_{timestamp}.csv")
    result_storage = CsvResultStorage(output_dir / f"results_{timestamp}.csv")

    evaluator = Evaluator()
    for rulebook in REGISTERED_RULEBOOKS:
        evaluator.register(rulebook)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # signal handlers aren't available on all platforms

    task = asyncio.create_task(
        _consume(aggregator, telemetry_storage, evaluator, result_storage), name="telemetry_engine_consume"
    )
    await stop.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await telemetry_storage.close()
    await result_storage.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    asyncio.run(main(args.output_dir))
