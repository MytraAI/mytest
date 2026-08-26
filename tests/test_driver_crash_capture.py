"""What kills a driver has to reach the driver's own log.

By default it does not. Python prints an unhandled exception through
sys.excepthook, straight to stderr, never touching logging - and a driver is
started by a testbed with stderr inherited from whoever launched the test, so
the traceback lands in a terminal scrollback rather than in the run directory.
That is how a 6 h 25 m zdrive run ended with its cause recorded nowhere: the
ODrive's USB transport failed, libodrive raised, and logs.txt showed only a
telemetry stream stopping mid-frame with no error at all.

These run real subprocesses, because that is the only way to test a process
dying. Nothing here is mocked: the interpreter really raises, really raises on a
thread it did not start, and really takes a segmentation fault.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from hardware.driver_process import start_driver
from protocol.paths import driver_console_path, driver_log_path


def _run(tmp_path: Path, body: str) -> str:
    """Run `body` in a driver-like subprocess and return what its log file holds."""
    log = tmp_path / "logs.txt"
    script = tmp_path / "driver_like.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(Path.cwd())!r})\n"
        "from hardware.driver_logging import configure\n"
        f"configure({str(log)!r}, device='test')\n"
        + body
    )
    subprocess.run([sys.executable, str(script)], capture_output=True, timeout=60)
    return log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""


def test_an_unhandled_exception_on_the_main_thread_lands_in_the_log(tmp_path):
    written = _run(tmp_path, "raise RuntimeError('transport failed')\n")

    assert "unhandled exception" in written
    assert "RuntimeError: transport failed" in written
    assert "Traceback" in written, "the stack is the part that names the cause"


def test_an_unhandled_exception_on_another_thread_lands_in_the_log(tmp_path):
    """The case that actually bit. A vendor library runs its own threads - the odrive
    package has an event loop thread and a native USB worker - and an exception on one
    of those never passes through the main thread, so sys.excepthook never sees it."""
    written = _run(tmp_path, (
        "import threading\n"
        "def boom():\n"
        "    raise RuntimeError('raised off the main thread')\n"
        "t = threading.Thread(target=boom, name='vendor-worker')\n"
        "t.start(); t.join()\n"
    ))

    assert "unhandled exception in thread vendor-worker" in written
    assert "RuntimeError: raised off the main thread" in written


def test_faulthandler_is_pointed_at_the_log_not_at_stderr():
    """A C library reached through ctypes can take the interpreter down with no Python
    exception to catch. faulthandler is the only thing that leaves a stack behind when it
    does - and it is what distinguishes a crash from a hang, which the recorded artifacts
    of the zdrive failure could not.

    Proven without crashing, by writing a stack through the descriptor faulthandler was
    handed and checking it lands in the driver's log. That descriptor is the whole of the
    wiring: dump_traceback() defaults to stderr and does NOT use it, so the only way to
    show where a fault would go is to write through it. The real fault is exercised by
    the test below, opt-in because a genuine segmentation fault raises the OS crash
    reporter on a developer's machine."""
    import faulthandler

    from hardware import driver_logging

    log = Path(tempfile.mkdtemp()) / "logs.txt"
    log.write_text("")
    try:
        driver_logging.capture_crashes(log)
        assert faulthandler.is_enabled(), "a native fault would leave nothing behind"
        registered = driver_logging._faulthandler_file
        assert registered is not None and registered.name == str(log)
        faulthandler.dump_traceback(file=registered)
        registered.flush()
    finally:
        driver_logging.restore_crash_capture()

    written = log.read_text()
    assert "most recent call first" in written, written
    assert "test_driver_crash_capture.py" in written, (
        "the stack went somewhere, but not into the driver's log"
    )


@pytest.mark.skipif(
    os.environ.get("MYTEST_CRASH_TESTS") != "1",
    reason="a real segmentation fault raises the OS crash reporter; set MYTEST_CRASH_TESTS=1",
)
def test_a_real_native_fault_leaves_a_stack_in_the_log(tmp_path):
    """The genuine article, opt-in. Everything above tests the wiring; this tests that a
    process actually dying of a native fault still says so in the driver's own log."""
    written = _run(tmp_path, (
        "import faulthandler\n"
        "faulthandler._sigsegv()\n"
    ))

    assert "Fatal Python error" in written or "Segmentation fault" in written, written[-2000:]


def test_a_keyboard_interrupt_is_not_dressed_up_as_a_crash(tmp_path):
    """A person stopping a driver by hand is not a failure, and a critical-level
    traceback for one buries the record of what it was doing when they did."""
    written = _run(tmp_path, "raise KeyboardInterrupt\n")

    assert "unhandled exception" not in written


def test_the_log_still_holds_ordinary_records_after_the_hooks_are_installed(tmp_path):
    written = _run(tmp_path, (
        "import logging\n"
        "logging.getLogger('hardware.test').info('still logging normally')\n"
    ))

    assert "still logging normally" in written


# --- the raw file-descriptor capture ----------------------------------------


def test_a_drivers_raw_stderr_is_captured_beside_its_log(tmp_path):
    """For what Python is not in a position to reformat: a vendor library writing to
    the file descriptor, or anything printed before logging was configured. Without a
    console path these are inherited - which means the launching terminal, and nowhere
    else."""
    console = driver_console_path(tmp_path, "run-1", "odrive")
    script = tmp_path / "noisy.py"
    script.write_text("import sys\nsys.stderr.write('libusb: device gone\\n')\n"
                      "print('on stdout too')\n")

    process = start_driver([sys.executable, str(script)], console)
    process.wait(timeout=60)

    written = console.read_text()
    assert "libusb: device gone" in written
    assert "on stdout too" in written, "stdout is folded in, so one file is the record"


def test_capture_appends_so_a_restarted_driver_does_not_erase_the_last_attempt(tmp_path):
    console = driver_console_path(tmp_path, "run-1", "odrive")
    script = tmp_path / "once.py"
    script.write_text("import sys\nsys.stderr.write('attempt\\n')\n")

    for _ in range(2):
        start_driver([sys.executable, str(script)], console).wait(timeout=60)

    assert console.read_text().count("attempt") == 2


def test_without_a_console_path_nothing_changes(tmp_path):
    """A driver run by hand keeps its terminal."""
    script = tmp_path / "quiet.py"
    script.write_text("pass\n")

    process = start_driver([sys.executable, str(script)])
    process.wait(timeout=60)

    assert process.returncode == 0


def test_the_two_files_sit_together_under_the_device(tmp_path):
    assert (driver_console_path(tmp_path, "run-1", "odrive").parent
            == driver_log_path(tmp_path, "run-1", "odrive").parent)
