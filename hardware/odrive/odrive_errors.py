"""Decodes the ODrive's numeric error, fault and state channels into names.

`active_errors` = 1056 becomes `DRV_FAULT | DC_BUS_OVER_CURRENT`. Used by the
driver to log a fault in words, and by anything reading a stored run.

Enum definitions come from the `odrive` package (`odrive.enums`) rather than a
table in this repo, so they track the installed package. The import is guarded:
this module imports fine without the package, and degrades to reporting raw
numbers.

Bitmasks are decoded bit by bit rather than through `IntFlag`, so a bit the
installed package does not know - newer firmware - is reported as
`UNKNOWN_BIT_0x...` rather than dropped, and the output does not depend on the
Python version's handling of undefined flag bits.

What this is NOT: a source of telemetry channels. The raw integers stay in the
frame and the text is produced on demand, so nothing here adds a mostly-empty
string column to every row of a run.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

NO_ERROR = "none"
"""What a zero bitmask decodes to. A word rather than an empty string, so a log
line reads `active_errors 1056 -> 0 (none)` rather than trailing off."""

_UNAVAILABLE = "cannot decode: the 'odrive' package is not installed"


def _enum_classes() -> Optional[Dict[str, Any]]:
    """The odrive enum classes needed here, or None if the package is absent.

    Resolved on first call and cached, so importing this module never requires
    the package."""
    cached = getattr(_enum_classes, "_cache", "unset")
    if cached != "unset":
        return cached

    try:
        from odrive.enums import (
            AxisState,
            ComponentStatus,
            ControlMode,
            DrvFault,
            EncoderId,
            InputMode,
            MotorType,
            ODriveError,
            ProcedureResult,
        )
    except ImportError:
        logger.warning(
            "the 'odrive' package is not installed, so ODrive error codes will be reported as raw "
            "numbers - run 'uv sync' to decode them"
        )
        _enum_classes._cache = None
        return None

    _enum_classes._cache = {
        "AxisState": AxisState,
        "ComponentStatus": ComponentStatus,
        "ControlMode": ControlMode,
        "DrvFault": DrvFault,
        "EncoderId": EncoderId,
        "InputMode": InputMode,
        "MotorType": MotorType,
        "ODriveError": ODriveError,
        "ProcedureResult": ProcedureResult,
    }
    return _enum_classes._cache


# Channel name -> (enum class name, is it a bitmask). Only channels whose value
# is an enum or a bitmask appear; a channel carrying volts or turns has nothing
# to decode. Kept as a table so ERROR_CHANNELS below can be derived from it
# rather than listed twice.
_DECODABLE: Dict[str, Tuple[str, bool]] = {
    "active_errors": ("ODriveError", True),
    "disarm_reason": ("ODriveError", True),
    "last_drv_fault": ("DrvFault", True),
    "axis_current_state": ("AxisState", False),
    "axis_procedure_result": ("ProcedureResult", False),
    "commutmapper_status": ("ComponentStatus", False),
    "posvelmapper_status": ("ComponentStatus", False),
    "encoder_onboard0_status": ("ComponentStatus", False),
    "controller_config_control_mode": ("ControlMode", False),
    "controller_config_input_mode": ("InputMode", False),
    "motor_config_motor_type": ("MotorType", False),
    "axis_config_load_encoder": ("EncoderId", False),
    "axis_config_commutation_encoder": ("EncoderId", False),
}

# The channels worth logging a transition on: something went wrong, or the axis
# changed what it is doing. Deliberately excludes the config channels in
# _DECODABLE above - control mode and encoder assignment are still decodable
# when reporting on a stored run, but a test changing them on purpose is not an
# event a driver should narrate.
LATCHED_CHANNELS: Tuple[str, ...] = (
    "active_errors",
    "disarm_reason",
    "detailed_disarm_reason",
    "axis_procedure_result",
)
"""What `clear_errors()` resets. These hold a value until something clears them.

`last_drv_fault` is NOT among them - see RECORD_ONLY_CHANNELS."""

RECORD_ONLY_CHANNELS: Tuple[str, ...] = ("last_drv_fault",)
"""What `clear_errors()` does not touch, and nothing can: the gate driver's record
of the last fault it saw.

THE NAME IS THE SEMANTICS - the LAST fault, not a present one. It is written when
the DRV chip faults and then kept for diagnosis, so once it is non-zero it stays
non-zero for the life of the board's power. Treated as something to clear, it
refuses every subsequent run: measured on 2026-08-26, a person switched the
supplies off mid-run, the gate driver recorded its own rail collapsing as
0x400000, and every restart afterwards failed with "still latched after being
cleared" while `active_errors` read 0 and the board was fit.

Recorded and logged, never a gate. A gate-driver failure that actually prevents
operation says so through `active_errors`, which is what arming depends on."""

CONDITION_CHANNELS: Tuple[str, ...] = (
    "commutmapper_status",
    "posvelmapper_status",
    "encoder_onboard0_status",
)
"""What describes the board right now, and what `clear_errors()` cannot touch.

A ComponentStatus is a live reading, not a latch: a mapper reporting MISSING_INPUT
has no encoder estimate at this instant, and it says so again the moment it is
"cleared". Clearing errors and then waiting for one of these to go away waits
forever - the cause has to change, whether that is the bus coming up, a cable, or
which encoder the axis is configured to read.

ENCODER_FIELD_TOO_HIGH and ENCODER_FIELD_TOO_LOW on encoder_onboard0_status are
worth knowing by name: the onboard magnetic encoder is reading a field outside the
range it can resolve, so it produces no estimate and both mappers report
MISSING_INPUT downstream of it. Observed on the ydrive stand, and cleared by
turning the wheel by hand - a rotor parked where the field saturates reads that
way until it moves. `encoder_onboard0_get_field_strength` quantifies it if the
chip exposes it. A magnet that reads out of range at every position is a mounting
problem, not something a test can clear."""

ERROR_CHANNELS: Tuple[str, ...] = LATCHED_CHANNELS + RECORD_ONLY_CHANNELS + CONDITION_CHANNELS

GATING_CHANNELS: Tuple[str, ...] = ("active_errors",) + CONDITION_CHANNELS
"""What decides whether the board can operate NOW, and the only thing a caller
should refuse to start on.

Everything else watched here describes something that already happened -
`disarm_reason` is why it last disarmed, `axis_procedure_result` how the last
procedure ended, `last_drv_fault` what the gate driver last saw. History is worth
recording and worth reading; it is not a reason to refuse a stand that is
currently fit. The distinction is what stops an event nobody planned for -
somebody switching the supplies off mid-run - from leaving the stand unusable
until a person finds the one bit that is stuck."""

STATE_CHANNELS: Tuple[str, ...] = ("axis_current_state",)
"""Watched so a fault can be read against it rather than because it is wrong: a
DISARMED procedure result means something different depending on whether the
axis was in CLOSED_LOOP_CONTROL at the time."""

WATCHED_CHANNELS: Tuple[str, ...] = ERROR_CHANNELS + STATE_CHANNELS

_BENIGN_COMPONENT_STATUS: Tuple[int, ...] = (0, 9)
"""ComponentStatus values that are not faults: NOMINAL and RELATIVE_MODE.

RELATIVE_MODE is a normal steady state, not a problem: it means the mapper
reports position relative to startup, which is what an encoder with no absolute
reference does.

Every other value is treated as a fault, including NOT_ENABLED - which may be a
benign steady state on a board that does not use one of its three mappers, so
expect to add it here if such a board turns up.

MISSING_INPUT is deliberately NOT benign, and is the one to expect at startup: a
driver that connects while the motor bus is down reports it on commutmapper and
posvelmapper, alongside DC_BUS_UNDER_VOLTAGE, because the encoder feeding them is
not producing yet. It clears when the bus comes up. Left loud because the same
value after the bus is up is the reason an axis will refuse CLOSED_LOOP_CONTROL,
and that is worth a line in the log rather than a silent refusal later."""

_BENIGN: Dict[str, Tuple[Any, ...]] = {
    "axis_procedure_result": (0, 1),  # SUCCESS, BUSY
    "commutmapper_status": _BENIGN_COMPONENT_STATUS,
    "posvelmapper_status": _BENIGN_COMPONENT_STATUS,
    "encoder_onboard0_status": _BENIGN_COMPONENT_STATUS,
}
"""Values that mean "nothing wrong", for channels whose benign value is not
simply an empty bitmask. Decides whether a transition is logged as a fault
appearing, a fault clearing, or neither.

BUSY IS PROGRESS, NOT A FAULT: it means a procedure is running right now, which
is the normal state part-way through arming. Read as a fault it put a WARNING in
the log on every single arm, and any check sampling the board mid-arm saw a stand
that was working as one that was broken."""


def decode_bitmask(value: Any, enum_name: str) -> str:
    """Decode a bitmask to `NAME | NAME`, naming unrecognised bits explicitly."""
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return f"{value!r} (not an integer)"
    if raw == 0:
        return NO_ERROR

    classes = _enum_classes()
    if classes is None:
        return f"0x{raw:x} ({_UNAVAILABLE})"

    enum_cls = classes[enum_name]
    names: List[str] = []
    remaining = raw
    for member in enum_cls:
        bit = int(member.value)
        if bit and (remaining & bit) == bit:
            names.append(member.name)
            remaining &= ~bit
    if remaining:
        # Firmware newer than the installed package, or a reserved bit set. Say
        # so rather than dropping it - an unexplained bit is the interesting one.
        names.append(f"UNKNOWN_BIT_0x{remaining:x}")
    return " | ".join(names)


def decode_enum(value: Any, enum_name: str) -> str:
    """Decode a single-valued enum to its name, or `UNKNOWN(<n>)`."""
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return f"{value!r} (not an integer)"

    classes = _enum_classes()
    if classes is None:
        return f"{raw} ({_UNAVAILABLE})"

    for member in classes[enum_name]:
        if int(member.value) == raw:
            return member.name
    return f"UNKNOWN({raw})"


def describe(channel: str, value: Any) -> Optional[str]:
    """Human-readable text for one channel's value, or None if the channel has
    nothing to decode.

    None rather than str(value), so a caller can tell "this decodes to text"
    from "this is just a number" without keeping its own list of which is
    which."""
    entry = _DECODABLE.get(channel)
    if entry is None:
        return None
    enum_name, is_bitmask = entry
    return decode_bitmask(value, enum_name) if is_bitmask else decode_enum(value, enum_name)


def describe_frame(channels: Dict[str, Any]) -> Dict[str, str]:
    """Decode every decodable channel present in a frame. For reporting over a
    stored run; the driver uses `describe()` per changed channel instead."""
    described = {}
    for channel, value in channels.items():
        text = describe(channel, value)
        if text is not None:
            described[channel] = text
    return described


def faults_in_frame(channels: Dict[str, Any]) -> Dict[str, str]:
    """Just the watched channels of this frame that read as a fault, decoded.

    For a caller deciding whether the board is fit to be armed, rather than one
    reporting what it is doing - describe_frame() decodes everything it can,
    including the channels that are fine.

    ONLY THE CHANNELS THAT CAN STOP IT OPERATING, which is not every watched
    channel: see GATING_CHANNELS for why a record of a past fault must not refuse
    a board that is presently fit, and records_in_frame() for reading those."""
    return {
        channel: describe(channel, channels[channel])
        for channel in GATING_CHANNELS
        if channel in channels and is_fault(channel, channels[channel])
    }


def records_in_frame(channels: Dict[str, Any]) -> Dict[str, str]:
    """The watched channels describing something that already happened, decoded.

    The other half of faults_in_frame(): what is worth recording about this board
    without being a reason to refuse it - see GATING_CHANNELS."""
    return {
        channel: describe(channel, channels[channel])
        for channel in RECORD_ONLY_CHANNELS + LATCHED_CHANNELS
        if channel not in GATING_CHANNELS
        and channel in channels
        and is_fault(channel, channels[channel])
    }


def is_fault(channel: str, value: Any) -> bool:
    """Whether this value means something is wrong. Decides the log level.

    Bitmask channels are faults when non-zero. Enum-valued channels are checked
    against their own benign values (see _BENIGN), since "not a fault" is not
    always simply zero."""
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return False
    if channel in _BENIGN:
        return raw not in _BENIGN[channel]
    entry = _DECODABLE.get(channel)
    if entry is not None and entry[1]:
        return raw != 0
    if channel == "detailed_disarm_reason":
        return raw != 0
    return False


def format_transition(channel: str, previous: Any, current: Any) -> str:
    """One log line for a channel that changed.

    Carries the raw numbers alongside the decoded text, because the number is
    what appears in the telemetry CSV and in a verdict's violation record - the
    line has to be matchable against both."""
    text = describe(channel, current)
    if text is None:
        return f"{channel}: {previous} -> {current}"
    previous_text = describe(channel, previous)
    return f"{channel}: {previous} -> {current} ({previous_text} -> {text})"
