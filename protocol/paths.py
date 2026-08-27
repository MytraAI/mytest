"""The on-disk output layout, shared by every process that writes to it.

Two processes write into this tree and they must agree on it without
importing each other (see the package docstring in wire.py):

- The testcase execution process writes its own verdict.json, at the end
  of its run, straight into that run's directory.
- The telemetry engine writes the telemetry CSVs, amends verdict.json
  with telemetry-completeness stats, and synthesizes a verdict for a run
  whose test process died without writing one.

Layout::

    <output_dir>/                             ~/Desktop/mytestresults by default
      runs/<test_id>/                         e.g. endurance_cycle_test_2026-08-17_14-30-12
        verdict.json              one authoritative record per test run
        <device>/telemetry.csv    wide: one row per frame, one column per channel
        <device>/logs.txt         that driver process's own detailed log
      raw/<device>/telemetry_<session>.csv

A run directory is named by its test_id, which new_test_id() composes from the
test's name and the time it started, so a run is identifiable from the file
tree alone.

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

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

DEFAULT_OUTPUT_DIR = Path.home() / "Desktop" / "mytestresults"
"""Where the engine writes and where a test process looks for its run
directory.

On the Desktop rather than inside the checkout, so an operator can find a run's
output without knowing where the code lives. A test never hardcodes this - it
reads the engine's actual output dir out of the heartbeat file (see
heartbeat.py), so the two can't disagree even if the engine was started with
--output-dir."""

RUN_TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"
"""How a run's start time is written into its directory name.

Separated for reading, but with '-' rather than ':' between the hour, minute
and second: a run directory has to survive being copied to a Windows machine or
onto a USB stick, and Finder renders a ':' in a filename as '/'. Ordered
largest unit first, so listing the results folder puts runs in the order they
happened."""

RUNS_DIRNAME = "runs"
RAW_DIRNAME = "raw"
VERDICT_FILENAME = "verdict.json"
TELEMETRY_FILENAME = "telemetry.csv"
DRIVER_LOG_FILENAME = "logs.txt"
DRIVER_CONSOLE_FILENAME = "console.txt"
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


_UNSAFE_IN_PATH = re.compile(r"[^A-Za-z0-9._-]+")

_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{n}" for n in range(1, 10)]
    + [f"LPT{n}" for n in range(1, 10)]
)
"""Names Windows refuses as a path component, with or without an extension.

A run filed under a directory called CON cannot be created on the machine that
would create it, and the failure is an OSError at copy time rather than
anything legible. Cheap to avoid, and this is the one function that knows a
string is about to become a path component."""


def safe_path_component(text: str, fallback: str) -> str:
    """`text` reduced to something that behaves as a single path component.

    Anything outside [A-Za-z0-9._-] becomes '-', which takes out the separators,
    the characters Windows refuses outright, and the trailing dot or space that
    Explorer silently strips. A name that reduces to nothing, or to something
    Windows reserves, becomes `fallback`.

    Shared by run directory names and by the operator's answers where those
    become directories on the results share - one place that knows what a path
    component may contain, rather than one per writer."""
    safe = _UNSAFE_IN_PATH.sub("-", text).strip("-.")
    if not safe or safe.split(".")[0].upper() in _WINDOWS_RESERVED:
        return fallback
    return safe


def new_test_id(test_name: str, when: Optional[datetime] = None) -> str:
    """An id for one run: the test's name, then when it started.

    This is the run directory's own name (see run_dir), so the test name is
    reduced to characters that behave as a single path component - anything
    else becomes '-'. The time carries seconds because the same test is
    routinely run several times a day and each run needs its own directory.

    Not a globally unique id: two runs of the same test starting within the
    same second would share one, and the engine would record them as a single
    run. Nothing in a manual test workflow produces that, but an automated
    caller that might should pass its own id."""
    stamp = (when or datetime.now()).strftime(RUN_TIMESTAMP_FORMAT)
    return f"{safe_path_component(test_name, 'test')}_{stamp}"


def ensure_output_dir(output_dir: Path) -> Path:
    """Create the output root, and return it.

    Called by whoever writes into the tree first - in practice the engine at
    startup, which is what makes the results folder appear on the Desktop
    before any test has run."""
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def driver_console_path(output_dir: Path, test_id: str, device: str) -> Path:
    """Where a driver's raw stdout and stderr are captured, beside its own log.

    A second file rather than more of logs.txt, because these are two different
    things written by two different writers. logs.txt is this project's record,
    formatted and timestamped, and now carries an unhandled traceback too (see
    driver_logging.capture_crashes). This one is whatever the process emitted at the
    file-descriptor level - a vendor library's warnings, anything printed before
    logging was configured, an interpreter message on the way down - which nothing in
    Python is in a position to reformat, and which would otherwise exist only in the
    terminal of whoever started the run."""
    return device_dir(output_dir, test_id, device) / DRIVER_CONSOLE_FILENAME
