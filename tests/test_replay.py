"""Replay equivalence: a stored run must explain its own verdict.

This is the load-bearing check behind two structural decisions. If
replaying a run's stored telemetry through the same RulebookEvaluator
reproduces the transition timeline the live runner recorded, then the wide
per-device CSV kept everything evaluation needs, and collapsing to a single
online evaluator lost nothing. If it can't, the stored record is
insufficient - which is the thing to find out before a database port, not
after.
"""
from __future__ import annotations

import asyncio

from protocol.paths import run_telemetry_path
from protocol.verdict import BoundsResult, Lifecycle, Verdict, read_verdict, write_verdict
from telemetry_engine.replay import compare_with_verdict, read_frames, replay
from telemetry_engine.storage import WriteItem
from telemetry_engine.wide_csv_storage import WideCsvTelemetryStorage
from asimov.live_rulebook_runner import LiveRulebookRunner
from asimov.rulebook import Bound, Rulebook

RULEBOOK = Rulebook(
    name="test_rulebook",
    test_names=["t"],
    bounds=[Bound(name="overcurrent", channel="ibus", upper=30.0, fatal=False)],
)


class FakePublisher:
    """Stands in for RunStatePublisher - the runner only sets state and reads a
    snapshot back to merge into what it evaluates."""

    def set_state(self, name, value):
        pass

    def state_snapshot(self):
        return {}

    def record_frame(self, device, channels):
        pass  # derived channels are exercised in tests/test_derived_channels.py

    def await_derivation_frames(self):
        pass


def frames_for(values, device="odrive", test_id="run1"):
    return [
        WriteItem(device=device, seq=i, t=float(i), channels={"ibus": v}, test_id=test_id)
        for i, v in enumerate(values)
    ]


def record(tmp_path, frames):
    """Record frames exactly as the engine does, and author a verdict from
    a live run over the same frames - i.e. reproduce the real pipeline."""
    storage = WideCsvTelemetryStorage(tmp_path, "sess")

    async def scenario():
        for frame in frames:
            await storage.write(frame)
        await storage.close()

    asyncio.run(scenario())

    runner = LiveRulebookRunner(test_id="run1", rulebooks=[RULEBOOK], publisher=FakePublisher())
    for frame in frames:
        runner.evaluate(dict(frame.channels), seq=frame.seq, frame_t=frame.t)
    summary = runner.summary()

    write_verdict(
        Verdict(
            test_id="run1",
            test_name="t",
            lifecycle=Lifecycle.STOPPED,
            bounds_result=summary.bounds_result,
            started_at=frames[0].t,
            ended_at=frames[-1].t,
            any_fatal=summary.any_fatal,
            violations=summary.violations,
        ),
        tmp_path,
    )
    return run_telemetry_path(tmp_path, "run1", "odrive")


def test_replay_reproduces_the_recorded_timeline(tmp_path):
    telemetry = record(tmp_path, frames_for([1.0, 50.0, 60.0, 2.0, 99.0]))
    verdict = read_verdict(tmp_path / "runs" / "run1" / "verdict.json")

    comparison = compare_with_verdict(verdict, replay(telemetry, [RULEBOOK]))

    assert comparison.matches, comparison.explain()
    assert verdict.bounds_result == BoundsResult.FAIL
    assert [v.transition for v in verdict.violations] == ["violated", "cleared", "violated"]


def test_replay_of_a_clean_run_finds_nothing(tmp_path):
    telemetry = record(tmp_path, frames_for([1.0, 2.0, 3.0]))
    verdict = read_verdict(tmp_path / "runs" / "run1" / "verdict.json")

    comparison = compare_with_verdict(verdict, replay(telemetry, [RULEBOOK]))

    assert comparison.matches
    assert comparison.replayed == []
    assert verdict.bounds_result == BoundsResult.PASS


def test_replay_with_a_tighter_bound_finds_what_the_run_did_not(tmp_path):
    """The other reason offline replay exists: asking whether a different
    limit would have caught something."""
    telemetry = record(tmp_path, frames_for([1.0, 20.0, 25.0]))
    verdict = read_verdict(tmp_path / "runs" / "run1" / "verdict.json")
    assert verdict.bounds_result == BoundsResult.PASS

    tighter = Rulebook(
        name="tighter", test_names=["t"], bounds=[Bound(name="overcurrent", channel="ibus", upper=10.0)]
    )
    timeline = replay(telemetry, [tighter])

    assert [v.transition for v in timeline] == ["violated"]
    assert not compare_with_verdict(verdict, timeline).matches


def test_values_survive_the_csv_round_trip_with_their_types(tmp_path):
    storage = WideCsvTelemetryStorage(tmp_path, "sess")

    async def scenario():
        await storage.write(
            WriteItem(
                device="odrive",
                seq=0,
                t=1.5,
                channels={"num": 12.5, "count": 7, "flag": True, "state": "IDLE"},
                test_id="run1",
            )
        )
        await storage.close()

    asyncio.run(scenario())

    frame = next(read_frames(run_telemetry_path(tmp_path, "run1", "odrive")))
    assert frame.seq == 0 and frame.t == 1.5
    assert frame.channels["num"] == 12.5
    assert frame.channels["count"] == 7
    assert frame.channels["flag"] is True
    assert frame.channels["state"] == "IDLE"


def test_absent_channel_stays_absent_rather_than_becoming_zero(tmp_path):
    """An empty cell must not read back as 0.0 - a bound would then compare
    against a value the hardware never reported."""
    storage = WideCsvTelemetryStorage(tmp_path, "sess")

    async def scenario():
        await storage.write(
            WriteItem(device="odrive", seq=0, t=0.0, channels={"a": 1.0, "b": 2.0}, test_id="run1")
        )
        await storage.write(
            WriteItem(device="odrive", seq=1, t=1.0, channels={"a": 1.0}, test_id="run1")
        )
        await storage.close()

    asyncio.run(scenario())

    frames = list(read_frames(run_telemetry_path(tmp_path, "run1", "odrive")))
    assert "b" in frames[0].channels
    assert "b" not in frames[1].channels


def test_a_mismatch_is_explained_and_flags_transport_loss(tmp_path):
    """When replay disagrees, the explanation must point at frame loss as a
    legitimate cause - a debounced bound can resolve differently when replay
    sees a gap the live runner never did."""
    telemetry = record(tmp_path, frames_for([1.0, 2.0, 3.0]))
    verdict = read_verdict(tmp_path / "runs" / "run1" / "verdict.json")
    verdict.completeness = {"seq_gap_count": 12}

    tighter = Rulebook(
        name="tighter", test_names=["t"], bounds=[Bound(name="overcurrent", channel="ibus", upper=1.5)]
    )
    comparison = compare_with_verdict(verdict, replay(telemetry, [tighter]))

    assert not comparison.matches
    explanation = comparison.explain()
    assert "recorded 0 transition(s)" in explanation
    assert "lost 12 frame(s) in transit" in explanation
