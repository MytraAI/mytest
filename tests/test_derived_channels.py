"""Derived test-case channels: sampled from telemetry, not pushed from a code path.

set_state() latches - a value sits on every frame until something pushes again -
which is right for an event and wrong for a live quantity, whose record then reads
as a staircase whose steps are wherever the code happened to run. A derivation is
evaluated on every state tick instead, against the newest frame of each device.
"""
from __future__ import annotations

import time

import pytest

from testcases.state_publisher import RunStatePublisher


def _publisher(devices=("odrive",), **kwargs):
    return RunStatePublisher(test_id="t", test_name="n", devices=devices, **kwargs)


def test_a_derivation_sees_the_newest_frame_of_every_device():
    """Keyed by device rather than handed one stream, because the interesting derived
    quantities cross devices - bus power is the supply's volts times its amps."""
    seen = {}
    publisher = _publisher(devices=("odrive", "cpx400dp"))
    publisher.set_derivation(lambda latest: seen.update(latest) or {}, ("odrive",))
    publisher.record_frame("odrive", {"turns_traveled": 12.0})
    publisher.record_frame("cpx400dp", {"voltage": 48.0})
    publisher.record_frame("odrive", {"turns_traveled": 19.0})

    publisher._derived_state()

    assert seen == {"odrive": {"turns_traveled": 19.0}, "cpx400dp": {"voltage": 48.0}}


def test_derived_values_land_in_the_state_bounds_are_evaluated_against():
    """A Bound has to be able to gate on a derived channel the same way it can on a pushed
    one, and the evaluator reads state_snapshot() - so these belong in the state itself,
    not only in the frame that goes out on the wire."""
    publisher = _publisher()
    publisher.set_derivation(
        lambda latest: {"metres": latest["odrive"]["turns"] * 0.084}, ("odrive",)
    )
    publisher.record_frame("odrive", {"turns": 100.0})

    for name, value in publisher._derived_state().items():
        publisher.set_state(name, value)

    assert publisher.state_snapshot()["metres"] == pytest.approx(8.4)


def test_a_derivation_that_raises_does_not_take_the_publisher_down():
    """This stream is the engine's only evidence that the run is still open. A derivation
    reading a channel a device has not sent yet must not end that."""
    publisher = _publisher()
    publisher.set_derivation(lambda latest: {"x": latest["odrive"]["missing"]}, ("odrive",))
    publisher.record_frame("odrive", {"present": 1.0})

    assert publisher._derived_state() == {}


def test_a_derivation_reading_a_device_the_run_does_not_claim_is_refused():
    """Nothing would ever feed it, and its channels would then hold whatever their channel
    list seeded them with - present in the recording, numeric, and wrong."""
    publisher = _publisher(devices=("odrive",))

    with pytest.raises(ValueError, match="tc_daq"):
        publisher.set_derivation(lambda latest: {}, ("odrive", "tc_daq"))


def test_waiting_on_frames_that_never_come_raises_rather_than_freezing_a_channel():
    """The other half of the same hazard: a stream that exists but that nothing in this
    process reads, which no amount of declaring can detect until frames do or do not
    turn up."""
    publisher = _publisher()
    publisher.set_derivation(lambda latest: {}, ("odrive",))

    with pytest.raises(TimeoutError, match="odrive"):
        publisher.await_derivation_frames(timeout_s=0.2)


def test_waiting_returns_as_soon_as_every_declared_device_has_reported():
    publisher = _publisher(devices=("odrive", "tc_daq"))
    publisher.set_derivation(lambda latest: {}, ("odrive", "tc_daq"))
    publisher.record_frame("odrive", {})
    publisher.record_frame("tc_daq", {})

    started = time.monotonic()
    publisher.await_derivation_frames(timeout_s=5.0)

    assert time.monotonic() - started < 0.5


def test_no_derivation_means_no_wait_and_no_channels():
    """The default for every test that has not asked for one."""
    publisher = _publisher()

    publisher.await_derivation_frames(timeout_s=0.0)
    assert publisher._derived_state() == {}
