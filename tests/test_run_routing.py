"""Where a device's frames go, and why every frame lands exactly once.

The engine attributes frames itself rather than having the testcase process
relay them, which is what removed the double-write the old design implied. The
whole routing rule is:

  | frame from device D        | destination                          |
  |----------------------------|--------------------------------------|
  | run open, D declared by it | that run's directory, state merged   |
  | run open, D not declared   | the per-session record               |
  | no run open                | the per-session record               |

These pin that table, and the two properties that fall out of it: nothing is
written twice, and nothing falls in a gap - including across the moment a test
process dies, which is the window the per-session record exists to catch.
"""
from __future__ import annotations

import asyncio

from protocol.paths import verdict_path
from protocol.verdict import BoundsResult, Lifecycle, Verdict, read_verdict, write_verdict
from protocol.wire import RunStateFrame, TelemetryFrame
from telemetry_engine.run_recorder import RunRecorder
from telemetry_engine.wide_csv_storage import WideCsvTelemetryStorage

STALENESS_S = 5.0


def make_recorder(tmp_path):
    storage = WideCsvTelemetryStorage(tmp_path, "sess")
    return RunRecorder(tmp_path, storage, staleness_s=STALENESS_S)


def state(test_id="run1", devices=("odrive",), values=None):
    return RunStateFrame(
        test_id=test_id,
        test_name="endurance_cycle_test",
        devices=list(devices),
        state=values or {},
        t=0.0,
    )


def frame(device="odrive", seq=0):
    return TelemetryFrame(seq=seq, t=float(seq), channels={"a": 1.0}, device=device)


# ---- the routing table -----------------------------------------------------


def test_no_open_run_means_no_attribution(tmp_path):
    recorder = make_recorder(tmp_path)

    assert recorder.route("odrive", now=100.0) is None


def test_a_declared_device_is_attributed_to_the_open_run(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.observe_state(state(devices=("odrive", "daq")), now=100.0)

    assert recorder.route("odrive", now=100.0)[0] == "run1"
    assert recorder.route("daq", now=100.0)[0] == "run1"


def test_an_undeclared_device_stays_in_the_session_record(tmp_path):
    """A device streaming during someone else's run must not be attributed to
    it - that would put data in a run directory the run never used."""
    recorder = make_recorder(tmp_path)
    recorder.observe_state(state(devices=("odrive",)), now=100.0)

    assert recorder.route("odrive", now=100.0)[0] == "run1"
    assert recorder.route("power_supply", now=100.0) is None


def test_attribution_stops_when_the_state_stream_goes_quiet(tmp_path):
    """The continuity property. When a test process dies its state stream
    stops, so every device reverts to the per-session record and frames keep
    landing - no buffer, no switch-over window, no hole."""
    recorder = make_recorder(tmp_path)
    recorder.observe_state(state(devices=("odrive",)), now=100.0)

    assert recorder.route("odrive", now=100.0 + STALENESS_S - 0.1)[0] == "run1"
    assert recorder.route("odrive", now=100.0 + STALENESS_S) is None


# ---- what rides along on an attributed row ---------------------------------


def test_published_state_is_available_to_merge_into_attributed_rows(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.observe_state(state(values={"current_step": "move_to", "test_status": "PASS"}), now=100.0)

    assert recorder.route("odrive", now=100.0)[1] == {"current_step": "move_to", "test_status": "PASS"}


def test_state_is_scoped_to_the_open_run(tmp_path):
    """Rows are only ever attributed to the run that is actually open, so its
    state cannot leak onto another run's rows."""
    recorder = make_recorder(tmp_path)
    recorder.observe_state(state(test_id="run1", values={"current_step": "a"}), now=100.0)

    test_id, values = recorder.route("odrive", now=100.0)
    assert (test_id, values) == ("run1", {"current_step": "a"})


def test_the_latest_state_wins(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.observe_state(state(values={"current_step": "move_to"}), now=100.0)
    recorder.observe_state(state(values={"current_step": "cycle_position"}), now=100.1)

    assert recorder.route("odrive", now=100.1)[1]["current_step"] == "cycle_position"


# ---- run succession --------------------------------------------------------


def a_verdict(test_id):
    return Verdict(
        test_id=test_id,
        test_name="endurance_cycle_test",
        lifecycle=Lifecycle.STOPPED,
        bounds_result=BoundsResult.PASS,
        started_at=0.0,
        ended_at=1.0,
    )


def test_a_new_test_id_finalizes_the_previous_run(tmp_path):
    """Two runs never overlap on one stand, so a new run announcing itself is
    proof the old one is done - it doesn't have to wait out staleness."""
    recorder = make_recorder(tmp_path)
    write_verdict(a_verdict("run1"), tmp_path)

    recorder.observe_state(state(test_id="run1"), now=100.0)
    recorder.observe(frame(), "run1", now=100.0)
    recorder.observe_state(state(test_id="run2"), now=101.0)
    asyncio.run(recorder.reconcile(now=101.0))  # well inside run2's window

    assert read_verdict(verdict_path(tmp_path, "run1")).completeness["frame_count"] == 1
    assert recorder.route("odrive", now=101.0)[0] == "run2"


def test_the_superseding_run_is_not_itself_finalized_early(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.observe_state(state(test_id="run1"), now=100.0)
    recorder.observe_state(state(test_id="run2"), now=101.0)

    asyncio.run(recorder.reconcile(now=101.0))

    assert not verdict_path(tmp_path, "run2").exists()


def test_frames_for_a_finalized_run_are_ignored(tmp_path):
    """Attribution has already stopped by then, but the guard matters: a
    straggler must not resurrect a track and overwrite a correct record."""
    recorder = make_recorder(tmp_path)
    write_verdict(a_verdict("run1"), tmp_path)
    recorder.observe_state(state(), now=100.0)
    recorder.observe(frame(seq=0), "run1", now=100.0)
    asyncio.run(recorder.reconcile(now=200.0))

    recorder.observe(frame(seq=99), "run1", now=300.0)
    asyncio.run(recorder.reconcile(now=400.0))

    assert read_verdict(verdict_path(tmp_path, "run1")).completeness["frame_count"] == 1


def test_a_finalized_runs_state_frames_do_not_reopen_it(tmp_path):
    """A late state frame - the publisher's last message in flight - must not
    reopen a run whose files are already closed."""
    recorder = make_recorder(tmp_path)
    write_verdict(a_verdict("run1"), tmp_path)
    recorder.observe_state(state(), now=100.0)
    asyncio.run(recorder.reconcile(now=200.0))

    recorder.observe_state(state(), now=201.0)

    assert recorder.route("odrive", now=201.0) is None


def test_a_crashed_run_with_no_frames_reports_its_announced_window(tmp_path):
    """A test can declare devices that never stream - an acquisition device
    idles until acquisition is started - so a run can open, die, and have
    produced no frames at all. Its span then comes from how long it announced
    itself for, rather than defaulting to the epoch and claiming it ran in 1970.
    """
    recorder = make_recorder(tmp_path)
    opened = RunStateFrame(
        test_id="run1", test_name="endurance_cycle_test", devices=["daq"], state={}, t=1_700_000_000.0
    )
    later = RunStateFrame(
        test_id="run1", test_name="endurance_cycle_test", devices=["daq"], state={}, t=1_700_000_009.0
    )
    recorder.observe_state(opened, now=100.0)
    recorder.observe_state(later, now=104.0)

    asyncio.run(recorder.reconcile(now=200.0))

    verdict = read_verdict(verdict_path(tmp_path, "run1"))
    assert verdict.lifecycle == Lifecycle.CRASHED
    assert verdict.started_at == 1_700_000_000.0
    assert verdict.ended_at == 1_700_000_009.0
    assert verdict.completeness["frame_count"] == 0
    assert verdict.test_name == "endurance_cycle_test"  # known even with no frames


def test_recorded_frames_still_win_over_the_announced_window(tmp_path):
    """When there are frames, the span is theirs - the state window is only a
    fallback, not a replacement."""
    recorder = make_recorder(tmp_path)
    recorder.observe_state(
        RunStateFrame(test_id="run1", test_name="t", devices=["odrive"], state={}, t=1_700_000_000.0),
        now=100.0,
    )
    recorder.observe(TelemetryFrame(seq=0, t=50.0, channels={"a": 1}, device="odrive"), "run1", now=100.0)
    recorder.observe(TelemetryFrame(seq=1, t=60.0, channels={"a": 1}, device="odrive"), "run1", now=100.0)

    asyncio.run(recorder.reconcile(now=200.0))

    verdict = read_verdict(verdict_path(tmp_path, "run1"))
    assert (verdict.started_at, verdict.ended_at) == (50.0, 60.0)
