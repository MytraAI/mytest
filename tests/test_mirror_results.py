"""Copying finished runs to the results share.

The mirror is the one participant that touches the network, and it runs
unattended on a schedule nobody watches. So what is pinned here is mostly what
it must NOT do: copy a run twice, copy one that is still being written, leave a
half-copied run where a reader will find it, copy numbers nobody measured, or
lose a run because it could not be filed neatly.

The other half is where a run lands. That path is built from two strings an
operator typed, and it is the only thing that makes a stored run findable
later.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from protocol.mirror_status import MirrorStatus, describe_for_operator
from tools import mirror_results
from tools.mirror_results import (
    NO_DUT,
    NO_DUT_SERIAL,
    NO_ER_TICKET,
    PARTIAL_PREFIX,
    copy_run,
    destination,
    is_finished,
    pending_runs,
    run_once,
    skip_reason,
)

SHARE = Path("/share")


def verdict(**overrides) -> dict:
    base = dict(
        test_id="t", test_name="zdrive_brake_hold_test", lifecycle="COMPLETED",
        bounds_result="PASS", started_at=0.0, ended_at=1.0, dut="zdrive",
        used_mock=False, completeness={"frame_count": 10},
        metadata={"er_ticket": "ER-64", "dut_serial_number": "ZDRIVE2IN"},
    )
    base.update(overrides)
    return base


def write_run(runs: Path, test_id: str, data: dict, age_s: float = 0.0) -> Path:
    run = runs / test_id
    (run / "odrive").mkdir(parents=True)
    (run / "odrive" / "telemetry.csv").write_text("t,ibus\n0.0,1.2\n")
    (run / "odrive" / "logs.txt").write_text("driver said something\n")
    path = run / "verdict.json"
    path.write_text(json.dumps(data))
    if age_s:
        os.utime(path, (time.time() - age_s, time.time() - age_s))
    return run


# --- where a run is filed -------------------------------------------------------


def test_a_run_is_filed_by_dut_then_ticket_then_unit():
    assert destination(SHARE, verdict(), "run-1") == (
        SHARE / "zdrive" / "ER-64" / "ZDRIVE2IN" / "runs" / "run-1"
    )


def test_a_run_nobody_attributed_is_still_filed_somewhere():
    """Ctrl+C during setup ends a run before its operator prompt, and that run has
    telemetry that usually says why somebody killed it."""
    assert destination(SHARE, verdict(metadata={}), "run-1") == (
        SHARE / "zdrive" / NO_ER_TICKET / NO_DUT_SERIAL / "runs" / "run-1"
    )


def test_a_verdict_the_engine_synthesised_has_no_dut_and_is_still_filed():
    """The engine writes a verdict for a run whose process died without one. It
    knows the run's id and not the class that was running it."""
    assert destination(SHARE, verdict(dut="", metadata={}), "run-1") == (
        SHARE / NO_DUT / NO_ER_TICKET / NO_DUT_SERIAL / "runs" / "run-1"
    )


def test_the_operators_answers_are_reduced_to_one_path_component():
    """Both are typed, and both become directories - a separator in either would
    otherwise nest a run somewhere nobody will look for it."""
    filed = destination(
        SHARE, verdict(metadata={"er_ticket": "ER-64/old", "dut_serial_number": "A B"}), "r",
    )
    assert filed == SHARE / "zdrive" / "ER-64-old" / "A-B" / "runs" / "r"


# --- what is copied, and what is left alone -------------------------------------


@pytest.mark.parametrize("data,expected", [
    (verdict(), ""),
    (verdict(used_mock=True), "drove a mock backend"),
    (verdict(dut="example_dut"), "example_dut runs are not mirrored"),
])
def test_which_runs_are_mirrorable(data, expected):
    assert skip_reason(data) == expected


def test_a_run_from_before_the_dut_identifier_is_left_alone():
    """Absent, not empty. A verdict that never had the key cannot be filed by DUT
    and predates the share; an empty one came from the engine and is filed."""
    old = verdict()
    del old["dut"]
    assert skip_reason(old) == "predates the DUT identifier"
    assert skip_reason(verdict(dut="")) == ""


def test_a_run_is_finished_when_the_engine_says_so(tmp_path):
    path = tmp_path / "verdict.json"
    path.write_text("{}")
    assert is_finished(verdict(), path)


def test_a_run_the_engine_has_not_finalised_waits(tmp_path):
    """completeness is stamped when the run's stream goes quiet. Until then the
    engine may still be writing telemetry into that directory."""
    path = tmp_path / "verdict.json"
    path.write_text("{}")
    assert not is_finished(verdict(completeness=None), path)


def test_a_run_no_engine_will_ever_finalise_is_copied_anyway(tmp_path):
    """An engine killed outright never stamps completeness. Keying off a signal
    one process owns means handling that process never arriving."""
    path = tmp_path / "verdict.json"
    path.write_text("{}")
    old = time.time() - mirror_results.STALE_VERDICT_S - 1
    os.utime(path, (old, old))
    assert is_finished(verdict(completeness=None), path)


def test_a_run_still_being_written_has_no_verdict_to_find(tmp_path):
    """The test process writes its verdict at the end, so a directory without one
    is a run in progress - it is not skipped, it is not seen.

    Checked alongside a finished run, so this cannot pass by pending_runs finding
    nothing at all."""
    runs = tmp_path / "runs"
    (runs / "in-progress" / "odrive").mkdir(parents=True)
    write_run(runs, "finished", verdict())

    found = pending_runs(tmp_path, tmp_path / "share")

    assert [run.name for run, _ in found] == ["finished"]


# --- copying ---------------------------------------------------------------------


def test_a_copied_run_is_complete(tmp_path):
    runs = tmp_path / "runs"
    source = write_run(runs, "run-1", verdict())
    dest = tmp_path / "share" / "runs" / "run-1"

    copy_run(source, dest)

    assert (dest / "verdict.json").exists()
    assert (dest / "odrive" / "telemetry.csv").read_text() == "t,ibus\n0.0,1.2\n"
    assert (dest / "odrive" / "logs.txt").exists(), "the driver's own log is part of the run"


def test_the_destination_appears_whole_or_not_at_all(tmp_path, monkeypatch):
    """A copy that stops early must not leave a truncated telemetry.csv where a
    reader expects a complete one - a truncated CSV is a quiet way to be wrong."""
    runs = tmp_path / "runs"
    source = write_run(runs, "run-1", verdict())
    dest = tmp_path / "share" / "runs" / "run-1"

    def truncating_copytree(src, dst):
        Path(dst, "odrive").mkdir(parents=True)
        Path(dst, "odrive", "telemetry.csv").write_text("t,ibus\n")  # stopped early
        Path(dst, "odrive", "logs.txt").write_text("driver said something\n")
        Path(dst, "verdict.json").write_text(Path(src, "verdict.json").read_text())

    monkeypatch.setattr(mirror_results.shutil, "copytree", truncating_copytree)

    with pytest.raises(OSError, match="did not copy whole"):
        copy_run(source, dest)

    assert not dest.exists(), "a short copy was published"
    assert not list(dest.parent.glob(f"{PARTIAL_PREFIX}*")), "the partial copy was left behind"


def test_a_partial_left_by_an_interrupted_pass_is_redone(tmp_path):
    """It cannot be told apart from one that stopped mid-file, so it is not
    resumed."""
    runs = tmp_path / "runs"
    source = write_run(runs, "run-1", verdict())
    dest = tmp_path / "share" / "runs" / "run-1"
    partial = dest.parent / f"{PARTIAL_PREFIX}run-1"
    partial.mkdir(parents=True)
    (partial / "junk-from-last-time.csv").write_text("x")

    copy_run(source, dest)

    assert not (dest / "junk-from-last-time.csv").exists()
    assert not partial.exists()


# --- a whole pass -----------------------------------------------------------------


def test_a_pass_copies_what_is_outstanding_and_nothing_else(tmp_path):
    runs = tmp_path / "runs"
    share = tmp_path / "share"
    write_run(runs, "real", verdict())
    write_run(runs, "mocked", verdict(used_mock=True))
    write_run(runs, "unfinished", verdict(completeness=None))

    status = run_once(tmp_path, share)

    assert status.reachable and status.mirrored == 1 and status.outstanding == 0
    assert (share / "zdrive" / "ER-64" / "ZDRIVE2IN" / "runs" / "real").is_dir()
    assert not (share / "zdrive" / "ER-64" / "ZDRIVE2IN" / "runs" / "mocked").exists()


def test_running_again_copies_nothing(tmp_path):
    """The share is the state - a run is mirrored if its destination exists - so
    there is no ledger to lose when a box is reimaged."""
    runs = tmp_path / "runs"
    share = tmp_path / "share"
    write_run(runs, "real", verdict())

    run_once(tmp_path, share)
    second = run_once(tmp_path, share)

    assert second.mirrored == 0 and second.outstanding == 0


def test_a_backlog_drains_oldest_first(tmp_path):
    """The order somebody looking for the runs expects them to appear in."""
    runs = tmp_path / "runs"
    write_run(runs, "newer", verdict(), age_s=10)
    write_run(runs, "older", verdict(), age_s=1000)

    assert [run.name for run, _ in pending_runs(tmp_path, tmp_path / "share")] == ["older", "newer"]


def test_an_unreachable_share_stops_nothing_and_is_reported(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    write_run(runs, "real", verdict())
    monkeypatch.setattr(mirror_results, "check_reachable", lambda root: "no credential")

    status = run_once(tmp_path, tmp_path / "share")

    assert not status.reachable
    assert status.error == "no credential"
    assert status.outstanding == 1, "the backlog is what the operator needs told"


def test_a_dry_run_copies_nothing(tmp_path):
    runs = tmp_path / "runs"
    share = tmp_path / "share"
    write_run(runs, "real", verdict())

    status = run_once(tmp_path, share, dry_run=True)

    assert status.mirrored == 0 and status.outstanding == 1
    # The exact destination, not a glob over a tree that might not exist at all.
    assert not destination(share, verdict(), "real").exists()


# --- what the operator is told ------------------------------------------------------


def test_nothing_is_said_when_the_mirror_is_healthy():
    assert describe_for_operator(MirrorStatus(time.time(), "//nas/x", True)) is None


def test_a_mirror_that_has_never_run_is_the_loudest_case():
    """The failure a live share check cannot see: the share is fine and nothing
    is copying to it."""
    said = describe_for_operator(None)
    assert "has never run on this machine" in said
    assert "Setup-StandBox.ps1 -ResultsShareOnly" in said


def test_a_mirror_that_has_stopped_reporting_says_when_it_last_managed_a_pass():
    """Not "it has stopped", which is more than is known: a task registered
    without its repeating trigger runs at logon and no more."""
    said = describe_for_operator(MirrorStatus(time.time() - 3600, "//nas/x", True))
    assert "last completed a pass 60 min ago" in said
    assert "Setup-StandBox.ps1 -ResultsShareOnly" in said


def test_an_unreachable_share_says_so_and_says_the_run_is_safe():
    said = describe_for_operator(MirrorStatus(time.time(), "//nas/x", False, error="denied"))
    assert "cannot be reached" in said and "denied" in said
    assert "recorded on this machine either way" in said, "the reassurance is the point"


def test_a_down_share_is_not_asked_what_it_already_has(tmp_path, monkeypatch):
    """A stat against a dead SMB server does not fail fast, it blocks for the
    session timeout - so doing it once per run is how counting a backlog becomes
    a pass that never finishes.

    Shown by the count: this run IS already on the share, and it is still
    reported outstanding, because the share was never consulted."""
    runs = tmp_path / "runs"
    share = tmp_path / "share"
    write_run(runs, "already-there", verdict())
    run_once(tmp_path, share)  # gets it onto the share
    monkeypatch.setattr(mirror_results, "check_reachable", lambda root: "down")

    assert run_once(tmp_path, share).outstanding == 1


def test_a_reachable_share_is_asked_and_answers(tmp_path):
    """The contrast: with the share up, a run already on it is not outstanding."""
    runs = tmp_path / "runs"
    share = tmp_path / "share"
    write_run(runs, "already-there", verdict())
    run_once(tmp_path, share)

    assert pending_runs(tmp_path, share) == []
    assert len(pending_runs(tmp_path, share, check_destination=False)) == 1


def test_many_failures_are_summarised_rather_than_concatenated(tmp_path, monkeypatch):
    """The error text ends up in a dialog. Every failure is logged in full on the
    way past, so nothing is lost by capping what is reported."""
    runs = tmp_path / "runs"
    for n in range(5):
        write_run(runs, f"run-{n}", verdict())
    monkeypatch.setattr(
        mirror_results, "copy_run",
        lambda src, dest: (_ for _ in ()).throw(OSError("nope")),
    )

    status = run_once(tmp_path, tmp_path / "share")

    assert status.mirrored == 0 and status.outstanding == 5
    assert status.error.endswith("and 2 more")


def test_a_run_directory_windows_would_refuse_is_still_filable():
    """A name the far side rejects is a run that fails on every pass forever, and
    a permanently-failing run quietly poisons the backlog the prompt reports."""
    filed = destination(SHARE, verdict(), "CON")
    assert filed.name != "CON"
    assert filed == SHARE / "zdrive" / "ER-64" / "ZDRIVE2IN" / "runs" / "_UNNAMED-RUN"


def test_a_well_formed_run_id_is_untouched():
    """The reduction has to be identity for every id new_test_id() produces."""
    test_id = "zdrive_brake_hold_test_2026-08-27_10-00-00"
    assert destination(SHARE, verdict(), test_id).name == test_id


def test_a_backlog_that_will_not_clear_says_why():
    said = describe_for_operator(
        MirrorStatus(time.time(), "//nas/x", True, outstanding=2, error="disk full")
    )
    assert "disk full" in said and "2 finished run(s)" in said


def test_every_warning_body_says_where_the_run_is_and_how_to_fix_it():
    """Whatever is wrong, the body has to answer both questions the headline
    raises - is my data safe, and what do I do - since the headline itself only
    shouts that the run is local."""
    states = [
        None,
        MirrorStatus(time.time() - 99_999, "//nas/x", True),
        MirrorStatus(time.time(), "//nas/x", False, error="denied"),
    ]
    for status in states:
        said = describe_for_operator(status)
        assert "recorded on this machine either way" in said, said
        assert "Setup-StandBox.ps1 -ResultsShareOnly" in said, said
