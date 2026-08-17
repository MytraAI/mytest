"""The on-disk output layout, shared by every process that writes to it.

Two processes write into this tree and they must agree on it without
importing each other (see the package docstring in wire.py):

- The testcase execution process writes its own verdict.json, at the end
  of its run, straight into that run's directory.
- The telemetry engine writes the telemetry CSVs, amends verdict.json
  with telemetry-completeness stats, and synthesizes a verdict for a run
  whose test process died without writing one.

Layout::

    <output_dir>/
      runs/<test_id>/
        verdict.json              one authoritative record per test run
        <device>/telemetry.csv    wide: one row per frame, one column per channel
      raw/<device>/telemetry_<session>.csv

One directory per run, one subdirectory per device inside it. Per-device
because devices sample at different rates, declare different channel sets,
and number `seq` independently, so a single table per run would have to
interpolate (inventing data) or pad most cells empty. Keeping them apart
preserves each device's native rate and leaves aligning them a deliberate
query on `t`. Subdirectories rather than filename suffixes, so a device can
gain more artifact types (status/error logs) without renaming anything.

The per-session files live outside runs/ because they hold exactly the frames
that belong to no run: everything a device published while no test claimed it,
including the window after a test process died and its state stream went quiet.
A frame is written to one place or the other, never both - the engine decides
which from the open run's declared devices (see
telemetry_engine/run_recorder.py). Recover an unattributed slice by time range.
"""
from __future__ import annotations

from pathlib import Path

DEFAULT_OUTPUT_DIR = Path("telemetry_engine/data")
"""Where the engine writes and where a test process looks for its run
directory. A test never hardcodes this - it reads the engine's actual
output dir out of the heartbeat file (see heartbeat.py), so the two
can't disagree even if the engine was started with --output-dir."""

RUNS_DIRNAME = "runs"
RAW_DIRNAME = "raw"
VERDICT_FILENAME = "verdict.json"
TELEMETRY_FILENAME = "telemetry.csv"
DRIVER_LOG_FILENAME = "logs.txt"
"""A driver process's own detailed log, written next to the telemetry it
produced. This is the second artifact type a device directory holds, and the
reason the layout above uses subdirectories rather than filename suffixes.

Written by the hardware driver process, which is the only participant that can
see its own device's failures in detail - a decoded ODrive fault, a refused
setpoint, a reconnect. None of that fits a telemetry channel: it is text, it is
episodic rather than sampled, and a column carrying it would be empty in almost
every row. Landing it in the run directory is what makes a recorded run
self-explaining, so "what happened at 03:12" is answerable from the stored
output alone rather than from whatever scrolled past in a terminal."""


def runs_dir(output_dir: Path) -> Path:
    return Path(output_dir) / RUNS_DIRNAME


def run_dir(output_dir: Path, test_id: str) -> Path:
    """The one directory holding everything about a single test run."""
    return runs_dir(output_dir) / test_id


def verdict_path(output_dir: Path, test_id: str) -> Path:
    return run_dir(output_dir, test_id) / VERDICT_FILENAME


def device_dir(output_dir: Path, test_id: str, device: str) -> Path:
    return run_dir(output_dir, test_id) / device


def run_telemetry_path(output_dir: Path, test_id: str, device: str) -> Path:
    """The per-device wide telemetry CSV for one run."""
    return device_dir(output_dir, test_id, device) / TELEMETRY_FILENAME


def raw_telemetry_path(output_dir: Path, device: str, session: str) -> Path:
    """The continuous, untagged per-device record. One file per engine
    session, since the raw stream has no test to be keyed by."""
    return Path(output_dir) / RAW_DIRNAME / device / f"telemetry_{session}.csv"


def driver_log_path(output_dir: Path, test_id: str, device: str) -> Path:
    """A driver's detailed log for one run, beside that device's telemetry.

    Composed here rather than by the driver, because a driver process knows
    nothing about runs and must not learn: it is handed a path to write and
    stays ignorant of what the directory means. Whoever starts the driver -
    a testbed, in PreTestSetup - is the participant that has both the run's
    test_id and the engine's output dir."""
    return device_dir(output_dir, test_id, device) / DRIVER_LOG_FILENAME
