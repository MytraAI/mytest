"""What happens when a run's published state and a device's own channels
share a name.

Nothing declares state channel names - a test invents them with
TestCase.set_state - so a name it picks can collide with something the device
actually measures. The merge used to be a plain dict.update(), which resolved
that silently in the test's favour and destroyed the only copy of a real
measurement. These pin the opposite rule: the device wins, and the collision
is reported once rather than per frame.
"""
from __future__ import annotations

import logging

from telemetry_engine.main import merge_state


def test_state_is_merged_alongside_the_devices_own_channels():
    channels = {"position": 7.0}
    merge_state("dut", channels, {"current_step": "move_to", "test_status": "PASS"}, set())

    assert channels == {"position": 7.0, "current_step": "move_to", "test_status": "PASS"}


def test_a_measured_value_is_never_overwritten_by_published_state():
    """The whole point: a measurement exists nowhere else, while the published
    value is still on the run-state stream."""
    channels = {"position": 7.0}
    merge_state("dut", channels, {"position": 130.0}, set())

    assert channels == {"position": 7.0}


def test_the_rest_of_the_state_still_merges_around_a_collision():
    channels = {"position": 7.0}
    merge_state("dut", channels, {"position": 130.0, "current_step": "move_to"}, set())

    assert channels == {"position": 7.0, "current_step": "move_to"}


def test_a_collision_is_reported(caplog):
    with caplog.at_level(logging.WARNING, logger="telemetry_engine.main"):
        merge_state("dut", {"position": 7.0}, {"position": 130.0}, set())

    assert "position" in caplog.text
    assert "dut" in caplog.text


def test_a_collision_is_reported_once_not_once_per_frame(caplog):
    """At frame rates a per-frame warning would bury every other log line."""
    shadowed = set()
    with caplog.at_level(logging.WARNING, logger="telemetry_engine.main"):
        for _ in range(50):
            merge_state("dut", {"position": 7.0}, {"position": 130.0}, shadowed)

    assert len(caplog.records) == 1


def test_each_device_reports_its_own_collision(caplog):
    """The state stream is merged into every device the run declared, so the
    same bad name collides separately per device - and one device having
    reported it must not silence another."""
    shadowed = set()
    with caplog.at_level(logging.WARNING, logger="telemetry_engine.main"):
        merge_state("dut", {"voltage": 5.0}, {"voltage": 1.0}, shadowed)
        merge_state("power_supply", {"voltage": 12.0}, {"voltage": 1.0}, shadowed)

    assert len(caplog.records) == 2


def test_each_colliding_name_reports_separately(caplog):
    shadowed = set()
    with caplog.at_level(logging.WARNING, logger="telemetry_engine.main"):
        merge_state("dut", {"position": 7.0, "velocity": 1.0}, {"position": 130.0, "velocity": 0.0}, shadowed)

    assert len(caplog.records) == 2


def test_an_empty_state_leaves_the_channels_alone():
    channels = {"position": 7.0}
    merge_state("dut", channels, {}, set())

    assert channels == {"position": 7.0}
