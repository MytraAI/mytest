"""What the live runner judges when its subscriber is older than its start.

A TelemetryClient is created when the testbed starts, and a run reaches
main_execution seconds later - so the queue behind it holds frames from while
the stand was still being set up. Judging those fails a run for a condition that
predates it: a de-energized bus reads volts below any undervoltage bound.
"""
from __future__ import annotations

import time

from testcases.asimov.live_rulebook_runner import LiveRulebookRunner
from testcases.asimov.rulebook import Bound, Rulebook


class FakeFrame:
    def __init__(self, seq, channels, device="odrive"):
        self.seq = seq
        self.t = float(seq)
        self.channels = channels
        self.device = device


class FakePublisher:
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


class FakeTelemetryClient:
    """Frames already queued, then frames arriving live - the distinction the
    real client's discard_backlog() draws against its socket queue."""

    def __init__(self, queued, live):
        self._queued = list(queued)
        self._live = list(live)
        self.evaluated_backlog = False

    def discard_backlog(self):
        dropped = len(self._queued)
        self._queued = []
        return dropped

    def frames(self):
        for frame in self._queued:
            self.evaluated_backlog = True
            yield frame
        while True:
            for frame in self._live:
                yield frame
            # A real stream does not end, and keeps arriving - which is also
            # what lets the runner notice a stop between frames.
            time.sleep(0.01)


UNDERVOLTAGE = Bound(name="undervoltage_bound", channel="vbus", lower=10.5, fatal=True)

COLD_BUS = FakeFrame(1, {"vbus": 0.0})
LIVE_BUS = FakeFrame(2, {"vbus": 48.0})


def make_runner():
    rulebook = Rulebook(name="rb", test_names=["t"], bounds=[UNDERVOLTAGE])
    return LiveRulebookRunner(test_id="t1", rulebooks=[rulebook], publisher=FakePublisher())


def _run_until_settled(runner, client):
    runner.start(client)
    time.sleep(0.2)
    runner.stop()


def test_frames_queued_before_the_runner_started_are_not_judged():
    """The stand was cold on purpose while they were captured."""
    client = FakeTelemetryClient(queued=[COLD_BUS, COLD_BUS], live=[LIVE_BUS])
    runner = make_runner()

    _run_until_settled(runner, client)

    assert not client.evaluated_backlog, "the runner judged frames from before it started"
    assert runner.fatal_violation is None


def test_a_live_breach_still_stops_the_run():
    """Skipping the backlog must not skip anything that happens after it."""
    client = FakeTelemetryClient(queued=[LIVE_BUS], live=[COLD_BUS])
    runner = make_runner()

    _run_until_settled(runner, client)

    assert runner.fatal_violation is not None
    assert "undervoltage_bound" in str(runner.fatal_violation)
