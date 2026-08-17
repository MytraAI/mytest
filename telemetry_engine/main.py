"""Telemetry engine entry point - the recording process, the architecture
doc's process #5.

Four jobs, and deliberately no others:

1. **Subscribe to every device.** One socket per device in
   protocol/wire.py's TELEMETRY_ENDPOINTS, plus the testcase process's
   run-state stream. See aggregator.py.
2. **Attribute and record.** Each frame is routed - into the open run's
   directory if that run declared its device, otherwise into the continuous
   per-session record - then queued to a writer task and written wide, one
   file per device. Exactly one destination per frame, so nothing is written
   twice and nothing falls in a gap. See run_recorder.py for the routing rule
   and wide_csv_storage.py for the files.
3. **Stamp completeness.** Per run, count frames, per-device seq gaps and
   writer drops, and amend that run's verdict once its state stream goes quiet
   - or synthesize one if the test process died. See run_recorder.py.
4. **Advertise that it's recording, and what it covers.** A heartbeat a test
   checks before starting and while running, carrying the device set so a test
   can't declare a device nothing is recording. See protocol/heartbeat.py.

Note what the engine does *not* do: no telemetry is relayed to it by the
testcase process. Frames arrive straight from each driver, so a test that
crashes costs its state context and nothing else.

It deliberately does *not* evaluate Rulebooks. Pass/fail has one author, the
test process, which records its own transition timeline in the verdict; a
second evaluator here would read a lossier copy of the stream and could
disagree about the same run. The shared evaluation logic remains available
for offline replay against stored telemetry - see replay.py.

No evaluation result ever flows back to a running test. The heartbeat carries
liveness only.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from datetime import datetime
from pathlib import Path
from typing import Sequence

from protocol import heartbeat
from protocol.paths import DEFAULT_OUTPUT_DIR, ensure_output_dir
from protocol.wire import RunStateFrame

from .aggregator import Aggregator
from .run_recorder import RunRecorder
from .storage import WriteItem
from .wide_csv_storage import WideCsvTelemetryStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

WRITE_QUEUE_SIZE = 2000
"""Frames the writer task may fall behind by before the engine starts
dropping them. Storage writes are decoupled from the aggregator's socket
reads for exactly this reason: a disk hiccup used to backpressure straight
into the SUB socket (write() was awaited per frame, and every store
flushed per row), which is what would actually overflow a socket buffer.
A drop here is counted and reported in the verdict's completeness, never
silent - see run_recorder.py."""

FLUSH_INTERVAL_S = 1.0
"""How often the writer task flushes to disk. Replaces the old
flush-every-row behaviour, which at ~4,500 rows/s was the most likely
source of a stall in the first place."""

RECONCILE_INTERVAL_S = 1.0


async def _consume(aggregator: Aggregator, queue: "asyncio.Queue[WriteItem]", recorder: RunRecorder) -> None:
    """Read the merged stream, decide where each frame belongs, and hand it
    to the writer.

    This is where attribution happens. A run-state frame updates the
    recorder's view of which run is open and what it has published; a device
    frame is then routed by asking the recorder whether the open run claims
    that device. State is merged into the row here, which is why the recorded
    file still shows what the test was doing on every frame even though no
    telemetry passes through the test process.

    Accounting happens here rather than in the writer so that a frame dropped
    for want of queue space is still counted against its run - the engine knows
    it received it, which is the distinction completeness reports.
    """
    dropped_unattributed = 0
    next_warning = 1
    async for item in aggregator.merged_stream():
        if isinstance(item, RunStateFrame):
            recorder.observe_state(item)
            continue

        routed = recorder.route(item.device)
        channels = dict(item.channels)
        test_id = None
        if routed is not None:
            # The open run's published state rides along on every row it
            # claims, alongside the device's own channels.
            test_id, state = routed
            channels.update(state)
            recorder.observe(item, test_id)

        write_item = WriteItem(
            device=item.device, seq=item.seq, t=item.t, channels=channels, test_id=test_id
        )
        try:
            queue.put_nowait(write_item)
        except asyncio.QueueFull:
            if test_id is not None:
                recorder.note_dropped(test_id)  # reported per run in the verdict's completeness
            else:
                # An unattributed frame belongs to no run, so there's nowhere
                # to record it but the log - and sustained backpressure would
                # mean thousands of identical lines a second, which makes the
                # stall worse. Warn on an exponentially growing threshold so
                # the first drop is immediate and the scale stays visible.
                dropped_unattributed += 1
                if dropped_unattributed >= next_warning:
                    logger.warning(
                        "write queue full - %d unattributed frame(s) dropped so far (latest from %s)",
                        dropped_unattributed, item.device,
                    )
                    next_warning *= 10


async def _write_loop(queue: "asyncio.Queue[WriteItem]", storage: WideCsvTelemetryStorage) -> None:
    """Drain the queue into storage, flushing periodically."""
    last_flush = asyncio.get_running_loop().time()
    while True:
        item = await queue.get()
        try:
            await storage.write(item)
            now = asyncio.get_running_loop().time()
            if now - last_flush >= FLUSH_INTERVAL_S:
                storage.flush()
                last_flush = now
        except Exception:
            logger.exception("failed to write a telemetry frame")
        finally:
            queue.task_done()


async def _reconcile_loop(
    recorder: RunRecorder,
    output_dir: Path,
    devices: Sequence[str],
    stop: asyncio.Event,
    interval_s: float = RECONCILE_INTERVAL_S,
) -> None:
    """Tick the recorder and refresh the heartbeat ~once a second, waking
    promptly when stop is set rather than sleeping the full interval.

    The heartbeat advertises which devices this engine is subscribed to, so a
    test can confirm before it starts that everything it declares is actually
    being recorded (see protocol/heartbeat.py)."""
    while not stop.is_set():
        heartbeat.write_heartbeat(output_dir, devices)
        try:
            await recorder.reconcile()
        except Exception:
            # Stop refreshing the heartbeat and say why. A recorder that
            # can't finalize runs (a full disk, a permissions problem) is not
            # recording, and the heartbeat going stale is exactly how a
            # running test finds that out and aborts. Dying silently here
            # would leave the engine process up, looking alive.
            logger.exception("reconciliation failed - stopping heartbeat so running tests abort")
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass


async def main(output_dir: Path = DEFAULT_OUTPUT_DIR) -> None:
    output_dir = ensure_output_dir(output_dir)
    session = datetime.now().strftime("%Y%m%d_%H%M%S")
    aggregator = Aggregator()
    storage = WideCsvTelemetryStorage(output_dir, session)
    recorder = RunRecorder(output_dir, storage)
    queue: "asyncio.Queue[WriteItem]" = asyncio.Queue(maxsize=WRITE_QUEUE_SIZE)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # signal handlers aren't available on all platforms

    # Publish liveness before anything else, so a test started immediately
    # after this process comes up doesn't lose the race and refuse to run.
    heartbeat.write_heartbeat(output_dir, aggregator.devices)
    logger.info(
        "telemetry engine recording to %s (session %s), devices: %s",
        output_dir, session, ", ".join(aggregator.devices),
    )

    tasks = [
        asyncio.create_task(_consume(aggregator, queue, recorder), name="telemetry_engine_consume"),
        asyncio.create_task(_write_loop(queue, storage), name="telemetry_engine_write"),
        asyncio.create_task(
            _reconcile_loop(recorder, output_dir, aggregator.devices, stop),
            name="telemetry_engine_reconcile",
        ),
    ]
    try:
        await stop.wait()
    finally:
        # Stop reading first, then drain what's already queued, so a clean
        # shutdown doesn't discard frames the engine had already accepted.
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        try:
            await asyncio.wait_for(queue.join(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("gave up draining %d queued frames at shutdown", queue.qsize())
        for task in tasks[1:]:
            task.cancel()
        await asyncio.gather(*tasks[1:], return_exceptions=True)

        await recorder.flush()
        await storage.close()
        heartbeat.clear_heartbeat()  # so the next test fails fast instead of waiting out staleness
        logger.info("telemetry engine stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    asyncio.run(main(args.output_dir))
