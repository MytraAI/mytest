"""Logging setup shared by every hardware driver entry point: console at INFO,
and optionally a detailed file at DEBUG.

Lives here rather than in each device's main.py for the same reason runner.py
does - it is process wiring, identical whatever device is being driven, and a
driver that logged differently from its siblings would make a stand's output
harder to read rather than easier.

THE FILE IS THE RECORD; THE CONSOLE IS FOR WHOEVER IS WATCHING. A driver
process usually has no one attached to its stdout: it is started as a
subprocess by a testbed, and its output goes nowhere anybody reads. So the file
gets DEBUG - every command executed, every decoded fault transition, connect
and teardown detail - and the console keeps INFO so a person running a driver by
hand still sees the useful lines without the noise.

Given `--log-file`, that file is written next to the device's telemetry for the
run (see protocol/paths.py's driver_log_path). The driver is handed the path and
knows nothing about what the directory means; composing it belongs to whoever
started the process, which is the only participant that knows a run is
happening.

WHAT IS DELIBERATELY NOT LOGGED: anything per-frame. No backend logs a
telemetry frame, and none should start - at 12-30 Hz that turns a detailed log
into an unreadable second copy of the CSV. Everything here is episodic: it fires
on a command, a state change, or a failure. That is what keeps DEBUG affordable
for a run lasting hours.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Optional

PROJECT_LOGGERS = ("hardware", "protocol", "testcases", "telemetry_engine", "testbeds", "__main__")
"""The top-level packages whose loggers are raised to DEBUG for the file. A
package is listed by name rather than the root logger being lowered, so a
dependency's debug output never lands in a driver's log - see configure().

`__main__` is here because a driver run as `python -m hardware.<device>.main`
logs under that name, so it is the entry point's own logger rather than a
stray."""

CONSOLE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
FILE_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-8s %(name)s: %(message)s"
FILE_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
"""Milliseconds in the file but not the console: correlating a logged fault
against a telemetry frame's `t` needs better than one-second resolution, and
the whole point of putting this file beside telemetry.csv is that the two can be
read together."""


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

    Appends rather than truncates, so a driver restarted mid-run adds to the
    same file instead of erasing what the previous process recorded - the
    failure that killed it is usually the interesting part. Each process writes
    a header line naming itself, so a file holding two attempts is readable as
    two attempts.

    A file that cannot be opened is a warning, not a failure: losing the log is
    not a reason to refuse to drive the hardware."""
    # DEBUG is raised on this project's own loggers, NOT on the root logger.
    # Root at DEBUG would enable it for every dependency too: asyncio announces
    # its event-loop selector, and the odrive package sits on pyusb/libusb,
    # which can be extremely chatty per transfer. A detailed log is only useful
    # if it is this driver's detail rather than its dependencies', and at 12 Hz
    # of USB traffic the difference is the file being readable or not.
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
