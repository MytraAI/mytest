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

The raw stream lives outside runs/ because it carries no test_id - only the
Telemetry Publisher tags frames - so routing it per run would mean inventing
the raw/tagged seq correlation the architecture doc leaves open. It's the
continuous instrument record whose point is not depending on the test
process; recover a run's slice by time range from the verdict.
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
