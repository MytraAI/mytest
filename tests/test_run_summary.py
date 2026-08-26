"""The bound-summary rule the verdict's bounds_result comes from.

Two things are pinned here, both deliberate project decisions rather than
incidental behaviour:

- *Any* violation fails the run, fatal or not. `fatal` decides only whether
  the test aborts. So a Bound is always a pass/fail criterion, never a
  purely informational monitor.
- A runner that never evaluated a frame reports NOT_EVALUATED, not PASS.
  Without that, a run with zero monitoring is indistinguishable from a
  clean one - and BaseYdriveTest deliberately leaves start() to its
  subclasses, so "constructed but never started" is a reachable state.
"""
from __future__ import annotations

import time

from protocol.verdict import BoundsResult
from testcases.asimov.live_rulebook_runner import LiveRulebookRunner, RunSummary
from testcases.asimov.rulebook import Bound, Rulebook


class FakePublisher:
    """Stands in for RunStatePublisher - the runner sets state and reads a
    snapshot back to merge into what it evaluates."""

    def __init__(self):
        self.state = {}

    def set_state(self, name, value):
        self.state[name] = value

    def state_snapshot(self):
        return dict(self.state)

    def record_frame(self, device, channels):
        pass  # derived channels are exercised in tests/test_derived_channels.py

    def await_derivation_frames(self):
        pass


def make_runner(*bounds):
    rulebook = Rulebook(name="test_rulebook", test_names=["t"], bounds=list(bounds))
    return LiveRulebookRunner(test_id="abc", rulebooks=[rulebook], publisher=FakePublisher())


def test_never_started_is_not_evaluated_not_a_pass():
    runner = make_runner(Bound(name="b", channel="c", upper=10.0))
    summary = runner.summary()
    assert summary.evaluated_frames == 0
    assert summary.violations == []
    assert summary.bounds_result == BoundsResult.NOT_EVALUATED


def test_clean_run_passes():
    runner = make_runner(Bound(name="b", channel="c", upper=10.0))
    for seq in range(3):
        runner.evaluate({"c": 1.0}, seq=seq, frame_t=float(seq))
    summary = runner.summary()
    assert summary.evaluated_frames == 3
    assert summary.bounds_result == BoundsResult.PASS
    assert summary.any_fatal is False


def test_non_fatal_violation_still_fails_the_run():
    runner = make_runner(Bound(name="warn_bound", channel="c", upper=10.0, fatal=False))
    runner.evaluate({"c": 1.0}, seq=0, frame_t=0.0)
    runner.evaluate({"c": 99.0}, seq=1, frame_t=1.0)

    summary = runner.summary()
    assert summary.bounds_result == BoundsResult.FAIL
    assert summary.any_fatal is False  # it failed, but it didn't abort


def test_violation_that_later_clears_still_fails_the_run():
    runner = make_runner(Bound(name="warn_bound", channel="c", upper=10.0, fatal=False))
    runner.evaluate({"c": 1.0}, seq=0, frame_t=0.0)
    runner.evaluate({"c": 99.0}, seq=1, frame_t=1.0)
    runner.evaluate({"c": 1.0}, seq=2, frame_t=2.0)

    summary = runner.summary()
    assert summary.bounds_result == BoundsResult.FAIL
    assert [(v.transition, v.seq) for v in summary.violations] == [("violated", 1), ("cleared", 2)]


def test_timeline_records_both_directions_with_frame_identity():
    runner = make_runner(Bound(name="b", channel="c", upper=10.0, fatal=False))
    runner.evaluate({"c": 99.0}, seq=41, frame_t=1000.5)
    runner.evaluate({"c": 0.0}, seq=42, frame_t=1001.0)

    violated, cleared = runner.summary().violations
    assert (violated.transition, violated.seq, violated.t, violated.value) == ("violated", 41, 1000.5, 99.0)
    assert (cleared.transition, cleared.seq, cleared.t) == ("cleared", 42, 1001.0)
    assert violated.bound_label == "b"
    assert violated.rulebook_name == "test_rulebook"
    assert violated.channel == "c"


def test_fatal_violation_sets_any_fatal_and_raises():
    runner = make_runner(Bound(name="kill_bound", channel="c", upper=10.0, fatal=True))
    try:
        runner.evaluate({"c": 99.0}, seq=1, frame_t=1.0)
    except Exception as exc:  # FatalBoundViolation
        assert "kill_bound" in str(exc)
    else:
        raise AssertionError("a fatal bound must raise")

    summary = runner.summary()
    assert summary.any_fatal is True
    assert summary.bounds_result == BoundsResult.FAIL


def test_frames_with_the_bound_channel_absent_still_count_as_evaluated():
    """An absent channel means the bound doesn't apply, not that monitoring
    didn't happen - so this is a PASS, not NOT_EVALUATED."""
    runner = make_runner(Bound(name="b", channel="missing", upper=10.0))
    runner.evaluate({"other": 1.0}, seq=0, frame_t=0.0)

    summary = runner.summary()
    assert summary.evaluated_frames == 1
    assert summary.violations == []
    assert summary.bounds_result == BoundsResult.PASS


def test_summary_returns_a_snapshot_not_a_live_view():
    """summary() is read from the main thread while the runner's own thread
    may still be appending - the caller must not see later mutations."""
    runner = make_runner(Bound(name="b", channel="c", upper=10.0, fatal=False))
    runner.evaluate({"c": 99.0}, seq=1, frame_t=1.0)
    snapshot = runner.summary()
    runner.evaluate({"c": 0.0}, seq=2, frame_t=2.0)

    assert len(snapshot.violations) == 1
    assert len(runner.summary().violations) == 2


def test_empty_summary_defaults_to_not_evaluated():
    assert RunSummary().bounds_result == BoundsResult.NOT_EVALUATED


def test_unevaluable_bound_forces_not_evaluated_rather_than_pass():
    """Some frames evaluated cleanly, then supervision broke - reporting PASS
    would claim the DUT behaved when the run can't actually know.

    Regression test: found in review."""
    from testcases.asimov.rulebook import UnevaluableBoundError

    runner = make_runner(Bound(name="uv", channel="vbus", lower=10.5, unevaluable_grace_s=0.01))
    runner.evaluate({"vbus": 48.0}, seq=0, frame_t=0.0)
    assert runner.summary().bounds_result == BoundsResult.PASS

    # Twice, spanning the grace window: one absent sample is tolerated, a channel
    # that stays absent is a lost sensor - see DEFAULT_UNEVALUABLE_GRACE_S.
    runner.evaluate({"vbus": None}, seq=1, frame_t=1.0)
    time.sleep(0.05)
    try:
        runner.evaluate({"vbus": None}, seq=2, frame_t=2.0)
    except UnevaluableBoundError as exc:
        runner._unevaluable = str(exc)  # what _run() does on the runner's thread

    summary = runner.summary()
    # Two, not one: the frame whose absence was tolerated was still evaluated -
    # every other bound in it was judged. Only the frame that raised is not.
    assert summary.evaluated_frames == 2
    assert summary.unevaluable is not None
    assert summary.bounds_result == BoundsResult.NOT_EVALUATED
