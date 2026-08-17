"""Turn the ODrive's numeric error, fault and state channels into text a person
can act on.

`active_errors` reads 1056. That is `DRV_FAULT | MOTOR_FAILED`, and nothing in a
recorded run says so - the number lands in the CSV, in `verdict.json`'s
violation records, and in the driver's log, and a test engineer at 3am has to go
and look it up. This module is the lookup, in code.

THE ENUM DEFINITIONS COME FROM THE `odrive` PACKAGE, not from a table copied
into this repo. `odrive.enums` ships the real definitions for the firmware line
the package targets, so decoding stays correct across an `odrive` upgrade
instead of drifting from a hand-maintained copy that nobody re-checks. The
import is guarded: this module is imported by offline tooling that may run
where the package is absent, and a missing package degrades to showing the raw
number rather than failing.

BITMASKS ARE DECODED BIT BY BIT rather than by handing the value to `IntFlag`.
Two reasons. An unrecognised bit - newer firmware than the installed package
knows - is reported explicitly as `UNKNOWN_BIT_0x...` instead of being dropped
or raising, which matters because that bit is exactly the one you would want to
know about. And IntFlag's handling of undefined bits has changed across Python
versions, so doing the masking here keeps the output stable.

WHAT THIS IS NOT: a telemetry channel. The raw integers stay in the frame, and
the decoded text is produced on demand - by the driver when a value *changes*,
and by whatever reads a stored run afterwards. Text belongs in a driver's log
file (see protocol/paths.py's DRIVER_LOG_FILENAME), not in a column that would
be empty in almost every row of every run.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

NO_ERROR = "none"
"""What a zero bitmask decodes to. A word rather than an empty string, so a log
line reads `active_errors 1056 -> 0 (none)` instead of trailing off."""

_UNAVAILABLE = "cannot decode: the 'odrive' package is not installed"


def _enum_classes() -> Optional[Dict[str, Any]]:
    """The odrive enum classes needed here, or None if the package is absent.

    Resolved on each call and cached on the function, so importing this module
    never requires the package - offline tooling and unit tests import it
    freely - while a driver that does have it pays the lookup once."""
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
ERROR_CHANNELS: Tuple[str, ...] = (
    "active_errors",
    "disarm_reason",
    "detailed_disarm_reason",
    "last_drv_fault",
    "axis_procedure_result",
    "commutmapper_status",
    "posvelmapper_status",
    "encoder_onboard0_status",
)

STATE_CHANNELS: Tuple[str, ...] = ("axis_current_state",)
"""Not a failure, but the transition a fault has to be read against: a
DISARMED procedure result means something different depending on whether the
axis was in CLOSED_LOOP_CONTROL at the time."""

WATCHED_CHANNELS: Tuple[str, ...] = ERROR_CHANNELS + STATE_CHANNELS

_BENIGN_COMPONENT_STATUS: Tuple[int, ...] = (0, 9)
"""ComponentStatus values that are not faults: NOMINAL, and RELATIVE_MODE.

RELATIVE_MODE is here because of a measurement, not a reading of the docs. A
real ODrive Pro (fw 0.6.12) reports `posvelmapper_status` = 9 steadily and
healthily - it means the mapper is reporting position relative to startup, which
is simply what an encoder with no absolute reference does. Treating it as a
fault produced a warning on every single startup of the zdrive stand, which is
how a log teaches people to ignore it.

Every other ComponentStatus value is left classified as a fault. NOT_ENABLED in
particular may turn out to be another benign steady state on a board that does
not use one of the three mappers - but that has not been observed, and guessing
at it would trade this false positive for a false negative."""

_BENIGN: Dict[str, Tuple[Any, ...]] = {
    "axis_procedure_result": (0,),  # SUCCESS
    "commutmapper_status": _BENIGN_COMPONENT_STATUS,
    "posvelmapper_status": _BENIGN_COMPONENT_STATUS,
    "encoder_onboard0_status": _BENIGN_COMPONENT_STATUS,
}
"""Values that mean "nothing wrong" for channels whose zero is not an empty
bitmask. Used to decide whether a transition is a fault appearing or a fault
clearing, which is the difference between a warning and an info line."""


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
    """Human-readable text for one channel's value, or None if it has none.

    None rather than str(value) for an ordinary numeric channel: a caller
    formatting a report needs to know the difference between "this decodes to
    text" and "this is just a number", and `None` says it without the caller
    having to keep its own list."""
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


def is_fault(channel: str, value: Any) -> bool:
    """Whether this value means something is wrong, for deciding a log level.

    Bitmask channels are faults when non-zero. Enum-valued ones have a specific
    benign value (SUCCESS, NOMINAL) that is not always the same as zero-is-fine,
    which is why they are listed rather than assumed."""
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

    Carries the raw numbers as well as the decoded text on purpose: the number
    is what appears in the telemetry CSV and in a verdict's violation record, so
    a log line that dropped it could not be matched back to either."""
    text = describe(channel, current)
    if text is None:
        return f"{channel}: {previous} -> {current}"
    previous_text = describe(channel, previous)
    return f"{channel}: {previous} -> {current} ({previous_text} -> {text})"
