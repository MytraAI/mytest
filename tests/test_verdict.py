"""The verdict record's contract: round-trip, derived fields, atomic
write, and the engine's amend/parse behaviour.

This is the shape a report database eventually ingests, so it's the piece
most worth pinning down: a silent change to a field name or an enum value
here is a schema migration, not a refactor.
"""
from __future__ import annotations

import json

import pytest

from protocol.paths import run_dir, verdict_path
from protocol.verdict import (
    BoundsResult,
    Lifecycle,
    Verdict,
    Violation,
    amend_completeness,
    read_verdict,
    write_verdict,
)


def make_violation(label="overcurrent_bound", transition="violated", fatal=True, seq=7, t=100.5):
    return Violation(
        bound_label=label,
        rulebook_name="ydrive_rulebook",
        channel="board_ibus",
        value=31.2,
        fatal=fatal,
        transition=transition,
        seq=seq,
        t=t,
    )


def make_verdict(**overrides):
    defaults = dict(
        test_id="abc123",
        test_name="endurance_cycle_test",
        lifecycle=Lifecycle.STOPPED,
        bounds_result=BoundsResult.PASS,
        started_at=100.0,
        ended_at=160.0,
    )
    defaults.update(overrides)
    return Verdict(**defaults)


def test_round_trip_preserves_every_field():
    original = make_verdict(
        lifecycle=Lifecycle.ERRORED,
        bounds_result=BoundsResult.FAIL,
        reason="fatal bound overcurrent_bound violated",
        any_fatal=True,
        violations=[make_violation(), make_violation(transition="cleared", seq=9)],
        metadata={"tuning_profile": "max_load", "operator": "cs"},
        completeness={"frame_count": 1200, "seq_gap_count": 3, "dropped_frames": 0},
    )
    restored = Verdict.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored == original


def test_round_trip_tolerates_absent_optional_fields():
    """A verdict written by an older/leaner writer must still parse - the
    engine reads files it didn't write."""
    minimal = {
        "test_id": "x",
        "test_name": "t",
        "lifecycle": Lifecycle.COMPLETED,
        "bounds_result": BoundsResult.PASS,
        "started_at": 1.0,
        "ended_at": 2.0,
    }
    verdict = Verdict.from_dict(minimal)
    assert verdict.violations == []
    assert verdict.metadata == {}
    assert verdict.completeness is None
    assert verdict.any_fatal is False


def test_outcome_is_derived_not_stored_as_source_of_truth():
    verdict = make_verdict(lifecycle=Lifecycle.STOPPED, bounds_result=BoundsResult.PASS)
    assert verdict.outcome == "STOPPED/PASS"
    # It appears in the serialized form for readability...
    assert verdict.to_dict()["outcome"] == "STOPPED/PASS"
    # ...but is never read back as state: a bogus stored value is ignored.
    restored = Verdict.from_dict({**verdict.to_dict(), "outcome": "NONSENSE"})
    assert restored.outcome == "STOPPED/PASS"


def test_stopped_run_with_no_violations_is_a_recordable_success():
    """The case a single flat enum could not express. Both real-hardware
    test cases run until stopped, so this is their *expected* good
    outcome - see protocol/verdict.py."""
    verdict = make_verdict(lifecycle=Lifecycle.STOPPED, bounds_result=BoundsResult.PASS)
    assert verdict.bounds_result == BoundsResult.PASS
    assert verdict.lifecycle == Lifecycle.STOPPED


def test_violated_bounds_is_distinct_and_first_seen_ordered():
    verdict = make_verdict(
        violations=[
            make_violation(label="undervoltage_bound", seq=1),
            make_violation(label="overcurrent_bound", seq=2),
            make_violation(label="undervoltage_bound", transition="cleared", seq=3),
            make_violation(label="undervoltage_bound", seq=4),
        ]
    )
    assert verdict.violated_bounds() == ["undervoltage_bound", "overcurrent_bound"]


def test_duration_never_negative():
    assert make_verdict(started_at=10.0, ended_at=4.0).duration_s == 0.0
    assert make_verdict(started_at=10.0, ended_at=12.5).duration_s == pytest.approx(2.5)


def test_write_verdict_lands_in_the_run_directory(tmp_path):
    verdict = make_verdict(violations=[make_violation()])
    path = write_verdict(verdict, tmp_path)

    assert path == verdict_path(tmp_path, verdict.test_id)
    assert path.parent == run_dir(tmp_path, verdict.test_id)
    assert read_verdict(path) == verdict
    # No temp files left behind by the atomic write.
    assert [p.name for p in path.parent.iterdir()] == ["verdict.json"]


def test_amend_completeness_preserves_the_tests_own_record(tmp_path):
    verdict = make_verdict(
        bounds_result=BoundsResult.FAIL,
        any_fatal=True,
        violations=[make_violation()],
        metadata={"operator": "cs"},
    )
    path = write_verdict(verdict, tmp_path)

    assert amend_completeness(path, {"frame_count": 42, "seq_gap_count": 1, "dropped_frames": 0}) is True

    amended = read_verdict(path)
    assert amended.completeness == {"frame_count": 42, "seq_gap_count": 1, "dropped_frames": 0}
    # Everything the test authored survives untouched.
    assert amended.violations == verdict.violations
    assert amended.bounds_result == BoundsResult.FAIL
    assert amended.metadata == {"operator": "cs"}
    assert amended.any_fatal is True


def test_amend_completeness_reports_failure_rather_than_clobbering(tmp_path):
    """A corrupt verdict must not be silently replaced - the engine logs
    and leaves it for a human. See run_recorder._finalize."""
    path = verdict_path(tmp_path, "abc123")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")

    assert amend_completeness(path, {"frame_count": 1}) is False
    assert path.read_text() == "{not json"


def test_amend_completeness_on_missing_file_is_false(tmp_path):
    assert amend_completeness(verdict_path(tmp_path, "nope"), {"frame_count": 1}) is False


@pytest.mark.parametrize("bad", ["{}", "[]", '{"test_id": "x"}'])
def test_read_verdict_raises_on_unusable_content(tmp_path, bad):
    path = tmp_path / "verdict.json"
    path.write_text(bad)
    with pytest.raises((KeyError, ValueError, TypeError)):
        read_verdict(path)
