"""The engine's liveness heartbeat and the test-side abort it drives.

A test's whole product is its record, and both real-hardware test cases run
indefinitely, so a test that keeps moving hardware after recording stopped
spends real mechanical wear producing nothing recoverable. These pin the
three behaviours that prevent that: refuse to start with no recorder, abort
if the recorder disappears mid-run, and stay out of the way entirely for
callers that deliberately run without one.

Note what is deliberately *not* tested here, because it isn't the design: a
lost recorder does not mean lost safety monitoring. The LiveRulebookRunner
keeps evaluating from its own subscription throughout.
"""
from __future__ import annotations

import json

import pytest

from protocol import heartbeat
from testcases.base import DeviceNotRecorded, RecordingLost, TestCase


class MinimalTest(TestCase):
    """A TestCase with the abstract methods stubbed - these tests exercise
    the recording checks, not a lifecycle."""

    TEST_NAME = "minimal_test"

    def pre_test_setup(self):
        pass

    def main_execution(self):
        pass

    def post_test_teardown(self):
        pass


@pytest.fixture
def beat_path(tmp_path, monkeypatch):
    """Redirect the heartbeat to a temp path, so a real engine running on
    this machine can't influence the result (and vice versa)."""
    path = tmp_path / "mytest-engine.json"
    monkeypatch.setattr(heartbeat, "heartbeat_path", lambda: path)
    return path


def test_write_read_clear_round_trip(beat_path, tmp_path):
    heartbeat.write_heartbeat(tmp_path / "data")

    beat = heartbeat.read_heartbeat()
    assert beat is not None
    assert beat.output_dir == str(tmp_path / "data")
    assert beat.pid > 0
    assert beat.is_fresh()

    heartbeat.clear_heartbeat()
    assert heartbeat.read_heartbeat() is None


def test_absent_and_corrupt_are_both_simply_not_recording(beat_path, tmp_path):
    assert heartbeat.read_heartbeat() is None
    beat_path.write_text("{not json")
    assert heartbeat.read_heartbeat() is None
    beat_path.write_text(json.dumps({"pid": 1}))  # missing required fields
    assert heartbeat.read_heartbeat() is None


def test_staleness_is_judged_against_the_embedded_timestamp(beat_path, tmp_path):
    heartbeat.write_heartbeat(tmp_path)
    beat = heartbeat.read_heartbeat()

    assert beat.is_fresh(now=beat.updated_at + 1.0)
    assert not beat.is_fresh(now=beat.updated_at + heartbeat.DEFAULT_STALE_AFTER_S + 1.0)
    assert beat.age_s(now=beat.updated_at + 4.0) == pytest.approx(4.0)


def test_check_recording_alive_passes_while_the_engine_is_fresh(beat_path, tmp_path):
    heartbeat.write_heartbeat(tmp_path)
    MinimalTest().check_recording_alive()  # no raise


def test_check_recording_alive_raises_when_the_heartbeat_is_gone(beat_path):
    with pytest.raises(RecordingLost, match="heartbeat is gone"):
        MinimalTest().check_recording_alive()


def test_check_recording_alive_raises_when_the_heartbeat_is_stale(beat_path, tmp_path):
    heartbeat.write_heartbeat(tmp_path)
    stale = json.loads(beat_path.read_text())
    stale["updated_at"] -= heartbeat.DEFAULT_STALE_AFTER_S * 2
    beat_path.write_text(json.dumps(stale))

    with pytest.raises(RecordingLost, match="stale"):
        MinimalTest().check_recording_alive()


def test_require_engine_false_opts_out_entirely(beat_path):
    """The demos legitimately run with no engine and want no record."""
    test = MinimalTest(require_engine=False)
    test.check_recording_alive()
    test.require_recording_started()


def test_refuses_to_start_with_no_recorder(beat_path):
    with pytest.raises(RecordingLost, match="no telemetry engine is running"):
        MinimalTest().require_recording_started()


def test_output_dir_is_taken_from_the_engine_not_hardcoded(beat_path, tmp_path):
    """Both processes must agree on where the run directory is, even when
    the engine was started with a non-default --output-dir."""
    engine_dir = tmp_path / "somewhere" / "else"
    heartbeat.write_heartbeat(engine_dir)

    test = MinimalTest()
    test._resolve_output_dir()

    assert test._output_dir == engine_dir


def test_output_dir_falls_back_to_the_default_with_no_engine(beat_path):
    from protocol.paths import DEFAULT_OUTPUT_DIR

    test = MinimalTest(require_engine=False)
    test._resolve_output_dir()

    assert test._output_dir == DEFAULT_OUTPUT_DIR


def test_recording_checks_stop_once_teardown_begins(beat_path, tmp_path):
    """Teardown steps run through @step like any other, so a dead engine
    would otherwise abort each one individually - leaving the axis
    un-idled, exactly the state teardown exists to prevent. By then the
    verdict is already written, so liveness has nothing left to protect.

    Regression test: this was observed for real, by killing the engine
    during a mock ydrive run and watching YdriveTestbed's "move to
    position 0" idle step fail."""
    test = MinimalTest()
    with pytest.raises(RecordingLost):
        test.check_recording_alive()

    test._tearing_down = True
    test.check_recording_alive()  # no raise


def test_recording_lost_does_not_linger(beat_path, tmp_path):
    """Lingering exists so an operator can read the result off the status
    page. A recorder dying mid-run is unattended infrastructure failure,
    and the point of aborting was to stop consuming hardware - which a
    process hanging on a Ctrl+C nobody will press defeats.

    Regression test: the first implementation lingered here, leaving the
    aborted run's process alive indefinitely."""
    heartbeat.write_heartbeat(tmp_path)

    class AbortingTest(MinimalTest):
        def main_execution(self):
            heartbeat.clear_heartbeat()
            self.check_recording_alive()

    test = AbortingTest()
    # _wait_until_interrupted is what lingering would call; make it loud.
    test._wait_until_interrupted = lambda: (_ for _ in ()).throw(
        AssertionError("must not linger after RecordingLost")
    )

    with pytest.raises(RecordingLost):
        test.run()


def test_require_engine_false_keeps_an_explicitly_set_output_dir(beat_path, tmp_path):
    """A caller that opted out of engine-mediated recording manages its own
    output location. Redirecting it at whatever engine happens to be running
    would send its verdict somewhere it isn't looking.

    Regression test: found in review - the two demos that set _output_dir
    themselves were silently overridden whenever any engine was up."""
    heartbeat.write_heartbeat(tmp_path / "engine_dir")

    test = MinimalTest(require_engine=False)
    test._output_dir = tmp_path / "demo_dir"
    test._resolve_output_dir()

    assert test._output_dir == tmp_path / "demo_dir"


# ---- the device roster the heartbeat carries -------------------------------
#
# A run declares which devices it claims (TestCase.DEVICES). The engine
# advertises which it is subscribed to. If a test declares one the engine
# isn't recording, nothing would capture that device's frames and the run
# directory would come out quietly missing it - so that fails before setup,
# the same way a declared-but-absent channel does.


class TwoDeviceTest(MinimalTest):
    TEST_NAME = "two_device_test"
    DEVICES = ("odrive", "daq")


def test_heartbeat_carries_the_engines_device_roster(beat_path, tmp_path):
    heartbeat.write_heartbeat(tmp_path / "data", ["odrive", "daq"])

    beat = heartbeat.read_heartbeat()
    assert beat is not None
    assert beat.devices == ["odrive", "daq"]


def test_an_engine_with_no_roster_reads_back_as_empty_not_missing(beat_path, tmp_path):
    """Older heartbeats, and any writer that doesn't pass devices, must still
    parse - the field is additive, not required."""
    heartbeat.write_heartbeat(tmp_path / "data")

    beat = heartbeat.read_heartbeat()
    assert beat is not None and beat.devices == []


def test_starting_is_refused_when_a_declared_device_is_not_recorded(beat_path, tmp_path):
    heartbeat.write_heartbeat(tmp_path / "data", ["odrive"])  # daq is missing
    test_case = TwoDeviceTest()

    with pytest.raises(DeviceNotRecorded) as excinfo:
        test_case.require_recording_started()

    assert excinfo.value.missing == ["daq"]
    assert "daq" in str(excinfo.value)


def test_starting_proceeds_when_every_declared_device_is_recorded(beat_path, tmp_path):
    heartbeat.write_heartbeat(tmp_path / "data", ["odrive", "daq", "power_supply"])

    TwoDeviceTest().require_recording_started()  # extra coverage is fine; missing coverage isn't


def test_a_test_declaring_no_devices_is_never_blocked(beat_path, tmp_path):
    """The base cases and the demos declare nothing, and must still run."""
    heartbeat.write_heartbeat(tmp_path / "data", [])

    MinimalTest().require_recording_started()
