"""Decoding ODrive error codes, and the driver logging that uses it.

The failure this guards against is a quiet one, and it is about a person rather
than a program: a run ends, `active_errors` reads 1056 in the CSV, and nobody
can say what happened without going to look it up. These tests pin the decoding
to the `odrive` package's own enum definitions, and pin the driver's logging to
firing once per change rather than once per frame - because a fault logged at
the frame rate buries everything around it, which is the same as not logging it.
"""
from __future__ import annotations

import logging
import sys
import threading

import pytest

from hardware.driver_logging import PROJECT_LOGGERS, configure, restore_crash_capture
from hardware.odrive import odrive_errors as oe
from hardware.odrive.odrive_backend import OdriveBackend

odrive_enums = pytest.importorskip("odrive.enums", reason="decoding is only meaningful with the odrive package")


# --- bitmask decoding -------------------------------------------------------


def test_a_single_bit_decodes_to_its_name():
    assert oe.decode_bitmask(int(odrive_enums.ODriveError.DRV_FAULT), "ODriveError") == "DRV_FAULT"


def test_several_bits_decode_to_all_their_names():
    value = int(odrive_enums.ODriveError.DRV_FAULT) | int(odrive_enums.ODriveError.BAD_CONFIG)
    decoded = oe.decode_bitmask(value, "ODriveError")
    assert "DRV_FAULT" in decoded and "BAD_CONFIG" in decoded
    assert " | " in decoded


def test_zero_decodes_to_a_word_not_an_empty_string():
    """So a log line reads "1056 -> 0 (none)" rather than trailing off."""
    assert oe.decode_bitmask(0, "ODriveError") == oe.NO_ERROR == "none"


def test_an_unrecognised_bit_is_named_rather_than_dropped():
    """Firmware newer than the installed package sets a bit the enum doesn't
    know. That bit is precisely the one worth surfacing, so it must not be
    silently discarded - nor may it raise, which is what handing an undefined
    bit to IntFlag can do depending on the Python version."""
    known = 0
    for member in odrive_enums.ODriveError:
        known |= int(member.value)
    free = next(bit for bit in range(32) if not (known >> bit) & 1)

    decoded = oe.decode_bitmask(1 << free, "ODriveError")
    assert decoded == f"UNKNOWN_BIT_0x{1 << free:x}"

    mixed = oe.decode_bitmask(int(odrive_enums.ODriveError.DRV_FAULT) | (1 << free), "ODriveError")
    assert "DRV_FAULT" in mixed and f"UNKNOWN_BIT_0x{1 << free:x}" in mixed


def test_the_gate_driver_fault_register_decodes_too():
    """last_drv_fault is a second, separate bitmask - DrvFault, not ODriveError -
    and decoding it against the wrong enum would produce confident nonsense."""
    value = int(odrive_enums.DrvFault.FET_LOW_C_OVERCURRENT)
    assert oe.decode_bitmask(value, "DrvFault") == "FET_LOW_C_OVERCURRENT"


# --- single-valued enums ----------------------------------------------------


def test_an_enum_value_decodes_to_its_name():
    assert oe.decode_enum(int(odrive_enums.AxisState.CLOSED_LOOP_CONTROL), "AxisState") == "CLOSED_LOOP_CONTROL"
    assert oe.decode_enum(int(odrive_enums.ProcedureResult.DISARMED), "ProcedureResult") == "DISARMED"


def test_an_unknown_enum_value_says_so_with_the_number():
    assert oe.decode_enum(9999, "AxisState") == "UNKNOWN(9999)"


@pytest.mark.parametrize("value", [None, "not a number", object()])
def test_a_non_numeric_value_does_not_raise(value):
    """A decoder is called while formatting a log line, sometimes on a frame
    whose channel is missing or blank. Raising there would turn a diagnostic
    into a second failure."""
    assert "not an integer" in oe.decode_bitmask(value, "ODriveError")
    assert "not an integer" in oe.decode_enum(value, "AxisState")


# --- describe ---------------------------------------------------------------


def test_describe_dispatches_each_channel_to_the_right_enum():
    assert oe.describe("active_errors", int(odrive_enums.ODriveError.DRV_FAULT)) == "DRV_FAULT"
    assert oe.describe("axis_current_state", int(odrive_enums.AxisState.IDLE)) == "IDLE"
    assert oe.describe("axis_procedure_result", 0) == "SUCCESS"
    assert oe.describe("commutmapper_status", 0) == "NOMINAL"


def test_describe_returns_none_for_a_channel_with_nothing_to_decode():
    """None rather than str(value), so a caller can tell "this decodes" from
    "this is just a number" without keeping its own list."""
    assert oe.describe("vel_estimate", 1.25) is None
    assert oe.describe("board_vbus_voltage", 48.0) is None


def test_describe_frame_decodes_only_the_decodable_channels():
    frame = {"active_errors": 0, "vel_estimate": 3.0, "axis_current_state": 1, "board_vbus_voltage": 48.0}
    described = oe.describe_frame(frame)
    assert set(described) == {"active_errors", "axis_current_state"}


def test_every_watched_channel_is_a_real_declared_channel():
    """A watched channel that no longer exists would be silently never logged."""
    from hardware.odrive.odrive_channels import TELEMETRY_CHANNELS

    for channel in oe.WATCHED_CHANNELS:
        assert channel in TELEMETRY_CHANNELS, f"{channel} is watched but not declared"


# --- fault classification ---------------------------------------------------


def test_a_bitmask_is_a_fault_when_non_zero():
    assert oe.is_fault("active_errors", 1) is True
    assert oe.is_fault("active_errors", 0) is False


def test_an_enum_channel_uses_its_own_benign_value():
    """SUCCESS and NOMINAL are 0 here, but that is a fact about these enums
    rather than a rule - which is why they are listed rather than assumed."""
    assert oe.is_fault("axis_procedure_result", int(odrive_enums.ProcedureResult.SUCCESS)) is False
    assert oe.is_fault("axis_procedure_result", int(odrive_enums.ProcedureResult.DISARMED)) is True
    assert oe.is_fault("commutmapper_status", int(odrive_enums.ComponentStatus.NOMINAL)) is False
    assert oe.is_fault("commutmapper_status", int(odrive_enums.ComponentStatus.NO_RESPONSE)) is True


def test_an_axis_state_change_is_not_a_fault():
    """Watched so a fault can be read against it, not because it is wrong."""
    assert oe.is_fault("axis_current_state", int(odrive_enums.AxisState.IDLE)) is False
    assert oe.is_fault("axis_current_state", int(odrive_enums.AxisState.CLOSED_LOOP_CONTROL)) is False


def test_a_transition_line_carries_both_the_numbers_and_the_text():
    """The number is what appears in the CSV and in a verdict's violation
    record, so a log line without it could not be matched back to either."""
    line = oe.format_transition("active_errors", 0, int(odrive_enums.ODriveError.DRV_FAULT))
    assert "0" in line and "DRV_FAULT" in line and "none" in line
    assert str(int(odrive_enums.ODriveError.DRV_FAULT)) in line


# --- the driver's use of it -------------------------------------------------


def _backend() -> OdriveBackend:
    """A backend that was never connected - _log_error_transitions only touches
    its own bookkeeping, so no hardware or handle is needed."""
    return OdriveBackend()


def test_a_clean_first_frame_logs_nothing(caplog):
    """Otherwise every startup announces eight channels reading zero, and the
    log's signal-to-noise is set badly from the first line."""
    caplog.set_level(logging.DEBUG)
    _backend()._log_error_transitions({channel: 0 for channel in oe.WATCHED_CHANNELS})
    assert caplog.text == ""


def test_a_fault_already_present_at_startup_is_reported_once(caplog):
    caplog.set_level(logging.DEBUG)
    backend = _backend()
    frame = {channel: 0 for channel in oe.WATCHED_CHANNELS}
    frame["active_errors"] = int(odrive_enums.ODriveError.DRV_FAULT)

    backend._log_error_transitions(frame)
    assert "already set at startup" in caplog.text
    assert "DRV_FAULT" in caplog.text

    caplog.clear()
    backend._log_error_transitions(frame)
    assert caplog.text == "", "an unchanged fault must not log again"


def test_a_fault_appearing_warns_and_a_fault_clearing_informs(caplog):
    caplog.set_level(logging.DEBUG)
    backend = _backend()
    clean = {channel: 0 for channel in oe.WATCHED_CHANNELS}
    backend._log_error_transitions(clean)

    caplog.clear()
    faulted = dict(clean, active_errors=int(odrive_enums.ODriveError.DRV_FAULT))
    backend._log_error_transitions(faulted)
    assert "ODrive fault" in caplog.text and "DRV_FAULT" in caplog.text
    assert any(record.levelno == logging.WARNING for record in caplog.records)

    caplog.clear()
    backend._log_error_transitions(clean)
    assert "cleared" in caplog.text
    assert all(record.levelno <= logging.INFO for record in caplog.records)


def test_a_standing_fault_logs_once_not_once_per_frame(caplog):
    """The whole reason this is edge-triggered: at 12 Hz a level-triggered
    version would emit ~43000 identical lines an hour."""
    caplog.set_level(logging.DEBUG)
    backend = _backend()
    faulted = {channel: 0 for channel in oe.WATCHED_CHANNELS}
    faulted["active_errors"] = int(odrive_enums.ODriveError.DRV_FAULT)
    for _ in range(50):
        backend._log_error_transitions(faulted)
    assert len(caplog.records) == 1


def test_an_axis_state_change_is_logged_without_being_called_a_fault(caplog):
    caplog.set_level(logging.DEBUG)
    backend = _backend()
    backend._log_error_transitions({channel: 0 for channel in oe.WATCHED_CHANNELS})
    caplog.clear()
    backend._log_error_transitions(
        {**{c: 0 for c in oe.WATCHED_CHANNELS},
         "axis_current_state": int(odrive_enums.AxisState.CLOSED_LOOP_CONTROL)}
    )
    assert "CLOSED_LOOP_CONTROL" in caplog.text
    assert "fault" not in caplog.text.lower()


def test_a_frame_missing_a_watched_channel_is_tolerated(caplog):
    caplog.set_level(logging.DEBUG)
    _backend()._log_error_transitions({"active_errors": 0})  # no exception


def test_a_decode_failure_never_breaks_the_telemetry_stream(caplog, monkeypatch):
    """runner.py treats stream_samples() raising as a real device failure and
    shuts the process down. A diagnostic must never be able to cause that."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(oe, "is_fault", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    backend = _backend()
    backend._log_error_transitions({channel: 1 for channel in oe.WATCHED_CHANNELS})
    assert "failed to log an ODrive error transition" in caplog.text

# --- driver log file --------------------------------------------------------


@pytest.fixture
def isolated_logging():
    """Snapshot root logging, and undo exactly what configure() adds.

    configure() appends handlers to the root logger, so a test that does not
    remove its own would leak a FileHandler into every later test. Only handlers
    added during the test are closed - closing the ones pytest installed breaks
    caplog for everything that follows.

    It also installs interpreter-wide crash hooks, which leak the same way and are
    worse: pytest owns threading.excepthook to report a thread that died, and
    faulthandler would keep a deleted tmp_path file open. See restore_crash_capture."""
    root = logging.getLogger()
    before = list(root.handlers)
    before_level = root.level
    before_levels = {name: logging.getLogger(name).level for name in PROJECT_LOGGERS}
    before_hooks = (sys.excepthook, threading.excepthook)
    yield root
    restore_crash_capture()
    sys.excepthook, threading.excepthook = before_hooks
    for handler in root.handlers:
        if handler not in before:
            handler.close()
    root.handlers = before
    root.setLevel(before_level)
    for name, level in before_levels.items():
        logging.getLogger(name).setLevel(level)


def test_the_log_file_is_written_and_appended(tmp_path, isolated_logging):
    root = isolated_logging
    path = tmp_path / "runs" / "abc" / "odrive" / "logs.txt"

    assert configure(str(path), device="odrive") == path
    logging.getLogger("hardware.test").debug("first process, detail")
    for handler in root.handlers:
        handler.flush()
    assert "first process, detail" in path.read_text()

    # A second driver process writing the same path, as a restart mid-run would.
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            handler.close()
            root.removeHandler(handler)
    configure(str(path), device="odrive")
    logging.getLogger("hardware.test").debug("second process, detail")
    for handler in root.handlers:
        handler.flush()

    text = path.read_text()
    assert "first process, detail" in text, "a restarted driver must not erase the previous attempt"
    assert "second process, detail" in text


def test_debug_reaches_the_file_but_not_the_console(tmp_path, isolated_logging):
    """A driver's stdout usually goes nowhere anybody reads, so the detail
    belongs in the file while the console stays legible for a person running one
    by hand."""
    configure(str(tmp_path / "logs.txt"), device="odrive")
    levels = {type(h).__name__: h.level for h in isolated_logging.handlers}
    assert levels["FileHandler"] == logging.DEBUG
    assert levels["StreamHandler"] == logging.INFO


def test_dependencies_are_not_raised_to_debug(tmp_path, isolated_logging):
    """Root at DEBUG would pull in asyncio's event-loop chatter and, worse, the
    odrive package's libusb traffic - at which point the detailed log is mostly
    somebody else's detail."""
    configure(str(tmp_path / "logs.txt"), device="odrive")
    assert isolated_logging.level == logging.INFO
    assert logging.getLogger("asyncio").getEffectiveLevel() == logging.INFO
    for name in PROJECT_LOGGERS:
        assert logging.getLogger(name).level == logging.DEBUG


def test_an_unwritable_log_path_degrades_instead_of_failing(tmp_path, isolated_logging):
    """Losing the log is not a reason to refuse to drive the hardware."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    assert configure(str(blocker / "odrive" / "logs.txt"), device="odrive") is None


def test_no_log_file_requested_means_console_only(isolated_logging):
    """Compared against the handlers already present, because pytest's own
    logging plugin installs a /dev/null FileHandler on the root logger - so
    "root has no FileHandler" would never have been true here."""
    before = list(isolated_logging.handlers)
    assert configure(None, device="odrive") is None
    added = [h for h in isolated_logging.handlers if h not in before]
    assert [type(h).__name__ for h in added] == ["StreamHandler"]


# --- classification learned from the real board ------------------------------


def test_relative_mode_is_not_a_fault():
    """Measured on a real ODrive Pro (fw 0.6.12): posvelmapper_status sits at
    RELATIVE_MODE steadily and healthily, because the mapper reports position
    relative to startup when there is no absolute reference. Classifying it as a
    fault warned on every startup of the zdrive stand, which is how a log
    teaches people to stop reading it."""
    relative = int(odrive_enums.ComponentStatus.RELATIVE_MODE)
    assert oe.describe("posvelmapper_status", relative) == "RELATIVE_MODE"
    assert oe.is_fault("posvelmapper_status", relative) is False
    for channel in ("commutmapper_status", "encoder_onboard0_status"):
        assert oe.is_fault(channel, relative) is False


def test_genuine_component_faults_are_still_faults():
    for name in ("NO_RESPONSE", "PARITY_MISMATCH", "UNCONFIGURED", "SPINOUT_DETECTED"):
        value = int(getattr(odrive_enums.ComponentStatus, name))
        assert oe.is_fault("posvelmapper_status", value) is True, f"{name} should be a fault"


def test_an_unpowered_bus_reads_as_a_real_condition():
    """The value a real board reported with its DC bus at 0.03 V. Worth keeping
    as a literal: it is the exact number that motivated this whole module."""
    assert oe.describe("active_errors", 513) == "INITIALIZING | DC_BUS_UNDER_VOLTAGE"
    assert oe.is_fault("active_errors", 513) is True


def test_only_the_faulted_channels_are_reported_as_faults():
    """describe_frame() decodes everything it can, including what is fine.
    faults_in_frame() is for a caller deciding whether the board is fit to be
    armed, which a channel reading NOMINAL does not help with."""
    frame = {
        "active_errors": 4096,          # CURRENT_LIMIT_VIOLATION
        "disarm_reason": 0,             # nothing
        "axis_procedure_result": 0,     # SUCCESS
        "axis_current_state": 1,        # IDLE - a state, never a fault
        "pos_estimate": 3.5,            # not a watched channel at all
    }
    faults = oe.faults_in_frame(frame)

    assert list(faults) == ["active_errors"]
    assert "CURRENT_LIMIT_VIOLATION" in faults["active_errors"]


def test_a_clean_frame_reports_no_faults():
    assert oe.faults_in_frame({"active_errors": 0, "disarm_reason": 0}) == {}
