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
import faulthandler
import logging
import sys
import threading
from pathlib import Path
from typing import Optional

PROJECT_LOGGERS = ("hardware", "protocol", "testcases", "telemetry_engine", "testbeds", "__main__")
"""The top-level packages whose loggers are raised to DEBUG for the file. Listed
by name rather than lowering the root logger, so a dependency's debug output
never lands in a driver's log.

`__main__` is here because a driver run as `python -m hardware.<device>.main`
logs under that name."""

_faulthandler_file = None
"""The open file faulthandler writes a native crash dump to. Module-level purely to
keep it from being garbage-collected: faulthandler holds the descriptor, not the
object, and a closed file makes the dump land nowhere."""

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
    capture_crashes(path)
    logging.getLogger(__name__).info("driver log for %s: %s", device, path)
    return path


def capture_crashes(path: Optional[Path] = None) -> None:
    """Route what kills a driver into the driver's own log.

    THE REASON A DRIVER DIED IS THE ONE THING ITS LOG MUST CONTAIN, and by default it
    is the one thing that does not reach it. Python prints an unhandled exception
    through sys.excepthook, which writes a traceback straight to stderr and never
    touches logging - so nothing here sees it. A driver is started by a testbed with
    stderr inherited from whoever launched the test, so that traceback lands in a
    terminal scrollback: not in the run directory, not beside the telemetry it stops
    explaining, and gone when the window closes.

    That is not hypothetical. A 6 h 25 m zdrive endurance run on 2026-08-25 ended when
    the ODrive's USB transport failed and libodrive raised out of read_endpoints. The
    traceback naming it existed - on a terminal - while the run directory recorded only
    a telemetry stream that stopped mid-frame with no error of any kind, and the
    diagnosis had to be reconstructed from timestamps in four other files.

    Three hooks, because a driver can die three ways:

      - sys.excepthook: the main thread raising out of the top level.
      - threading.excepthook: any other thread. Vendor libraries run their own - the
        odrive package has an event loop thread and a native USB worker - and an
        exception on one of those never passes through the main thread at all.
      - faulthandler: a native fault. A C library reached through ctypes can take the
        interpreter down with no Python exception to catch, and this is the only thing
        that leaves a stack behind when it does. It also distinguishes a crash from a
        hang, which nothing in the recorded artifacts could.

    asyncio needs no hook: its default handler already reports through logging, so a
    task dying unretrieved lands in this file with everything else."""
    logger = logging.getLogger(__name__)

    def log_main_thread(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            # A person stopping a driver by hand is not a crash, and a traceback for
            # one buries the record of what it was doing when they did.
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("unhandled exception - this driver is going down",
                        exc_info=(exc_type, exc, tb))

    def log_other_thread(args):
        if issubclass(args.exc_type, SystemExit):
            return
        logger.critical("unhandled exception in thread %s - a driver can die on a thread "
                        "it did not start", args.thread.name if args.thread else "?",
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = log_main_thread
    threading.excepthook = log_other_thread

    global _faulthandler_file
    if path is None:
        faulthandler.enable()
        return
    try:
        _faulthandler_file = open(path, "a", encoding="utf-8", buffering=1)
    except OSError as exc:
        logger.warning("could not open %s for crash dumps (%s) - a native fault will "
                       "print to stderr only", path, exc)
        faulthandler.enable()
        return
    faulthandler.enable(file=_faulthandler_file)


def restore_crash_capture() -> None:
    """Undo capture_crashes().

    For a test that calls configure() in-process. These are interpreter-wide hooks, so
    left installed they outlive the test that installed them: pytest's own
    unhandled-thread-exception warning stops firing because threading.excepthook is no
    longer its own, and faulthandler goes on writing to a file the test has deleted.
    A driver process never needs this - it wants the hooks until it exits."""
    global _faulthandler_file
    sys.excepthook = sys.__excepthook__
    threading.excepthook = threading.__excepthook__
    faulthandler.disable()
    if _faulthandler_file is not None:
        _faulthandler_file.close()
        _faulthandler_file = None
