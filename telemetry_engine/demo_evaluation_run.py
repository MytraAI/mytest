"""End-to-end demo of offline replay - the verification that a stored run
explains its own verdict.

Records a real (shortened) CycleDutPositionTest run to disk exactly the way
the engine does, then replays the stored telemetry back through the *same*
RulebookEvaluator logic the live runner used, and compares the two
timelines. If they match, three things are proven at once: the wide
per-device CSV keeps everything evaluation needs, the verdict's embedded
timeline agrees with the telemetry beside it, and collapsing to a single
online evaluator lost nothing.

This replaces an earlier demo of a second, *online* post-hoc evaluator
running inside the engine (Evaluator -> CsvResultStorage). That evaluator
was removed: it duplicated the live runner's bound logic over a lossier
copy of the stream, gated on a hand-maintained rulebook list that had
already drifted. Post-hoc evaluation earns its place offline, against
stored telemetry, which is what this now demonstrates. See replay.py and
telemetry_engine/main.py.

Run with (from the repo root, Mytest/):
    python -m telemetry_engine.demo_evaluation_run
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from protocol import asyncio_compat
from protocol.paths import run_dir
from protocol.verdict import read_verdict
from testcases.example_dut.rulebooks.cycle_dut_position_rulebook import CYCLE_DUT_POSITION_RULEBOOK
from testcases.example_dut.testcases.halt_tests import CycleDutPositionTest
from protocol.wire import DEVICE_DUT

from .aggregator import Aggregator
from .replay import compare_with_verdict, replay
from .wide_csv_storage import WideCsvTelemetryStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

OUTPUT_DIR = Path("telemetry_engine/data") / f"demo_replay_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


async def main() -> None:
    aggregator = Aggregator()
    session = datetime.now().strftime("%Y%m%d_%H%M%S")
    storage = WideCsvTelemetryStorage(OUTPUT_DIR, session)

    async def consume() -> None:
        async for item in aggregator.merged_stream():
            await storage.write(item)

    consumer = asyncio.create_task(consume(), name="demo-replay-consume")

    print("--- running CycleDutPositionTest (shortened for verification) ---", flush=True)
    test = CycleDutPositionTest(cycle_duration_s=20.0, dwell_s=6.0, require_engine=False)
    test._output_dir = OUTPUT_DIR  # this demo is the recorder, so the verdict lands here
    await asyncio.to_thread(test.run)

    await asyncio.sleep(0.5)  # let any in-flight frames drain before shutting down
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await storage.close()

    directory = run_dir(OUTPUT_DIR, test.test_id)
    verdict = read_verdict(directory / "verdict.json")
    print(f"--- verdict: {verdict.outcome} ({verdict.reason or 'no reason recorded'}) ---", flush=True)
    print(f"recorded transitions: {len(verdict.violations)} {verdict.violated_bounds()}")

    telemetry = directory / DEVICE_DUT / "telemetry.csv"
    print(f"--- replaying {telemetry} ---", flush=True)
    timeline = replay(telemetry, [CYCLE_DUT_POSITION_RULEBOOK])
    comparison = compare_with_verdict(verdict, timeline)
    print(comparison.explain())
    for violation in timeline:
        print(f"  {violation.transition}: {violation.bound_label} ({violation.channel}={violation.value})")


if __name__ == "__main__":
    asyncio_compat.run(main())
