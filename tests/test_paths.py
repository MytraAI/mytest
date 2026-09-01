"""The output layout: where a run's results land, and what its directory is called."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from protocol.paths import (
    DEFAULT_OUTPUT_DIR,
    driver_log_path,
    ensure_output_dir,
    new_test_id,
    run_dir,
    safe_path_component,
    verdict_path,
)

WHEN = datetime(2026, 8, 17, 14, 30, 12)


def test_results_default_to_a_desktop_folder():
    """An operator finds a run's output without knowing where the code lives."""
    assert DEFAULT_OUTPUT_DIR == Path.home() / "Desktop" / "mytestresults"


def test_the_output_root_is_created_if_it_is_missing(tmp_path):
    root = tmp_path / "mytestresults"
    assert ensure_output_dir(root) == root
    assert root.is_dir()
    ensure_output_dir(root)  # an existing root is left alone rather than raising


def test_a_run_is_named_by_its_test_and_when_it_started():
    """The readable half of the id, which is what a person navigating the tree
    reads. The uuid that follows is checked separately below."""
    test_id = new_test_id("endurance_cycle_test", WHEN)
    assert test_id.startswith("endurance_cycle_test_2026-08-17_14-30-12_")


def test_two_runs_of_one_test_in_the_same_second_get_different_ids():
    """The id is the run directory's name, so a collision is not a duplicate
    label - it is two runs sharing one directory, both devices' frames
    attributed to it, and the second verdict overwriting the first. One run
    disappears into another and nothing reports it."""
    ids = {new_test_id("endurance_cycle_test", WHEN) for _ in range(1000)}
    assert len(ids) == 1000


def test_the_unique_half_is_a_uuid_and_is_path_safe():
    """A uuid4 hex, so it adds no character safe_path_component would have to
    reduce - the id stays usable verbatim as a directory name, and the results
    mirror copies it unchanged."""
    suffix = new_test_id("endurance_cycle_test", WHEN).rsplit("_", 1)[1]
    assert len(suffix) == 32
    assert all(c in "0123456789abcdef" for c in suffix)
    assert safe_path_component(suffix, "fallback") == suffix


def test_run_directories_list_in_the_order_they_happened():
    """Largest time unit first, so the plain alphabetical order the OS shows a
    folder in is also chronological."""
    earlier = new_test_id("t", datetime(2026, 8, 17, 9, 5, 0))
    later = new_test_id("t", datetime(2026, 8, 17, 14, 30, 12))
    next_year = new_test_id("t", datetime(2027, 1, 2, 3, 4, 5))
    assert sorted([next_year, later, earlier]) == [earlier, later, next_year]


def test_a_run_id_survives_being_copied_to_another_filesystem():
    """A ':' is legal on macOS but illegal on Windows, and Finder renders it as
    a '/' - a results folder that gets copied about must not carry one."""
    assert ":" not in new_test_id("endurance_cycle_test", WHEN)


def test_a_run_id_is_a_single_safe_path_component():
    """The id is used verbatim as a directory name, so a test name with a
    separator or a space in it must not become two directories, or a hidden
    one."""
    test_id = new_test_id(" ydrive / manual test! ", WHEN)
    assert "/" not in test_id and " " not in test_id
    assert not test_id.startswith(".")
    assert Path(test_id).name == test_id


def test_every_artifact_of_one_run_lands_in_that_run_directory(tmp_path):
    """The verdict, each device's telemetry and each driver's log share one
    directory, so a recorded run is self-explaining from the tree alone."""
    test_id = new_test_id("endurance_cycle_test", WHEN)
    directory = run_dir(tmp_path, test_id)

    assert verdict_path(tmp_path, test_id).parent == directory
    assert driver_log_path(tmp_path, test_id, "odrive").parent.parent == directory
    assert directory.name == test_id
