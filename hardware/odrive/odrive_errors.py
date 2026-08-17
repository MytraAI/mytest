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
expect to add it here if such a board turns up."""

_BENIGN: Dict[str, Tuple[Any, ...]] = {
    "axis_procedure_result": (0,),  # SUCCESS
    "commutmapper_status": _BENIGN_COMPONENT_STATUS,
    "posvelmapper_status": _BENIGN_COMPONENT_STATUS,
    "encoder_onboard0_status": _BENIGN_COMPONENT_STATUS,
}
"""Values that mean "nothing wrong", for channels whose benign value is not
simply an empty bitmask. Decides whether a transition is logged as a fault
appearing, a fault clearing, or neither."""


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
