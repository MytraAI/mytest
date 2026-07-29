"""Engine-side finalization: completeness stamping, CRASHED synthesis, and
the per-device seq accounting.

The bug this replaces is worth remembering. The old spool-and-reconcile
design could synthesize an INCOMPLETE verdict *and* then write the test's
real one, as two contradictory records for one run, whenever teardown
outlasted the staleness window. Now the verdict's existence is the whole
check, so a late verdict simply wins.
"""
from __future__ import annotations

import asyncio

from protocol.paths import verdict_path
from protocol.verdict import BoundsResult, Lifecycle, Verdict, read_verdict, write_verdict
from protocol.wire import TaggedTelemetryFrame
from telemetry_engine.run_recorder import RunRecorder
from telemetry_engine.wide_csv_storage import WideCsvTelemetryStorage


def frame(seq, test_id="run1", device="odrive", t=None, channels=None):
    return TaggedTelemetryFrame(
        test_id=test_id,
        test_name="endurance_cycle_test",
        seq=seq,
        t=float(seq) if t is None else t,
        channels=channels or {"a": 1.0},
        device=device,
    )


def make_recorder(tmp_path, staleness_s=15.0):
    storage = WideCsvTelemetryStorage(tmp_path, "sess")
    return RunRecorder(tmp_path, storage, staleness_s=staleness_s), storage


def a_verdict(test_id="run1", **overrides):
    defaults = dict(
        test_id=test_id,
        test_name="endurance_cycle_test",
        lifecycle=Lifecycle.STOPPED,
        bounds_result=BoundsResult.PASS,
        started_at=0.0,
        ended_at=10.0,
    )
    defaults.update(overrides)
    return Verdict(**defaults)


def test_completeness_is_stamped_onto_the_tests_own_verdict(tmp_path):
    recorder, _ = make_recorder(tmp_path)
    write_verdict(a_verdict(), tmp_path)

    for seq in range(5):
        recorder.observe(frame(seq), now=100.0)
    asyncio.run(recorder.reconcile(now=200.0))  # well past staleness

    verdict = read_verdict(verdict_path(tmp_path, "run1"))
    assert verdict.completeness["frame_count"] == 5
    assert verdict.completeness["seq_gap_count"] == 0
    assert verdict.completeness["dropped_frames"] == 0
    assert verdict.lifecycle == Lifecycle.STOPPED  # the test's own record is untouched


def test_seq_gaps_are_counted(tmp_path):
    recorder, _ = make_recorder(tmp_path)
    write_verdict(a_verdict(), tmp_path)

    for seq in (0, 1, 5, 6):  # 2,3,4 lost in transit
        recorder.observe(frame(seq), now=100.0)
    asyncio.run(recorder.reconcile(now=200.0))

    completeness = read_verdict(verdict_path(tmp_path, "run1")).completeness
    assert completeness["seq_gap_count"] == 3
    assert completeness["frame_count"] == 4


def test_seq_is_tracked_per_device_not_globally(tmp_path):
    """Each driver assigns seq independently, so a shared counter would
    invent gaps every time two devices' frames interleave."""
    recorder, _ = make_recorder(tmp_path)
    write_verdict(a_verdict(), tmp_path)

    for seq in range(3):
        recorder.observe(frame(seq, device="odrive"), now=100.0)
        recorder.observe(frame(seq, device="daq"), now=100.0)
    asyncio.run(recorder.reconcile(now=200.0))

    completeness = read_verdict(verdict_path(tmp_path, "run1")).completeness
    assert completeness["seq_gap_count"] == 0
    assert completeness["frame_count"] == 6
    assert set(completeness["devices"]) == {"odrive", "daq"}
    assert completeness["devices"]["daq"]["frame_count"] == 3


def test_writer_drops_are_counted_separately_from_transit_loss(tmp_path):
    """Two loss sources with different fixes, so they stay distinct."""
    recorder, _ = make_recorder(tmp_path)
    write_verdict(a_verdict(), tmp_path)

    recorder.observe(frame(0), now=100.0)
    recorder.observe(frame(2), now=100.0)  # one lost in transit
    recorder.note_dropped(frame(3))  # received but unwritable
    asyncio.run(recorder.reconcile(now=200.0))

    completeness = read_verdict(verdict_path(tmp_path, "run1")).completeness
    assert completeness["seq_gap_count"] == 1
    assert completeness["dropped_frames"] == 1


def test_crashed_is_synthesized_when_no_verdict_was_written(tmp_path):
    """A test process killed outright never writes one - the run must still
    leave a record."""
    recorder, _ = make_recorder(tmp_path)

    recorder.observe(frame(0, t=50.0), now=100.0)
    recorder.observe(frame(1, t=60.0), now=100.0)
    asyncio.run(recorder.reconcile(now=200.0))

    verdict = read_verdict(verdict_path(tmp_path, "run1"))
    assert verdict.lifecycle == Lifecycle.CRASHED
    assert verdict.bounds_result == BoundsResult.NOT_EVALUATED
    assert verdict.started_at == 50.0 and verdict.ended_at == 60.0
    assert verdict.completeness["frame_count"] == 2
    assert "never wrote a verdict" in verdict.reason


def test_a_late_verdict_is_not_shadowed_by_a_synthesized_one(tmp_path):
    """The old design's real bug: slow teardown produced two contradictory
    records. Existence is now the whole check, so the test's own verdict
    wins whenever it lands."""
    recorder, _ = make_recorder(tmp_path)
    recorder.observe(frame(0), now=100.0)

    # Teardown is still running at the moment staleness elapses...
    write_verdict(a_verdict(reason="stopped by operator"), tmp_path)
    asyncio.run(recorder.reconcile(now=200.0))

    verdict = read_verdict(verdict_path(tmp_path, "run1"))
    assert verdict.lifecycle == Lifecycle.STOPPED
    assert verdict.reason == "stopped by operator"
    assert verdict.completeness["frame_count"] == 1


def test_a_run_is_finalized_only_once(tmp_path):
    recorder, _ = make_recorder(tmp_path)
    write_verdict(a_verdict(), tmp_path)
    recorder.observe(frame(0), now=100.0)

    asyncio.run(recorder.reconcile(now=200.0))
    asyncio.run(recorder.reconcile(now=300.0))  # must not synthesize a second record

    assert read_verdict(verdict_path(tmp_path, "run1")).lifecycle == Lifecycle.STOPPED


def test_an_active_run_is_left_alone(tmp_path):
    recorder, _ = make_recorder(tmp_path)
    recorder.observe(frame(0), now=100.0)

    asyncio.run(recorder.reconcile(now=105.0))  # inside the staleness window

    assert not verdict_path(tmp_path, "run1").exists()


def test_corrupt_verdict_is_left_for_a_human_not_overwritten(tmp_path):
    recorder, _ = make_recorder(tmp_path)
    path = verdict_path(tmp_path, "run1")
    path.parent.mkdir(parents=True)
    path.write_text("{corrupt")
    recorder.observe(frame(0), now=100.0)

    asyncio.run(recorder.reconcile(now=200.0))

    assert path.read_text() == "{corrupt"


def test_flush_stamps_completeness_but_never_synthesizes(tmp_path):
    """At engine shutdown a run with no verdict may simply still be
    running, so it must not be declared crashed."""
    recorder, _ = make_recorder(tmp_path)
    recorder.observe(frame(0, test_id="finished"), now=100.0)
    recorder.observe(frame(0, test_id="ongoing"), now=100.0)
    write_verdict(a_verdict(test_id="finished"), tmp_path)

    asyncio.run(recorder.flush())

    assert read_verdict(verdict_path(tmp_path, "finished")).completeness["frame_count"] == 1
    assert not verdict_path(tmp_path, "ongoing").exists()


def test_a_straggler_frame_cannot_overwrite_a_finalized_record(tmp_path):
    """Finalizing pops the track, so a frame arriving afterwards used to start
    a fresh track holding just that frame - and the next tick finalized it
    again, replacing a correct completeness record with a count of 1.

    Regression test: found in review, not by a failing run."""
    recorder, _ = make_recorder(tmp_path)
    write_verdict(a_verdict(), tmp_path)
    for seq in range(5):
        recorder.observe(frame(seq), now=100.0)
    asyncio.run(recorder.reconcile(now=200.0))
    assert read_verdict(verdict_path(tmp_path, "run1")).completeness["frame_count"] == 5

    recorder.observe(frame(99), now=300.0)  # straggler
    recorder.note_dropped(frame(100))
    asyncio.run(recorder.reconcile(now=400.0))

    assert read_verdict(verdict_path(tmp_path, "run1")).completeness["frame_count"] == 5
