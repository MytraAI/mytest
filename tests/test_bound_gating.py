"""Gating: a Bound that only applies during part of a run.

Two things are pinned here, and the second used to be a silent bug.

**Gating on published state works live.** A gate channel can be a value the
test published via set_state(), not just a hardware channel. That only works
because the runner evaluates against the union of the device's frame and the
test's own state - the frames it consumes are a device's *raw* stream and carry
no test state at all. Without that merge such a bound could never fire.

**A gate channel that exists nowhere raises.** Previously an unresolvable gate
returned "doesn't apply to this frame", so the bound was skipped on every frame
for the whole run while `bounds_result` still reported PASS - supervision that
looked present and wasn't, the same failure shape as a declared-but-absent
channel. Now it's loud.

The distinction matters and is deliberate: a gate that *exists* and holds some
other value means the bound legitimately doesn't apply right now, which is the
entire point of gating. A gate that exists nowhere means it can never apply.
"""
from __future__ import annotations

import pytest

from testcases.asimov.live_rulebook_runner import LiveRulebookRunner
from testcases.asimov.rulebook import Bound, Rulebook, UnevaluableBoundError


class FakePublisher:
    """Stands in for RunStatePublisher: the runner sets state on it and reads a
    snapshot back to merge into what it evaluates."""

    def __init__(self, initial=None):
        self.state = dict(initial or {})

    def set_state(self, name, value):
        self.state[name] = value

    def state_snapshot(self):
        return dict(self.state)

    def record_frame(self, device, channels):
        pass  # derived channels are exercised in tests/test_derived_channels.py

    def await_derivation_frames(self):
        pass


GATED = Bound(
    name="overcurrent_while_moving",
    channel="ibus",
    upper=30.0,
    gate_channel="monitoring_active",
    gate_value=True,
    fatal=True,
)


# ---- Bound.evaluate, in isolation ------------------------------------------


def test_a_gate_holding_the_wrong_value_means_the_bound_does_not_apply():
    assert GATED.evaluate({"ibus": 99.0, "monitoring_active": False}) is None


def test_a_gate_holding_the_right_value_lets_the_bound_evaluate():
    assert GATED.evaluate({"ibus": 99.0, "monitoring_active": True}) is True
    assert GATED.evaluate({"ibus": 1.0, "monitoring_active": True}) is False


def test_a_gate_channel_that_exists_nowhere_raises():
    """The silent-skip bug: this used to return None forever."""
    with pytest.raises(UnevaluableBoundError) as excinfo:
        GATED.evaluate({"ibus": 99.0})

    assert "monitoring_active" in str(excinfo.value)
    assert "could never be evaluated" in str(excinfo.value)


def test_an_ungated_bound_is_unaffected():
    plain = Bound(name="overcurrent", channel="ibus", upper=30.0)

    assert plain.evaluate({"ibus": 99.0}) is True
    assert plain.evaluate({}) is None  # its own channel absent is still just "no result"


# ---- through the live runner, where the state merge happens ----------------


def make_runner(publisher, *bounds):
    rulebook = Rulebook(name="rb", test_names=["t"], bounds=list(bounds))
    return LiveRulebookRunner(test_id="t1", rulebooks=[rulebook], publisher=publisher)


def test_the_runner_gates_on_published_state_not_present_in_the_frame():
    """The frame is a device's raw stream - monitoring_active exists only in
    the test's published state, and gating on it must still work."""
    publisher = FakePublisher({"monitoring_active": True})
    runner = make_runner(publisher, GATED)

    with pytest.raises(Exception) as excinfo:  # FatalBoundViolation
        runner.evaluate({**{"ibus": 99.0}, **publisher.state_snapshot()}, seq=1, frame_t=1.0)

    assert "overcurrent_while_moving" in str(excinfo.value)


def test_the_runner_skips_a_gated_bound_while_the_gate_is_closed():
    publisher = FakePublisher({"monitoring_active": False})
    runner = make_runner(publisher, GATED)

    runner.evaluate({**{"ibus": 99.0}, **publisher.state_snapshot()}, seq=1, frame_t=1.0)

    summary = runner.summary()
    assert summary.violations == []
    assert summary.any_fatal is False


def test_a_gate_opening_mid_run_starts_evaluating():
    """The whole point of gating: a bound that is meaningless during setup and
    load-bearing once the axis is armed."""
    publisher = FakePublisher({"monitoring_active": False})
    runner = make_runner(publisher, GATED)

    runner.evaluate({**{"ibus": 99.0}, **publisher.state_snapshot()}, seq=1, frame_t=1.0)
    assert runner.summary().violations == []

    publisher.set_state("monitoring_active", True)
    with pytest.raises(Exception):
        runner.evaluate({**{"ibus": 99.0}, **publisher.state_snapshot()}, seq=2, frame_t=2.0)

    assert runner.summary().any_fatal is True
