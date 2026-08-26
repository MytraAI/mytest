"""The ydrive stand's thermal bounds, and the evaluation path that makes them
able to fire at all.

A rulebook covers a stand, not a device: these bounds are on the thermocouple
DAQ's channels while the bus bounds are on the ODrive's, and no one device
publishes both.
"""
from __future__ import annotations

import time

import pytest

from hardware.tc_daq.tc_daq_channels import CHANNEL_COUNT
from protocol.wire import DEVICE_TC_DAQ
from testbeds.ydrive_testbed.ydrive_testbed import YdriveTestbed
from hardware.cpx400dp.rails import deliverable_current_a
from testbeds.ydrive_testbed.ydrive_testbed import MOTOR_BUS, TC_DAQ_STALENESS_S
from hardware.tc_daq.transport import SILENCE_TIMEOUT_S
from testcases.asimov.live_rulebook_runner import LiveRulebookRunner
from testcases.asimov.rulebook import Bound, Rulebook, RulebookEvaluator, UnevaluableBoundError
from testcases.ydrive.rulebooks.ydrive_rulebook import (
    BUS_CURRENT_PERSISTENCE_S,
    TC_DROPOUT_GRACE_S,
    LIVE_TC_CHANNELS,
    MAX_BUS_CURRENT_A,
    MAX_TEMPERATURE_C,
    TC_PERSISTENCE_S,
    YDRIVE_RULEBOOK,
)

TEMPERATURE_BOUNDS = [b for b in YDRIVE_RULEBOOK.bounds if b.channel.startswith("temperature_")]

PUBLISHED_STATE = {"stopping_distance_m": 0.0}
"""What every ydrive run has in its published state, which the runner merges into
every frame it evaluates. Included here because stopping_distance_bound is
fatal and numeric: a frame without this channel is fine (an absent channel is no
result), but one carrying None would stop the run."""


# --- what is bounded --------------------------------------------------------


def test_every_live_channel_has_a_fatal_ceiling():
    assert {b.channel for b in TEMPERATURE_BOUNDS} == {
        f"temperature_{n}_c" for n in LIVE_TC_CHANNELS
    }
    for bound in TEMPERATURE_BOUNDS:
        assert bound.upper == MAX_TEMPERATURE_C == 80.0
        assert bound.fatal is True
        assert bound.persistence_s == TC_PERSISTENCE_S == 5.0


def test_no_unconnected_channel_is_bounded():
    """The DAQ publishes a faulted channel as None, and a numeric bound on None
    raises UnevaluableBoundError - which the runner treats as a stop. Bounding an
    unconnected channel would therefore abort every run on its first frame."""
    unwired = set(range(1, CHANNEL_COUNT + 1)) - set(LIVE_TC_CHANNELS)
    bounded = {b.channel for b in TEMPERATURE_BOUNDS}

    assert unwired, "this test is meaningless if every channel is wired"
    for n in unwired:
        assert f"temperature_{n}_c" not in bounded


def test_a_bounded_channel_going_open_stops_the_run():
    """Deliberate, and the other edge of the same rule: losing the sensor a
    thermal limit relies on is not a reason to keep driving."""
    bound = TEMPERATURE_BOUNDS[0]

    with pytest.raises(UnevaluableBoundError):
        bound.evaluate({bound.channel: None})


def test_a_brief_spike_above_the_ceiling_does_not_stop_the_run():
    """A thermocouple spikes from electrical noise as well as from heat, and this
    stand switches 48 V near the harness. Timestamps are passed in rather than
    waited out, so this stays deterministic and fast."""
    evaluator = RulebookEvaluator()
    evaluator.register(YDRIVE_RULEBOOK)
    hot = {**PUBLISHED_STATE, TEMPERATURE_BOUNDS[0].channel: 91.0}

    assert evaluator.evaluate(hot, 100.0) == [], "a violation on the first hot frame"
    assert evaluator.evaluate(hot, 100.0 + TC_PERSISTENCE_S - 0.1) == [], "fired early"


def test_staying_above_the_ceiling_for_the_debounce_stops_the_run():
    evaluator = RulebookEvaluator()
    evaluator.register(YDRIVE_RULEBOOK)
    hot = {**PUBLISHED_STATE, TEMPERATURE_BOUNDS[0].channel: 91.0}

    evaluator.evaluate(hot, 100.0)
    transitions = evaluator.evaluate(hot, 100.0 + TC_PERSISTENCE_S)

    assert [t.bound_label for t in transitions] == [TEMPERATURE_BOUNDS[0].label]
    assert transitions[0].violated is True and transitions[0].fatal is True


def test_one_cool_sample_resets_the_debounce_rather_than_accumulating():
    """So an intermittently spiking channel never trips it - which is the point,
    and also why a channel that spikes constantly hides a real rise."""
    evaluator = RulebookEvaluator()
    evaluator.register(YDRIVE_RULEBOOK)
    hot = {**PUBLISHED_STATE, TEMPERATURE_BOUNDS[0].channel: 91.0}
    cool = {**PUBLISHED_STATE, TEMPERATURE_BOUNDS[0].channel: 24.0}

    evaluator.evaluate(hot, 100.0)
    evaluator.evaluate(cool, 100.0 + TC_PERSISTENCE_S - 0.1)
    transitions = evaluator.evaluate(hot, 100.0 + TC_PERSISTENCE_S + 0.1)

    assert transitions == [], "the clock accumulated across an interruption"


def test_a_momentary_open_channel_does_not_stop_the_run():
    """Observed: one faulted frame out of 6852 ended a twelve-minute run. A
    thermocouple that drops a sample is not a thermocouple that is gone."""
    evaluator = RulebookEvaluator()
    evaluator.register(YDRIVE_RULEBOOK)
    absent = {**PUBLISHED_STATE, TEMPERATURE_BOUNDS[0].channel: None}

    assert evaluator.evaluate(absent, 100.0) == []
    assert evaluator.evaluate({**PUBLISHED_STATE, TEMPERATURE_BOUNDS[0].channel: 24.0}, 100.2) == []


def test_the_thermocouples_get_a_wider_dropout_window_than_the_framework_default():
    """This DAQ drops the odd sample; the bus channels do not, and a window is time
    spent without supervision - so it is widened where it is needed rather than
    everywhere."""
    from testcases.asimov.rulebook import DEFAULT_UNEVALUABLE_GRACE_S

    assert TC_DROPOUT_GRACE_S == 10.0 > DEFAULT_UNEVALUABLE_GRACE_S
    for bound in TEMPERATURE_BOUNDS:
        assert bound.unevaluable_grace_s == TC_DROPOUT_GRACE_S
    others = [b for b in YDRIVE_RULEBOOK.bounds if b not in TEMPERATURE_BOUNDS]
    assert all(b.unevaluable_grace_s == DEFAULT_UNEVALUABLE_GRACE_S for b in others)


def test_a_channel_that_stays_open_stops_the_run():
    """The other half: a bound that cannot be checked is not a bound that passed,
    so a sensor that is actually gone still ends the run - just not on frame one."""
    bound = TEMPERATURE_BOUNDS[0]
    evaluator = RulebookEvaluator()
    evaluator.register(YDRIVE_RULEBOOK)
    absent = {**PUBLISHED_STATE, TEMPERATURE_BOUNDS[0].channel: None}

    evaluator.evaluate(absent, 100.0)
    with pytest.raises(UnevaluableBoundError):
        evaluator.evaluate(absent, 100.0 + bound.unevaluable_grace_s + 0.1)


def test_the_ceiling_fires_above_80_and_not_below():
    bound = TEMPERATURE_BOUNDS[0]

    assert bound.evaluate({bound.channel: 80.5}) is True
    assert bound.evaluate({bound.channel: 79.5}) is False


def test_the_bus_current_ceiling_is_sized_from_the_stand_not_the_supply():
    """The ceiling comes off a measured run - 80% of a 14.97 A peak - and the
    debounce off the same run's longest stroke cycle, 27.8 s, times 1.5. Recorded
    here so a later edit to either has to restate what it was measured against."""
    bound = next(b for b in YDRIVE_RULEBOOK.bounds if b.channel == "board_ibus")

    assert bound.upper == MAX_BUS_CURRENT_A == 12.0
    assert bound.persistence_s == BUS_CURRENT_PERSISTENCE_S == 42.0
    assert bound.fatal is True
    assert MAX_BUS_CURRENT_A == pytest.approx(0.8 * 14.97, abs=0.05)
    assert BUS_CURRENT_PERSISTENCE_S == pytest.approx(1.5 * 27.8, abs=0.5)

    # Whether it can engage at all is open: the 420 W envelope caps a steady draw at
    # 8.75 A on this rail, so a long overdraw sags it into undervoltage_bound first.
    # Asserted so the day the bus is fed by something bigger, this fails loudly.
    assert deliverable_current_a(MOTOR_BUS.voltage_v) < MAX_BUS_CURRENT_A


# --- the stand publishes them -----------------------------------------------


def test_the_stand_records_the_daq_alongside_the_other_two_devices():
    """DEVICES is what the engine records into the run's directory, so a device
    missing from it streams into the unattributed record instead."""
    assert DEVICE_TC_DAQ in YdriveTestbed.DEVICES


# --- one rulebook, several streams ------------------------------------------


class FakePublisher:
    def __init__(self):
        self.state = {}

    def set_state(self, name, value):
        self.state[name] = value

    def state_snapshot(self):
        return dict(self.state)


class FakeFrame:
    def __init__(self, channels):
        self.seq = 0
        self.t = 0.0
        self.channels = channels


class FakeStream:
    """One device's telemetry, repeating - the runner takes one of these per
    stream and threads them independently."""

    def __init__(self, channels):
        self._channels = channels

    def discard_backlog(self):
        return 0

    def frames(self):
        while True:
            yield FakeFrame(dict(self._channels))
            time.sleep(0.01)


ODRIVE_FRAME = {"board_vbus_voltage": 48.0, "board_ibus": 1.0}
COOL_TC_FRAME = {f"temperature_{n}_c": 24.0 for n in LIVE_TC_CHANNELS}
HOT_TC_FRAME = {**COOL_TC_FRAME, "temperature_6_c": 91.0}


UNDEBOUNCED_RULEBOOK = Rulebook(
    name="undebounced_ydrive",
    test_names=["t"],
    bounds=[
        Bound(channel="board_vbus_voltage", lower=10.5, name="undervoltage_bound", fatal=True),
        Bound(
            channel="temperature_6_c",
            upper=MAX_TEMPERATURE_C,
            name="overtemperature_bound_6",
            fatal=True,
        ),
    ],
)
"""The same two kinds of bound as YDRIVE_RULEBOOK, minus the 5s debounce.

What the runner tests below check is which *streams* get evaluated, and waiting
out a real debounce would only make them slow - the debounce itself is checked
against explicit timestamps above."""


def _run_until_settled(*streams, rulebook=UNDEBOUNCED_RULEBOOK):
    runner = LiveRulebookRunner(test_id="t1", rulebooks=[rulebook], publisher=FakePublisher())
    runner.start(*streams)
    time.sleep(0.2)
    runner.stop()
    return runner


def test_a_temperature_bound_cannot_fire_from_the_odrive_stream_alone():
    """The failure this evaluation path exists to prevent: the channel is absent
    from every frame being watched, so the bound returns no result forever while
    the run reports a clean pass."""
    runner = _run_until_settled(FakeStream({**ODRIVE_FRAME, **{}}))

    assert runner.fatal_violation is None
    assert runner.summary().evaluated_frames > 0, "the stream was being read"


def test_a_temperature_breach_on_its_own_stream_stops_the_run():
    runner = _run_until_settled(FakeStream(ODRIVE_FRAME), FakeStream(HOT_TC_FRAME))

    assert runner.fatal_violation is not None
    assert "overtemperature_bound_6" in str(runner.fatal_violation)


def test_both_streams_are_evaluated_against_the_one_rulebook():
    """Each stream evaluates the bounds it carries and ignores the rest, which is
    what lets one rulebook span devices with no bound-to-device mapping."""
    runner = _run_until_settled(FakeStream(ODRIVE_FRAME), FakeStream(COOL_TC_FRAME))

    assert runner.fatal_violation is None
    statuses = runner.summary()
    assert statuses.evaluated_frames > 1


def test_the_stands_read_deadline_allows_the_gap_the_driver_tolerates():
    """Both ends watch the same silence. If the consumer gives up first, the
    driver's extra patience is unreachable and the only diagnosis left is "no
    frame arrived", instead of which port went quiet and for how long."""
    assert TC_DAQ_STALENESS_S > SILENCE_TIMEOUT_S == 10.0


def test_a_runner_with_no_stream_is_a_mistake_rather_than_a_quiet_pass():
    runner = LiveRulebookRunner(test_id="t1", rulebooks=[YDRIVE_RULEBOOK], publisher=FakePublisher())

    with pytest.raises(ValueError, match="at least one telemetry stream"):
        runner.start()
