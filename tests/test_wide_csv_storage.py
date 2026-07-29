"""Wide per-device telemetry storage: header formation, routing, and the
two ways a channel can be missing.

The header is the subtle part. A wide file's columns are fixed when the
header line is written, but the tagged stream's full channel set isn't
knowable from frame one - test-published state channels (test_status,
{bound}_status, current_step) appear only once something publishes them.
So the writer samples the first HEADER_SAMPLE_FRAMES frames to take the
union first.
"""
from __future__ import annotations

import asyncio
import csv

from protocol.paths import raw_telemetry_path, run_telemetry_path
from protocol.wire import TaggedTelemetryFrame, TelemetryFrame
from telemetry_engine.wide_csv_storage import HEADER_SAMPLE_FRAMES, WideCsvTelemetryStorage


def tagged(seq, channels, test_id="run1", device="odrive", t=None):
    return TaggedTelemetryFrame(
        test_id=test_id,
        test_name="endurance_cycle_test",
        seq=seq,
        t=float(seq) if t is None else t,
        channels=channels,
        device=device,
    )


def raw(seq, channels, device="odrive"):
    return TelemetryFrame(seq=seq, t=float(seq), channels=channels, device=device)


def read_csv(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


async def write_all(storage, frames):
    for frame in frames:
        await storage.write(frame)


def run(coro):
    """Drive a coroutine to completion. Deliberately plain asyncio.run
    rather than pytest-asyncio, so the project gains no extra dependency
    for four async call sites."""
    return asyncio.run(coro)


def test_row_per_frame_column_per_channel(tmp_path):
    storage = WideCsvTelemetryStorage(tmp_path, "sess")

    async def scenario():
        await write_all(storage, [tagged(i, {"a": i, "b": i * 2}) for i in range(3)])
        await storage.close()

    run(scenario())

    rows = read_csv(run_telemetry_path(tmp_path, "run1", "odrive"))
    assert len(rows) == 3
    assert list(rows[0].keys()) == ["seq", "t", "a", "b"]
    assert rows[2]["a"] == "2" and rows[2]["b"] == "4"


def test_header_is_the_union_of_the_sampled_frames(tmp_path):
    """A state channel that only appears on a later frame - but still
    within the sampling window - must make it into the header."""
    storage = WideCsvTelemetryStorage(tmp_path, "sess")

    async def scenario():
        await storage.write(tagged(0, {"a": 1}))
        await storage.write(tagged(1, {"a": 1, "test_status": "PASS"}))
        await storage.close()

    run(scenario())

    rows = read_csv(run_telemetry_path(tmp_path, "run1", "odrive"))
    assert list(rows[0].keys()) == ["seq", "t", "a", "test_status"]
    assert rows[0]["test_status"] == ""  # absent on that frame, visible as empty
    assert rows[1]["test_status"] == "PASS"


def test_channel_appearing_after_the_header_is_fixed_is_dropped(tmp_path):
    """Documented, bounded behaviour: past the sampling window the schema
    is fixed. The frame is still recorded, just without the late channel."""
    storage = WideCsvTelemetryStorage(tmp_path, "sess")

    async def scenario():
        await write_all(storage, [tagged(i, {"a": i}) for i in range(HEADER_SAMPLE_FRAMES)])
        await storage.write(tagged(HEADER_SAMPLE_FRAMES, {"a": 99, "late": 1}))
        await storage.close()

    run(scenario())

    rows = read_csv(run_telemetry_path(tmp_path, "run1", "odrive"))
    assert "late" not in rows[0]
    assert len(rows) == HEADER_SAMPLE_FRAMES + 1
    assert rows[-1]["a"] == "99"


def test_short_run_below_the_sampling_window_still_writes(tmp_path):
    """close() must open and flush a file that never reached
    HEADER_SAMPLE_FRAMES, or a brief run would lose everything."""
    storage = WideCsvTelemetryStorage(tmp_path, "sess")

    async def scenario():
        await storage.write(tagged(0, {"a": 1}))
        await storage.close()

    run(scenario())

    rows = read_csv(run_telemetry_path(tmp_path, "run1", "odrive"))
    assert len(rows) == 1


def test_devices_and_runs_are_routed_to_separate_files(tmp_path):
    """Per-device files are why different rates and independent seq
    numbering don't have to be reconciled at storage time."""
    storage = WideCsvTelemetryStorage(tmp_path, "sess")

    async def scenario():
        await storage.write(tagged(0, {"a": 1}, device="odrive"))
        await storage.write(tagged(0, {"x": 9}, device="daq"))
        await storage.write(tagged(0, {"a": 2}, test_id="run2", device="odrive"))
        await storage.close()

    run(scenario())

    assert len(read_csv(run_telemetry_path(tmp_path, "run1", "odrive"))) == 1
    assert list(read_csv(run_telemetry_path(tmp_path, "run1", "daq"))[0].keys()) == ["seq", "t", "x"]
    assert len(read_csv(run_telemetry_path(tmp_path, "run2", "odrive"))) == 1


def test_untagged_frames_go_to_the_raw_tree_not_a_run(tmp_path):
    """Raw frames carry no test_id, so they can't be keyed per run - they
    belong to the continuous instrument record."""
    storage = WideCsvTelemetryStorage(tmp_path, "sess")

    async def scenario():
        await storage.write(raw(0, {"a": 1}))
        await storage.close()

    run(scenario())

    assert raw_telemetry_path(tmp_path, "odrive", "sess").exists()
    assert not (tmp_path / "runs").exists()


def test_reopening_appends_rather_than_truncating(tmp_path):
    """An engine restart mid-run must keep adding to the same run's file."""
    async def session_one():
        storage = WideCsvTelemetryStorage(tmp_path, "sess1")
        await storage.write(tagged(0, {"a": 1}))
        await storage.close()

    async def session_two():
        storage = WideCsvTelemetryStorage(tmp_path, "sess2")
        await storage.write(tagged(1, {"a": 2}))
        await storage.close()

    run(session_one())
    run(session_two())

    rows = read_csv(run_telemetry_path(tmp_path, "run1", "odrive"))
    assert [r["seq"] for r in rows] == ["0", "1"]


def test_close_run_closes_only_that_runs_files(tmp_path):
    storage = WideCsvTelemetryStorage(tmp_path, "sess")

    async def scenario():
        await storage.write(tagged(0, {"a": 1}, test_id="run1"))
        await storage.write(tagged(0, {"a": 1}, test_id="run2"))
        storage.close_run("run1")
        # run2 is still open and still accepting frames
        await storage.write(tagged(1, {"a": 2}, test_id="run2"))
        await storage.close()

    run(scenario())

    assert len(read_csv(run_telemetry_path(tmp_path, "run1", "odrive"))) == 1
    assert len(read_csv(run_telemetry_path(tmp_path, "run2", "odrive"))) == 2


def test_paths_and_row_counts_survive_close(tmp_path):
    """close() empties the open-writer dict, and a caller summarising what was
    produced naturally asks afterwards - reading off the live dict silently
    returned nothing.

    Regression test: found in review; demo_storage_run's file summary was a
    silent no-op because of it."""
    storage = WideCsvTelemetryStorage(tmp_path, "sess")

    async def scenario():
        await storage.write(tagged(0, {"a": 1}, test_id="run1"))
        await storage.write(tagged(0, {"a": 1}, test_id="run2"))
        storage.close_run("run1")  # closed early, must still be reported
        await storage.close()

    run(scenario())

    assert len(storage.paths()) == 2
    assert all(count == 1 for count in storage.row_counts().values())
