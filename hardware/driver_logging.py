"""Logging setup shared by every hardware driver entry point: console at INFO,
and optionally a detailed file at DEBUG.

Lives here rather than in each device's main.py because it is process wiring,
identical whatever device is being driven - the same reason runner.py exists.

The file is the record; the console is for whoever is watching. A driver started
as a subprocess by a testbed has nobody attached to its stdout, so the file gets
DEBUG - every command executed, every decoded fault transition, connect and
teardown detail - while the console keeps INFO for a person running a driver by
hand.

Given `--log-file`, that file is written next to the device's telemetry for the
run (protocol/paths.py's driver_log_path). The driver is handed the path and
knows nothing about what the directory means; composing it belongs to whoever
started the process, being the only participant that knows a run is happening.

What is NOT logged: anything per-frame. No backend logs a telemetry frame, and
none should - at 12-30 Hz that makes the file an unreadable second copy of the
CSV. Everything here is episodic, firing on a command, a state change or a
failure, which is what keeps DEBUG affordable over a run lasting hours.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Optional

PROJECT_LOGGERS = ("hardware", "protocol", "testcases", "telemetry_engine", "testbeds", "__main__")
"""The top-level packages whose loggers are raised to DEBUG for the file. Listed
by name rather than lowering the root logger, so a dependency's debug output
never lands in a driver's log.

`__main__` is here because a driver run as `python -m hardware.<device>.main`
logs under that name."""

CONSOLE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
FILE_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-8s %(name)s: %(message)s"
FILE_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
"""Milliseconds in the file but not the console: correlating a logged fault
against a telemetry frame's `t` needs better than one-second resolution."""


def add_logging_args(parser: argparse.ArgumentParser) -> None:
    """Add `--log-file` to a driver's argument parser."""
    parser.add_argument(
        "--log-file",
        default=None,
        help=(
            "write a detailed (DEBUG) driver log here, in addition to the console. "
            "A testbed passes the run's per-device path (see protocol/paths.py's driver_log_path); "
            "omitted, the driver logs to the console only"
        ),
    )


def configure(log_file: Optional[str] = None, device: str = "unknown") -> Optional[Path]:
    """Set up console and (optionally) file logging. Returns the file path used.

    Appends rather than truncates, so a driver restarted mid-run adds to the file
    instead of erasing what the previous process recorded. Each process writes a
    header line naming itself, so a file holding two attempts reads as two
    attempts.

    A file that cannot be opened is a warning, not a failure: losing the log is
    not a reason to refuse to drive the hardware."""
    # DEBUG is raised on this project's own loggers, NOT on the root logger.
    # Root at DEBUG enables it for every dependency too: asyncio announces its
    # event-loop selector, and the odrive package sits on pyusb/libusb, which is
    # chatty per transfer. A detailed log is only useful if the detail is this
    # driver's rather than its dependencies'.
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for name in PROJECT_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
    root.addHandler(console)

    if log_file is None:
        return None

    path = Path(log_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "could not open the driver log file %s (%s) - continuing with console logging only", path, exc
        )
        return None

    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(FILE_FORMAT, datefmt=FILE_DATE_FORMAT))
    root.addHandler(handler)
    logging.getLogger(__name__).info("driver log for %s: %s", device, path)
    return path
