"""Real backend for the Keysight N6974A Advanced Power System, over ethernet.
See n6974a_channels.py for the declared channel surface and transport.py for
the line protocol and its failure modes.

There is deliberately no mock backend. As with the CPX400DP, this driver's risk
is not wrong device paths but wrong *response parsing and message building* -
60 queries batched into one message, three status registers decoded bit by bit -
and a HardwareBackend-level mock would replace exactly the code most likely to
be wrong. Tests substitute a fake *transport* instead, so the real parsing runs
against replies recorded from the instrument (tests/test_n6974a.py).

ONE MESSAGE PER FRAME. Every telemetry channel is re-read every frame in a
single compound SCPI message, so there is no settings cache and no assumption
that this driver is the only thing touching the instrument. What the frame costs
is the measurement inside it, not the I/O: ~32 ms total, of which ~21 ms is the
MEASure acquisition at the default 1 power line cycle and ~1.4 ms is every other
query combined.

CONNECT AND DISCONNECT ARE PASSIVE. connect() confirms the reply format and the
model, verifies the declared N7909A count against the instrument, clears this
session's status and error queue, reads the instrument's own limits, probes every
declared query once, and reads a first frame. It does not enable the output,
does not disable one it finds already on, does not touch a protection level, and
does not arm the watchdog. It logs the state it adopted, so a run records what
it inherited. disconnect() closes the link and leaves the output exactly as it
is. The framework never assumes a supply's output should be on because it
connected, and equally does not assume it may de-energize something a person
deliberately energized.

EVERY READABLE QUERY IS PROBED ONCE, AT CONNECT - all 136, not just the 60 a
frame uses. A command this unit does not implement is answered with silence and
discards the whole message it was part of, so a single wrong mnemonic inside the
frame would cost a read timeout and a link resynchronisation on every frame for
the entire run, and a wrong one among the readback-only queries would do the
same inside a write and be reported against a command that actually succeeded.
Probed individually at connect it is instead a setup-time error naming the
channel, for about a tenth of a second.

EVERY WRITE IS CHECKED, AND ITS READBACK IS A SECOND MESSAGE. A command travels
with its `SYSTem:ERRor?` check in one message, so the error cannot belong to
another caller's command. The readback follows as a second message inside the
same transaction, because on this instrument a setting query in the *same*
message as the write that changes it answers with the value from before the
write - deterministically one step behind, and only for the source-programming
parameters (`VOLTage`, `CURRent:LIMit`, `CURRent:LIMit:NEGative`), while
protection parameters answer freshly. Neither `*WAI` nor `*OPC?` between them
changes it. Two messages inside one transaction cost ~0.9 ms against ~0.5 ms and
are correct for every parameter, which is the trade worth making: the readback is
what tells a caller a clamped setpoint was clamped, so a stale one would be
worse than none.

The error queue this driver reads is its own. The guide describes one queue per
interface (GPIB, USB, VXI-11, Telnet/Sockets); measurement on this firmware is
finer still - two simultaneous socket connections do not see each other's
errors, and a reopened connection starts empty. So a `SYSTem:ERRor?` answer is
attributable to this driver's own last command, and neither the front panel nor
another client can pollute it.

A READBACK THAT DISAGREES WITH THE COMMAND IS REPORTED. Some parameters belong to
one priority mode and are quietly ignored in the other: `CURRent` written while
in voltage priority is accepted with no error and does not take effect, where
`VOLTage:LIMit` written in the same state at least answers
`+315,"Settings conflict error"`. Rather than enumerate which parameter belongs
to which mode, every numeric write compares what came back against what was
commanded and logs a warning when they differ - which catches the silent
mode-dependent no-op, and anything else of that shape, without the driver having
to predict it.

COMMANDED SETPOINTS ARE CLAMPED, NOT REFUSED. A voltage or current beyond what
the instrument allows is applied at the limit instead, with a warning naming
what was asked and what was applied, and a sticky telemetry channel recording
that it happened. The bounds are read from the instrument itself
(`VOLTage? MAX`, `CURRent:LIMit:NEGative? MIN` and so on), narrowed to this
model's rating where that is tighter than the programmable range it reports
(CEILING_VOLTAGE_V/CEILING_CURRENT_A), and the negative bound is additionally
held to what the declared N7909A count permits. The rating is a fact about an
N6974A, so a stand needing a tighter ceiling than 80 V/25 A owns that itself.
Clamping applies only to the quantities that carry energy - setpoints, limits and
triggered levels. Everything else is passed through and an out-of-range value
raises with the instrument's own error text, because silently altering a watchdog
delay or a comparator level would hide a mistake rather than contain a hazard.
The OVP threshold is clamped too, but to the instrument's own range rather than
the rating: a threshold above the rated output is a legitimate way to leave
over-voltage protection out of the way.

BEHAVIOUR OF THIS INSTRUMENT worth knowing before writing a test against it:
  - ~0.4 ms per query round trip, and a compound message costs one round trip
    however many queries it carries.
  - A MEASure is an acquisition, not a read: it takes NPLC power line cycles
    (~21 ms at the default 1), and the front panel shows dashes while it runs.
    MEASure:VOLTage? followed by FETCh:CURRent? returns both from the same
    acquisition; measuring each separately would cost twice as long and give two
    different instants.
  - Switching priority mode turns the output off and reverts every output
    setting to its reset value. execute() refuses it while the output is on.
  - Every protection latches. `clear_protection` only takes effect once the
    cause is gone.
  - `protection_mode` LOWZ, the reset default, actively sinks the load's energy
    for 2 ms while shutting down; HIGHZ disconnects without sinking. HIGHZ is
    not absolute on this model: its output exceeds 60 V, and for that class the
    guide states the down-programmer stays enabled for a power-fail fault
    whatever the mode says.
  - `OUTPut:PROTection:DELay` and `CURRent:PROTection:DELay` are one parameter,
    not two - writing either moves both.
  - Which function a digital pin accepts depends on the pin: FAULt is pin 1
    only, INHibit pin 3 only, ONCouple/OFFCouple pins 4-7 only. Setting a pin to
    ONCouple or OFFCouple also moves its polarity.
  - An N7909A is only recognised at power-on. Cabling one to a running supply
    does nothing, and it reads as absent.
  - `*TST?` runs a real self-test and takes 5.2 s.
  - A command beginning with `*` must not be preceded by a root colon inside a
    message; `join_message` in transport.py handles that.
  - The three sense-lead faults are not equivalent, and only one of them is
    benign. An OPEN sense lead raises the sense fault (SF) within ~50 us, and
    the instrument falls back to local sensing and keeps regulating with the
    output terminals ~1% above the programmed value; reconnecting returns it to
    normal. A SHORTED sense lead is detected by over-voltage protection and
    disables the output (OV). A REVERSED one is detected by negative
    over-voltage protection and disables it (OV-). Neither of the latter two is
    programmable, and neither can be detected until the output is enabled -
    which the guide notes means mis-wiring is only discovered by briefly
    subjecting the load to an unintended voltage.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence, Tuple

from ..backend import HardwareBackend, HardwareError, MissingChannelError
from protocol.wire import DEVICE_N6974A

from .n6974a_channels import (
    CLAMPED_QUANTITIES,
    COMMAND_CHANNELS,
    DIGITAL_PINS,
    DRIVER_CHANNELS,
    MAX_DISSIPATORS,
    METER_CHANNELS,
    RATED_POWER_W,
    SETTING_CHANNELS,
    SIGNAL_EXPRESSIONS,
    SINK_FRACTION_BY_DISSIPATORS,
    STATUS_CHANNELS,
    STATUS_REGISTERS,
    TELEMETRY_CHANNELS,
    TEMPERATURE_CHANNELS,
    THRESHOLD_COMPARATORS,
)
from .transport import DEFAULT_PORT, KeysightSocketTransport

logger = logging.getLogger(__name__)

DEFAULT_N6974A_HOST = "169.254.236.129"
"""The instrument on this stand. NOT a stable address: it is configured for
automatic addressing and the segment it is on has no DHCP server, so it
self-assigned a link-local address, which moves if a DHCP server appears or on
an address collision. A testbed is expected to pass an explicit host from its
own config; this exists so `python -m hardware.n6974a.main` works standalone.
connect() verifies the model in `*IDN?` precisely because a moving address could
otherwise point this driver at a different instrument.

An mDNS name is a stabler identity: the instrument advertises itself as
`A-N6974A-00121.local` and follows that name when its address changes. It needs
an mDNS responder on the host - macOS has one built in, a Windows or CentOS
stand may not."""

SAMPLE_INTERVAL_S = 0.02
"""Sleep *between* frames, not the frame period - and on this instrument the
difference is most of the number.

A frame is one compound message costing ~32 ms, nearly all of it the MEASure
acquisition inside it, so the achieved rate is around 20 Hz rather than the 50 Hz
this interval would suggest. A consumer should read a frame's `t` rather than
assume a fixed period.

The lever that actually moves the frame rate is `set_nplc`: the acquisition is
NPLC power line cycles long, so 0.1 PLC gives a ~4 ms frame and ~90 Hz at the
cost of the line-frequency noise rejection the 1 PLC default buys. That is a
test-engineering decision rather than a driver one, which is why it is an action
and not a constructor argument.

On Windows expect a coarser and lumpier period than the ~19 Hz measured on
macOS: the default system timer granularity is about 15.6 ms, so a 20 ms sleep
lands somewhere between 15 and 31 ms. Nothing breaks - the frame carries its own
`t` and a consumer should read it rather than assume a period - but a recorded
run from a Windows stand will not have the same cadence as one from a Mac."""

EXPECTED_MODEL = "N6974A"
"""Checked against the model field of `*IDN?` at connect. The N6900/N7900 family
shares this command set, but the ratings, the dissipator arithmetic and the
absent options below are this model's."""

EXPECTED_DATA_FORMAT = "ASC"
"""`FORMat?` must read this.

In REAL format the instrument answers with definite-length binary blocks that
this line-oriented transport cannot carry. The guide is specific that the setting
affects only "a small subset of queries that can return large quantities of
data", so it does not touch the scalar queries a frame is built from - the one
declared action it could break is `read_acquire_trigger_indices`. `*RST` sets it
back to ASCII, so a unit found in REAL was deliberately put there by another
client. Checked once at connect because it costs a single query and the failure
it prevents is an unreadable reply rather than a reported error."""

SINK_TOLERANCE_A = 0.05
"""How far the instrument's reported negative-current floor may sit from the
value the declared dissipator count predicts before the two are called
different. Covers the rounding in the instrument's own percentage arithmetic,
and is far tighter than the gap between adjacent counts (10%, 50% and 100% of
25.5 A are 2.55 A apart at the closest)."""

SLOW_COMMAND_TIMEOUT_S = 20.0
"""Read ceiling for the few commands that legitimately block far longer than a
query.

`*TST?` runs a real self-test on this instrument and takes 5.2 s measured, which
already exceeds the ordinary read ceiling; `*OPC?` and `*WAI` block until every
pending operation finishes, which is bounded by whatever those operations are.
Given a longer ceiling only for these, rather than raising it for everything -
the ordinary ceiling is what turns an unimplemented mnemonic's silence into a
prompt error instead of a long stall on every frame."""

SLOW_ACTIONS = frozenset({"self_test", "read_operation_complete", "wait_operation_complete"})
"""Actions that get SLOW_COMMAND_TIMEOUT_S instead of the transport's default."""

MAX_CONSECUTIVE_FRAME_FAILURES = 3
"""How many frames in a row may fail before stream_samples() gives up and
raises.

A frame failure means the link desynchronised and the transport reopened it, so
a single one is recoverable and costs a gap in the record rather than a run. A
run of them means the instrument or the network is genuinely broken, and then
raising is right: runner.run() treats a telemetry task dying as a real device
failure and exits the process, which is what should happen."""


# --- Response parsing ------------------------------------------------------
# This instrument is consistent, unlike a supply that echoes its own mnemonic:
# numbers come back in a fixed exponential form (`+8.160000E+01`), booleans as
# `0`/`1`, integers with a sign (`+8192`), and discrete parameters as the
# short-form keyword in upper case (`VOLT`, `LOWZ`, `SCH`). Strings are quoted.


def _as_float(reply: str) -> float:
    try:
        return float(reply)
    except ValueError as exc:
        raise HardwareError(f"expected a number, got {reply!r}") from exc


def _as_int(reply: str) -> int:
    try:
        return int(reply)
    except ValueError:
        # Some integer-valued parameters answer in exponential form.
        try:
            return int(float(reply))
        except ValueError as exc:
            raise HardwareError(f"expected an integer, got {reply!r}") from exc


def _as_bool(reply: str) -> bool:
    stripped = reply.strip()
    if stripped in ("0", "1"):
        return stripped == "1"
    raise HardwareError(f"expected a boolean 0 or 1, got {reply!r}")


def _as_str(reply: str) -> str:
    return reply.strip()


def _as_quoted_str(reply: str) -> str:
    """Strip the quotes the instrument puts around string data."""
    stripped = reply.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        return stripped[1:-1]
    return stripped


Parser = Callable[[str], Any]

# --- Readable channels -----------------------------------------------------
# channel -> (query, parser). Covers everything readable, including settings
# that are not telemetry (comparator levels, digital pin functions), because a
# write's readback is looked up here.

_QUERIES: Dict[str, Tuple[str, Parser]] = {
    # Status registers. The condition register is what is true now; the event
    # register is what became true since it was last read, which is how a
    # protection that trips and clears inside one frame still gets recorded.
    "operation_status": ("STAT:OPER:COND?", _as_int),
    "operation_events": ("STAT:OPER:EVEN?", _as_int),
    "questionable_status": ("STAT:QUES:COND?", _as_int),
    "questionable_events": ("STAT:QUES:EVEN?", _as_int),
    "questionable2_status": ("STAT:QUES2:COND?", _as_int),
    "questionable2_events": ("STAT:QUES2:EVEN?", _as_int),
    # Output state and priority mode
    "output_enabled": ("OUTP?", _as_bool),
    "priority_mode": ("FUNC?", _as_str),
    # Voltage
    "setpoint_voltage": ("VOLT?", _as_float),
    "voltage_limit": ("VOLT:LIM?", _as_float),
    "voltage_mode": ("VOLT:MODE?", _as_str),
    "triggered_voltage": ("VOLT:TRIG?", _as_float),
    "voltage_slew": ("VOLT:SLEW?", _as_float),
    "voltage_slew_max": ("VOLT:SLEW:MAX?", _as_bool),
    "ovp_level": ("VOLT:PROT?", _as_float),
    "voltage_priority_resistance": ("VOLT:RES?", _as_float),
    "voltage_priority_resistance_enabled": ("VOLT:RES:STAT?", _as_bool),
    # Current
    "setpoint_current": ("CURR?", _as_float),
    "current_limit": ("CURR:LIM?", _as_float),
    "current_limit_negative": ("CURR:LIM:NEG?", _as_float),
    "current_mode": ("CURR:MODE?", _as_str),
    "triggered_current": ("CURR:TRIG?", _as_float),
    "current_slew": ("CURR:SLEW?", _as_float),
    "current_slew_max": ("CURR:SLEW:MAX?", _as_bool),
    "ocp_enabled": ("CURR:PROT:STAT?", _as_bool),
    "ocp_delay": ("CURR:PROT:DEL?", _as_float),
    "ocp_delay_start": ("CURR:PROT:DEL:STAR?", _as_str),
    "current_sharing": ("CURR:SHAR?", _as_bool),
    "resistance": ("RES?", _as_float),
    "resistance_enabled": ("RES:STAT?", _as_bool),
    # Output and protection
    "protection_mode": ("OUTP:PROT:MODE?", _as_str),
    "protection_coupling": ("OUTP:PROT:COUP?", _as_bool),
    "watchdog_enabled": ("OUTP:PROT:WDOG?", _as_bool),
    "watchdog_delay": ("OUTP:PROT:WDOG:DEL?", _as_int),
    "user_protection_enabled": ("OUTP:PROT:USER?", _as_bool),
    "user_protection_source": ("OUTP:PROT:USER:SOUR?", _as_str),
    "inhibit_mode": ("OUTP:INH:MODE?", _as_str),
    "output_delay_rise": ("OUTP:DEL:RISE?", _as_float),
    "output_delay_fall": ("OUTP:DEL:FALL?", _as_float),
    "output_coupling": ("OUTP:COUP?", _as_bool),
    "output_coupling_delay_offset": ("OUTP:COUP:DOFF?", _as_float),
    "relay_lock": ("OUTP:REL:LOCK?", _as_bool),
    "power_on_state": ("OUTP:PON:STAT?", _as_str),
    # Measurement configuration
    "sense_fault_detection": ("SENS:FAUL:STAT?", _as_bool),
    "sense_function_voltage": ("SENS:FUNC:VOLT?", _as_bool),
    "sense_function_current": ("SENS:FUNC:CURR?", _as_bool),
    "nplc": ("SENS:SWE:NPLC?", _as_float),
    "voltage_measurement_range": ("SENS:VOLT:RANG?", _as_float),
    "current_measurement_range": ("SENS:CURR:RANG?", _as_float),
    "current_measurement_autorange": ("SENS:CURR:RANG:AUTO?", _as_bool),
    # Interface and display
    "display_enabled": ("DISP?", _as_bool),
    "display_view": ("DISP:VIEW?", _as_str),
    "digital_input": ("DIG:INP:DATA?", _as_int),
    # Measurements. MEASure:VOLTage? performs the acquisition; the two FETCh
    # queries read the same one, so all three describe a single instant. Their
    # order in the frame is therefore load-bearing.
    "voltage": ("MEAS:VOLT?", _as_float),
    "current": ("FETC:CURR?", _as_float),
    "power": ("FETC:POW?", _as_float),
    "amp_hours": ("FETC:AHO?", _as_float),
    "watt_hours": ("FETC:WHO?", _as_float),
    # Temperatures
    "ambient_temperature": ("SYST:TEMP:AMB?", _as_float),
    "over_temperature_margin": ("OUTP:PROT:TEMP:MARG?", _as_float),
}

# Settings that are readable but not streamed: configuration for the comparator,
# expression and digital-port subsystems. Not in the frame because they are
# configured once and would otherwise add 60 columns to every recorded row;
# reachable through their own read_* actions, and used as write readbacks.
for _n in THRESHOLD_COMPARATORS:
    _QUERIES[f"threshold_function_{_n}"] = (f"SENS:THR{_n}:FUNC?", _as_str)
    _QUERIES[f"threshold_operation_{_n}"] = (f"SENS:THR{_n}:OPER?", _as_str)
    _QUERIES[f"threshold_voltage_{_n}"] = (f"SENS:THR{_n}:VOLT?", _as_float)
    _QUERIES[f"threshold_current_{_n}"] = (f"SENS:THR{_n}:CURR?", _as_float)
    _QUERIES[f"threshold_power_{_n}"] = (f"SENS:THR{_n}:POW?", _as_float)
    _QUERIES[f"threshold_amp_hour_{_n}"] = (f"SENS:THR{_n}:AHO?", _as_float)
    _QUERIES[f"threshold_watt_hour_{_n}"] = (f"SENS:THR{_n}:WHO?", _as_float)
for _n in DIGITAL_PINS:
    _QUERIES[f"digital_pin_function_{_n}"] = (f"DIG:PIN{_n}:FUNC?", _as_str)
    _QUERIES[f"digital_pin_polarity_{_n}"] = (f"DIG:PIN{_n}:POL?", _as_str)
for _n in SIGNAL_EXPRESSIONS:
    _QUERIES[f"signal_expression_{_n}"] = (f"SYST:SIGN:DEF? EXPR{_n}", _as_quoted_str)
_QUERIES["digital_output_data"] = ("DIG:OUTP:DATA?", _as_int)
_QUERIES["digital_trigger_out_bus"] = ("DIG:TOUT:BUS?", _as_bool)
_QUERIES["transient_trigger_source"] = ("TRIG:TRAN:SOUR?", _as_str)
_QUERIES["acquire_trigger_source"] = ("TRIG:ACQ:SOUR?", _as_str)
_QUERIES["arb_trigger_source"] = ("TRIG:ARB:SOUR?", _as_str)
_QUERIES["acquire_trigger_voltage"] = ("TRIG:ACQ:VOLT?", _as_float)
_QUERIES["acquire_trigger_voltage_slope"] = ("TRIG:ACQ:VOLT:SLOP?", _as_str)
_QUERIES["acquire_trigger_current"] = ("TRIG:ACQ:CURR?", _as_float)
_QUERIES["acquire_trigger_current_slope"] = ("TRIG:ACQ:CURR:SLOP?", _as_str)
_QUERIES["acquire_trigger_out"] = ("TRIG:ACQ:TOUT?", _as_bool)
_QUERIES["step_trigger_out"] = ("STEP:TOUT?", _as_bool)
_QUERIES["transient_continuous"] = ("INIT:CONT:TRAN?", _as_bool)
_QUERIES["operation_enable"] = ("STAT:OPER:ENAB?", _as_int)
_QUERIES["questionable_enable"] = ("STAT:QUES:ENAB?", _as_int)
_QUERIES["questionable2_enable"] = ("STAT:QUES2:ENAB?", _as_int)
_QUERIES["operation_ptr"] = ("STAT:OPER:PTR?", _as_int)
_QUERIES["operation_ntr"] = ("STAT:OPER:NTR?", _as_int)
_QUERIES["questionable_ptr"] = ("STAT:QUES:PTR?", _as_int)
_QUERIES["questionable_ntr"] = ("STAT:QUES:NTR?", _as_int)
_QUERIES["questionable2_ptr"] = ("STAT:QUES2:PTR?", _as_int)
_QUERIES["questionable2_ntr"] = ("STAT:QUES2:NTR?", _as_int)
_QUERIES["display_saver"] = ("DISP:SAV:STAT?", _as_bool)
_QUERIES["lxi_identify"] = ("LXI:IDEN:STAT?", _as_bool)
_QUERIES["remote_state"] = ("SYST:COMM:RLST?", _as_str)
for _n in (1, 2):
    _QUERIES[f"operation_user_source_{_n}"] = (f"STAT:OPER:USER{_n}:SOUR?", _as_str)

# --- The frame -------------------------------------------------------------
# One compound message, in this order. Status first so instrument state is read
# before the 21 ms measurement rather than after it; the measurement trio last,
# with MEASure ahead of its two FETCh readers.
_FRAME_CHANNELS: Tuple[str, ...] = (
    *(f"{register}_{kind}" for register in STATUS_REGISTERS for kind in ("status", "events")),
    *SETTING_CHANNELS,
    *METER_CHANNELS,
    *TEMPERATURE_CHANNELS,
)
_FRAME_QUERIES: Tuple[str, ...] = tuple(_QUERIES[channel][0] for channel in _FRAME_CHANNELS)

ERROR_QUERY = "SYST:ERR?"
NO_ERROR_PREFIXES = ("+0,", "0,")
"""How an empty error queue answers: `+0,"No error"`."""


# --- Clamping --------------------------------------------------------------
# action -> (channel whose instrument MIN/MAX bound it, whether its negative
# bound is additionally held to the declared dissipator count). Only quantities
# that carry energy are clamped - see the module docstring.
_CLAMPED: Dict[str, Tuple[str, bool]] = {
    "set_voltage": ("setpoint_voltage", False),
    "set_voltage_limit": ("voltage_limit", False),
    "set_triggered_voltage": ("triggered_voltage", False),
    "set_ovp": ("ovp_level", False),
    "set_current": ("setpoint_current", True),
    "set_current_limit": ("current_limit", False),
    "set_current_limit_negative": ("current_limit_negative", True),
    "set_triggered_current": ("triggered_current", True),
}

_CLAMP_QUANTITY: Dict[str, str] = {
    "set_voltage": "voltage",
    "set_voltage_limit": "voltage",
    "set_triggered_voltage": "voltage",
    "set_ovp": "voltage",
    "set_current": "current",
    "set_current_limit": "current",
    "set_current_limit_negative": "sink_current",
    "set_triggered_current": "current",
}
"""Which sticky `clamped_*` channel an action's clamp is recorded under when it
hits the upper bound. A clamp at the lower bound of a signed quantity is
recorded under `sink_current` instead, since that is the direction it was
heading."""

CEILING_VOLTAGE_V = 80.0
CEILING_CURRENT_A = 25.0
"""This model's rated output, and the most this driver will command.

The instrument reports a programmable range wider than its rating - 81.6 V and
25.5 A - so a setpoint is held to the smaller of the two. These are properties of
an N6974A rather than of any stand: a testbed that needs a tighter ceiling than
the hardware's own owns that limit itself, because the same driver serves a stand
running a higher bus."""

UNRATED_CHANNELS = frozenset({"ovp_level"})
"""The clamped channels the rating does NOT bind, which keep the instrument's own
reported range instead.

Every other clamped channel gets the rating, so a clamped action added later is
held to it without anyone remembering to opt in - the failure of an opt-in list
would be a setpoint quietly clamped at 81.6 V again, which is the thing the
rating exists to prevent.

`ovp_level` is here because it is a protection threshold rather than an output
setpoint: one set above the rated output is a legitimate way to leave
over-voltage protection out of the way, so it keeps its 0..96 V range."""


# --- Command tables --------------------------------------------------------
# action -> (command template, value cast, readback channels). The template's
# `{value}` is filled from the action's `value` parameter, and the readback
# channels are re-read after the write - in their own message, since a setting
# query alongside its write answers one step stale on this instrument.
_VALUE_COMMANDS: Dict[str, Tuple[str, Callable[[Any], Any], Tuple[str, ...]]] = {}
# action -> (command, readback channels). No parameters.
_BARE_COMMANDS: Dict[str, Tuple[str, Tuple[str, ...]]] = {}
# action -> (query, parser). Returns a value; changes nothing.
_QUERY_ACTIONS: Dict[str, Tuple[str, Parser]] = {}


def _to_bool_arg(value: Any) -> int:
    """SCPI booleans go out as 0/1. Accepts Python truthiness, and the strings
    a JSON wire may deliver."""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("on", "1", "true", "yes"):
            return 1
        if lowered in ("off", "0", "false", "no"):
            return 0
        raise HardwareError(f"expected a boolean, got {value!r}")
    return 1 if value else 0


def _keyword(*allowed: str) -> Callable[[Any], str]:
    """Cast for a discrete parameter, rejecting anything outside the documented
    keywords before it reaches the wire - the instrument would answer an
    unrecognised one with silence and discard the whole message."""

    def cast(value: Any) -> str:
        text = str(value).strip().upper()
        if text not in allowed:
            raise HardwareError(f"expected one of {', '.join(allowed)}, got {value!r}")
        return text

    return cast


def _slew(value: Any) -> str:
    """A slew rate, which accepts the keywords MAXimum and INFinity as well as a
    number. Passed through as a keyword when it is one, so `MAX` reaches the
    instrument rather than failing a float conversion."""
    if isinstance(value, str) and value.strip().upper() in ("MAX", "MAXIMUM", "INF", "INFINITY", "MIN", "MINIMUM"):
        return value.strip().upper()
    return repr(float(value))


def _expression_source(value: Any) -> str:
    """An EXPRession<1-8> or NONE, for the sources that route a user-defined
    signal into protection, status or output coupling."""
    text = str(value).strip().upper()
    if text == "NONE":
        return "NONE"
    digits = text.removeprefix("EXPRESSION").removeprefix("EXPR")
    if digits.isdigit() and int(digits) in SIGNAL_EXPRESSIONS:
        return f"EXPR{int(digits)}"
    raise HardwareError(f"expected NONE or EXPR1-EXPR{max(SIGNAL_EXPRESSIONS)}, got {value!r}")


def _quoted(value: Any) -> str:
    """String program data, quoted as the instrument requires. A double quote in
    the value is escaped by doubling it, per the SCPI string rules."""
    text = str(value).replace('"', '""')
    return f'"{text}"'


def _register_mask(value: Any) -> int:
    mask = int(value)
    if not 0 <= mask <= 0xFFFF:
        raise HardwareError(f"a status register mask must be 0-65535, got {value!r}")
    return mask


def _state_slot(value: Any) -> int:
    slot = int(value)
    if not 0 <= slot <= 9:
        raise HardwareError(f"an instrument state slot must be 0-9, got {value!r}")
    return slot


# Output regulation
_VALUE_COMMANDS["set_priority_mode"] = ("FUNC {value}", _keyword("VOLT", "VOLTAGE", "CURR", "CURRENT"), ("priority_mode",))
_VALUE_COMMANDS["set_voltage"] = ("VOLT {value}", float, ("setpoint_voltage",))
_VALUE_COMMANDS["set_voltage_limit"] = ("VOLT:LIM {value}", float, ("voltage_limit",))
_VALUE_COMMANDS["set_triggered_voltage"] = ("VOLT:TRIG {value}", float, ("triggered_voltage",))
_VALUE_COMMANDS["set_voltage_mode"] = ("VOLT:MODE {value}", _keyword("FIX", "FIXED", "STEP"), ("voltage_mode",))
_VALUE_COMMANDS["set_voltage_slew"] = ("VOLT:SLEW {value}", _slew, ("voltage_slew",))
_VALUE_COMMANDS["set_voltage_slew_max"] = ("VOLT:SLEW:MAX {value}", _to_bool_arg, ("voltage_slew_max",))
_VALUE_COMMANDS["set_ovp"] = ("VOLT:PROT {value}", float, ("ovp_level",))
_VALUE_COMMANDS["set_voltage_priority_resistance"] = ("VOLT:RES {value}", float, ("voltage_priority_resistance",))
_VALUE_COMMANDS["set_voltage_priority_resistance_state"] = ("VOLT:RES:STAT {value}", _to_bool_arg, ("voltage_priority_resistance_enabled",))
_VALUE_COMMANDS["set_current"] = ("CURR {value}", float, ("setpoint_current",))
_VALUE_COMMANDS["set_current_limit"] = ("CURR:LIM {value}", float, ("current_limit",))
_VALUE_COMMANDS["set_current_limit_negative"] = ("CURR:LIM:NEG {value}", float, ("current_limit_negative",))
_VALUE_COMMANDS["set_triggered_current"] = ("CURR:TRIG {value}", float, ("triggered_current",))
_VALUE_COMMANDS["set_current_mode"] = ("CURR:MODE {value}", _keyword("FIX", "FIXED", "STEP"), ("current_mode",))
_VALUE_COMMANDS["set_current_slew"] = ("CURR:SLEW {value}", _slew, ("current_slew",))
_VALUE_COMMANDS["set_current_slew_max"] = ("CURR:SLEW:MAX {value}", _to_bool_arg, ("current_slew_max",))
_VALUE_COMMANDS["set_current_sharing"] = ("CURR:SHAR {value}", _to_bool_arg, ("current_sharing",))
_VALUE_COMMANDS["set_resistance"] = ("RES {value}", float, ("resistance",))
_VALUE_COMMANDS["set_resistance_state"] = ("RES:STAT {value}", _to_bool_arg, ("resistance_enabled",))

# Output state and sequencing
_VALUE_COMMANDS["enable_output"] = ("OUTP {value}", _to_bool_arg, ("output_enabled",))
_VALUE_COMMANDS["set_output_delay_rise"] = ("OUTP:DEL:RISE {value}", float, ("output_delay_rise",))
_VALUE_COMMANDS["set_output_delay_fall"] = ("OUTP:DEL:FALL {value}", float, ("output_delay_fall",))
_VALUE_COMMANDS["set_output_coupling"] = ("OUTP:COUP {value}", _to_bool_arg, ("output_coupling",))
_VALUE_COMMANDS["set_output_coupling_delay_offset"] = ("OUTP:COUP:DOFF {value}", float, ("output_coupling_delay_offset",))
_VALUE_COMMANDS["set_output_coupling_on_source"] = ("OUTP:COUP:ON:SOUR {value}", _expression_source, ())
_VALUE_COMMANDS["set_output_coupling_off_source"] = ("OUTP:COUP:OFF:SOUR {value}", _expression_source, ())
_VALUE_COMMANDS["set_power_on_state"] = ("OUTP:PON:STAT {value}", _keyword("RST", "RCL0"), ("power_on_state",))
_VALUE_COMMANDS["set_relay_lock"] = ("OUTP:REL:LOCK {value}", _to_bool_arg, ("relay_lock",))

# Protection
_BARE_COMMANDS["clear_protection"] = ("OUTP:PROT:CLE", ("questionable_status", "output_enabled"))
_VALUE_COMMANDS["set_protection_mode"] = ("OUTP:PROT:MODE {value}", _keyword("LOWZ", "HIGHZ"), ("protection_mode",))
_VALUE_COMMANDS["set_protection_coupling"] = ("OUTP:PROT:COUP {value}", _to_bool_arg, ("protection_coupling",))
_VALUE_COMMANDS["set_ocp_state"] = ("CURR:PROT:STAT {value}", _to_bool_arg, ("ocp_enabled",))
_VALUE_COMMANDS["set_ocp_delay"] = ("CURR:PROT:DEL {value}", float, ("ocp_delay",))
_VALUE_COMMANDS["set_ocp_delay_start"] = ("CURR:PROT:DEL:STAR {value}", _keyword("SCH", "SCHANGE", "CCTR", "CCTRANS"), ("ocp_delay_start",))
_VALUE_COMMANDS["set_watchdog_state"] = ("OUTP:PROT:WDOG {value}", _to_bool_arg, ("watchdog_enabled",))
_VALUE_COMMANDS["set_watchdog_delay"] = ("OUTP:PROT:WDOG:DEL {value}", int, ("watchdog_delay",))
_VALUE_COMMANDS["set_user_protection_state"] = ("OUTP:PROT:USER {value}", _to_bool_arg, ("user_protection_enabled",))
_VALUE_COMMANDS["set_user_protection_source"] = ("OUTP:PROT:USER:SOUR {value}", _expression_source, ("user_protection_source",))
_VALUE_COMMANDS["set_inhibit_mode"] = ("OUTP:INH:MODE {value}", _keyword("LATC", "LATCHING", "LIVE", "OFF"), ("inhibit_mode",))

# Measurement configuration and readback
_VALUE_COMMANDS["set_nplc"] = ("SENS:SWE:NPLC {value}", float, ("nplc",))
_VALUE_COMMANDS["set_voltage_measurement_range"] = ("SENS:VOLT:RANG {value}", float, ("voltage_measurement_range",))
_VALUE_COMMANDS["set_current_measurement_range"] = ("SENS:CURR:RANG {value}", float, ("current_measurement_range",))
_VALUE_COMMANDS["set_current_measurement_autorange"] = ("SENS:CURR:RANG:AUTO {value}", _to_bool_arg, ("current_measurement_autorange",))
_VALUE_COMMANDS["set_sense_function_voltage"] = ("SENS:FUNC:VOLT {value}", _to_bool_arg, ("sense_function_voltage",))
_VALUE_COMMANDS["set_sense_function_current"] = ("SENS:FUNC:CURR {value}", _to_bool_arg, ("sense_function_current",))
_VALUE_COMMANDS["set_sense_fault_detection"] = ("SENS:FAUL:STAT {value}", _to_bool_arg, ("sense_fault_detection",))
_BARE_COMMANDS["reset_amp_hours"] = ("SENS:AHO:RES", ("amp_hours",))
_BARE_COMMANDS["reset_watt_hours"] = ("SENS:WHO:RES", ("watt_hours",))
_QUERY_ACTIONS["read_voltage_rms"] = ("MEAS:VOLT:ACDC?", _as_float)
_QUERY_ACTIONS["read_voltage_max"] = ("MEAS:VOLT:MAX?", _as_float)
_QUERY_ACTIONS["read_voltage_min"] = ("MEAS:VOLT:MIN?", _as_float)
_QUERY_ACTIONS["read_voltage_high"] = ("MEAS:VOLT:HIGH?", _as_float)
_QUERY_ACTIONS["read_voltage_low"] = ("MEAS:VOLT:LOW?", _as_float)
_QUERY_ACTIONS["read_current_rms"] = ("MEAS:CURR:ACDC?", _as_float)
_QUERY_ACTIONS["read_current_max"] = ("MEAS:CURR:MAX?", _as_float)
_QUERY_ACTIONS["read_current_min"] = ("MEAS:CURR:MIN?", _as_float)
_QUERY_ACTIONS["read_current_high"] = ("MEAS:CURR:HIGH?", _as_float)
_QUERY_ACTIONS["read_current_low"] = ("MEAS:CURR:LOW?", _as_float)
_QUERY_ACTIONS["read_max_coupling_delay_offset"] = ("OUTP:COUP:MAX:DOFF?", _as_float)

# Signal comparators and expressions
_THRESHOLD_LEVEL_CHANNEL = {
    "VOLT": "threshold_voltage", "CURR": "threshold_current", "POW": "threshold_power",
    "AHO": "threshold_amp_hour", "WHO": "threshold_watt_hour",
}
for _n in THRESHOLD_COMPARATORS:
    _VALUE_COMMANDS[f"set_threshold_function_{_n}"] = (
        f"SENS:THR{_n}:FUNC {{value}}", _keyword("VOLT", "CURR", "POW", "AHO", "WHO"), (f"threshold_function_{_n}",))
    _VALUE_COMMANDS[f"set_threshold_operation_{_n}"] = (
        f"SENS:THR{_n}:OPER {{value}}", _keyword("GT", "LT"), (f"threshold_operation_{_n}",))
    _VALUE_COMMANDS[f"set_threshold_voltage_{_n}"] = (f"SENS:THR{_n}:VOLT {{value}}", float, (f"threshold_voltage_{_n}",))
    _VALUE_COMMANDS[f"set_threshold_current_{_n}"] = (f"SENS:THR{_n}:CURR {{value}}", float, (f"threshold_current_{_n}",))
    _VALUE_COMMANDS[f"set_threshold_power_{_n}"] = (f"SENS:THR{_n}:POW {{value}}", float, (f"threshold_power_{_n}",))
    _VALUE_COMMANDS[f"set_threshold_amp_hour_{_n}"] = (f"SENS:THR{_n}:AHO {{value}}", float, (f"threshold_amp_hour_{_n}",))
    _VALUE_COMMANDS[f"set_threshold_watt_hour_{_n}"] = (f"SENS:THR{_n}:WHO {{value}}", float, (f"threshold_watt_hour_{_n}",))
    _QUERY_ACTIONS[f"read_threshold_function_{_n}"] = _QUERIES[f"threshold_function_{_n}"]
    _QUERY_ACTIONS[f"read_threshold_operation_{_n}"] = _QUERIES[f"threshold_operation_{_n}"]
for _n in SIGNAL_EXPRESSIONS:
    _VALUE_COMMANDS[f"set_signal_expression_{_n}"] = (
        f"SYST:SIGN:DEF EXPR{_n},{{value}}", _quoted, (f"signal_expression_{_n}",))
    _QUERY_ACTIONS[f"read_signal_expression_{_n}"] = _QUERIES[f"signal_expression_{_n}"]

# Digital port
_VALUE_COMMANDS["set_digital_output_data"] = ("DIG:OUTP:DATA {value}", int, ("digital_output_data",))
_QUERY_ACTIONS["read_digital_output_data"] = _QUERIES["digital_output_data"]
_VALUE_COMMANDS["set_digital_trigger_out_bus"] = ("DIG:TOUT:BUS {value}", _to_bool_arg, ("digital_trigger_out_bus",))
# The function a digital pin will accept depends on WHICH pin it is, which the
# guide's single flat list does not say. Measured, by offering every function to
# every pin and reading the error queue: FAULt is pin 1 only, INHibit is pin 3
# only, and ONCouple/OFFCouple are pins 4-7 only. An invalid combination answers
# `-224,"Illegal parameter value"` rather than silence, so it is not the
# message-discarding hazard an unknown keyword is - but rejecting it here names
# the pin, which the instrument's error does not.
_UNIVERSAL_PIN_FUNCTIONS = ("DIO", "DINP", "DINPUT", "TOUT", "TOUTPUT", "TINP", "TINPUT",
                            *(f"EXPR{n}" for n in SIGNAL_EXPRESSIONS))
_PIN_FUNCTIONS_BY_PIN: Dict[int, Tuple[str, ...]] = {
    1: (*_UNIVERSAL_PIN_FUNCTIONS, "FAUL", "FAULT"),
    2: _UNIVERSAL_PIN_FUNCTIONS,
    3: (*_UNIVERSAL_PIN_FUNCTIONS, "INH", "INHIBIT"),
    **{pin: (*_UNIVERSAL_PIN_FUNCTIONS, "ONC", "ONCOUPLE", "OFFC", "OFFCOUPLE")
       for pin in (4, 5, 6, 7)},
}
for _n in DIGITAL_PINS:
    _VALUE_COMMANDS[f"set_digital_pin_function_{_n}"] = (
        f"DIG:PIN{_n}:FUNC {{value}}", _keyword(*_PIN_FUNCTIONS_BY_PIN[_n]),
        # Reads the polarity back as well: setting ONCouple or OFFCouple moves
        # the pin's polarity as a side effect, measured.
        (f"digital_pin_function_{_n}", f"digital_pin_polarity_{_n}"))
    _VALUE_COMMANDS[f"set_digital_pin_polarity_{_n}"] = (
        f"DIG:PIN{_n}:POL {{value}}", _keyword("POS", "POSITIVE", "NEG", "NEGATIVE"), (f"digital_pin_polarity_{_n}",))
    _QUERY_ACTIONS[f"read_digital_pin_function_{_n}"] = _QUERIES[f"digital_pin_function_{_n}"]
    _QUERY_ACTIONS[f"read_digital_pin_polarity_{_n}"] = _QUERIES[f"digital_pin_polarity_{_n}"]

# Transient and acquisition trigger systems
# Each trigger subsystem takes a different set of sources, so they get different
# keyword lists rather than one permissive union: an unrecognised keyword is
# answered with silence and discards the whole message, so it is worth rejecting
# before it is sent. The acquisition system accepts the measurable quantities and
# the transient system as sources but NOT IMMediate; the transient and Arb
# systems accept IMMediate and none of the quantities.
_COMMON_TRIGGER_SOURCES = (*(f"EXPR{n}" for n in SIGNAL_EXPRESSIONS),
                           *(f"PIN{n}" for n in DIGITAL_PINS), "BUS", "EXT", "EXTERNAL")
_TRANSIENT_TRIGGER_SOURCES = (*_COMMON_TRIGGER_SOURCES, "IMM", "IMMEDIATE")
_ACQUIRE_TRIGGER_SOURCES = (*_COMMON_TRIGGER_SOURCES, "CURR1", "CURRENT1", "VOLT1", "VOLTAGE1",
                            "TRAN1", "TRANSIENT1")
_BARE_COMMANDS["initiate_transient"] = ("INIT:TRAN", ())
_VALUE_COMMANDS["initiate_transient_continuous"] = ("INIT:CONT:TRAN {value}", _to_bool_arg, ("transient_continuous",))
_BARE_COMMANDS["abort_transient"] = ("ABOR:TRAN", ())
_BARE_COMMANDS["trigger_transient"] = ("TRIG:TRAN:IMM", ())
_VALUE_COMMANDS["set_transient_trigger_source"] = ("TRIG:TRAN:SOUR {value}", _keyword(*_TRANSIENT_TRIGGER_SOURCES), ("transient_trigger_source",))
_QUERY_ACTIONS["read_transient_trigger_source"] = _QUERIES["transient_trigger_source"]
_VALUE_COMMANDS["set_step_trigger_out"] = ("STEP:TOUT {value}", _to_bool_arg, ("step_trigger_out",))
_BARE_COMMANDS["initiate_acquire"] = ("INIT:ACQ", ())
_BARE_COMMANDS["abort_acquire"] = ("ABOR:ACQ", ())
_BARE_COMMANDS["trigger_acquire"] = ("TRIG:ACQ:IMM", ())
_VALUE_COMMANDS["set_acquire_trigger_source"] = ("TRIG:ACQ:SOUR {value}", _keyword(*_ACQUIRE_TRIGGER_SOURCES), ("acquire_trigger_source",))
_QUERY_ACTIONS["read_acquire_trigger_source"] = _QUERIES["acquire_trigger_source"]
_VALUE_COMMANDS["set_acquire_trigger_voltage"] = ("TRIG:ACQ:VOLT {value}", float, ("acquire_trigger_voltage",))
_VALUE_COMMANDS["set_acquire_trigger_voltage_slope"] = ("TRIG:ACQ:VOLT:SLOP {value}", _keyword("POS", "POSITIVE", "NEG", "NEGATIVE"), ("acquire_trigger_voltage_slope",))
_VALUE_COMMANDS["set_acquire_trigger_current"] = ("TRIG:ACQ:CURR {value}", float, ("acquire_trigger_current",))
_VALUE_COMMANDS["set_acquire_trigger_current_slope"] = ("TRIG:ACQ:CURR:SLOP {value}", _keyword("POS", "POSITIVE", "NEG", "NEGATIVE"), ("acquire_trigger_current_slope",))
_VALUE_COMMANDS["set_acquire_trigger_out"] = ("TRIG:ACQ:TOUT {value}", _to_bool_arg, ("acquire_trigger_out",))
_QUERY_ACTIONS["read_acquire_trigger_count"] = ("TRIG:ACQ:IND:COUN?", _as_int)
_QUERY_ACTIONS["read_acquire_trigger_indices"] = ("TRIG:ACQ:IND:DATA?", _as_str)
_VALUE_COMMANDS["set_arb_trigger_source"] = ("TRIG:ARB:SOUR {value}", _keyword(*_TRANSIENT_TRIGGER_SOURCES), ("arb_trigger_source",))
_QUERY_ACTIONS["read_arb_trigger_source"] = _QUERIES["arb_trigger_source"]

# Status registers
_QUERY_ACTIONS["read_operation_events"] = _QUERIES["operation_events"]
_QUERY_ACTIONS["read_questionable_events"] = _QUERIES["questionable_events"]
_QUERY_ACTIONS["read_questionable2_events"] = _QUERIES["questionable2_events"]
for _register in ("operation", "questionable", "questionable2"):
    _scpi = {"operation": "OPER", "questionable": "QUES", "questionable2": "QUES2"}[_register]
    _VALUE_COMMANDS[f"set_{_register}_enable"] = (f"STAT:{_scpi}:ENAB {{value}}", _register_mask, (f"{_register}_enable",))
    _VALUE_COMMANDS[f"set_{_register}_ptr"] = (f"STAT:{_scpi}:PTR {{value}}", _register_mask, (f"{_register}_ptr",))
    _VALUE_COMMANDS[f"set_{_register}_ntr"] = (f"STAT:{_scpi}:NTR {{value}}", _register_mask, (f"{_register}_ntr",))
_QUERY_ACTIONS["read_operation_enable"] = _QUERIES["operation_enable"]
_QUERY_ACTIONS["read_questionable_enable"] = _QUERIES["questionable_enable"]
_QUERY_ACTIONS["read_questionable2_enable"] = _QUERIES["questionable2_enable"]
_QUERY_ACTIONS["read_operation_ptr"] = _QUERIES["operation_ptr"]
_QUERY_ACTIONS["read_questionable_ptr"] = _QUERIES["questionable_ptr"]
_QUERY_ACTIONS["read_questionable2_ptr"] = _QUERIES["questionable2_ptr"]
_QUERY_ACTIONS["read_operation_ntr"] = _QUERIES["operation_ntr"]
_QUERY_ACTIONS["read_questionable_ntr"] = _QUERIES["questionable_ntr"]
_QUERY_ACTIONS["read_questionable2_ntr"] = _QUERIES["questionable2_ntr"]
_BARE_COMMANDS["preset_status"] = ("STAT:PRES", ("operation_ptr", "questionable_ptr"))
for _n in (1, 2):
    _VALUE_COMMANDS[f"set_operation_user_source_{_n}"] = (
        f"STAT:OPER:USER{_n}:SOUR {{value}}", _expression_source, (f"operation_user_source_{_n}",))
    _QUERY_ACTIONS[f"read_operation_user_source_{_n}"] = _QUERIES[f"operation_user_source_{_n}"]

# Errors, identity and system
_QUERY_ACTIONS["read_error"] = (ERROR_QUERY, _as_str)
_QUERY_ACTIONS["read_identity"] = ("*IDN?", _as_str)
_QUERY_ACTIONS["read_options"] = ("*OPT?", _as_str)
_QUERY_ACTIONS["read_learn_string"] = ("*LRN?", _as_str)
_QUERY_ACTIONS["read_scpi_version"] = ("SYST:VERS?", _as_str)
_QUERY_ACTIONS["read_line_frequency"] = ("SYST:LFR?", _as_float)
_VALUE_COMMANDS["set_line_frequency_mode"] = ("SYST:LFR:MODE {value}", _keyword("AUTO", "MAN50", "MAN60"), ())
_QUERY_ACTIONS["read_calibration_date"] = ("CAL:DATE?", _as_quoted_str)
_QUERY_ACTIONS["read_calibration_count"] = ("CAL:COUN?", _as_int)
_QUERY_ACTIONS["read_power_limit"] = ("POW:LIM?", _as_float)
_QUERY_ACTIONS["read_data_format"] = ("FORM?", _as_str)
_QUERY_ACTIONS["read_byte_order"] = ("FORM:BORD?", _as_str)
_QUERY_ACTIONS["read_ambient_temperature"] = _QUERIES["ambient_temperature"]
_QUERY_ACTIONS["read_control_socket_port"] = ("SYST:COMM:TCP:CONT?", _as_int)
_VALUE_COMMANDS["set_remote_state"] = ("SYST:COMM:RLST {value}", _keyword("LOC", "LOCAL", "REM", "REMOTE", "RWL", "RWLOCK"), ("remote_state",))
_QUERY_ACTIONS["read_remote_state"] = _QUERIES["remote_state"]
_VALUE_COMMANDS["set_display_state"] = ("DISP {value}", _to_bool_arg, ("display_enabled",))
_VALUE_COMMANDS["set_display_view"] = ("DISP:VIEW {value}", _keyword("METER_VI", "METER_VP", "METER_VIP"), ("display_view",))
_VALUE_COMMANDS["set_display_saver"] = ("DISP:SAV:STAT {value}", _to_bool_arg, ("display_saver",))
_QUERY_ACTIONS["read_display_saver"] = _QUERIES["display_saver"]
_VALUE_COMMANDS["set_lxi_identify"] = ("LXI:IDEN:STAT {value}", _to_bool_arg, ("lxi_identify",))
_QUERY_ACTIONS["read_lxi_identify"] = _QUERIES["lxi_identify"]
_VALUE_COMMANDS["set_date"] = ("SYST:DATE {value}", _as_str, ())
_VALUE_COMMANDS["set_time"] = ("SYST:TIME {value}", _as_str, ())
_QUERY_ACTIONS["read_date"] = ("SYST:DATE?", _as_str)
_QUERY_ACTIONS["read_time"] = ("SYST:TIME?", _as_str)

# IEEE-488 common commands
_BARE_COMMANDS["clear_status"] = ("*CLS", ())
_BARE_COMMANDS["reset"] = ("*RST", ("output_enabled", "setpoint_voltage", "current_limit"))
_VALUE_COMMANDS["save_state"] = ("*SAV {value}", _state_slot, ())
_VALUE_COMMANDS["recall_state"] = ("*RCL {value}", _state_slot, ("setpoint_voltage", "current_limit", "current_limit_negative", "output_enabled"))
_BARE_COMMANDS["trigger"] = ("*TRG", ())
_BARE_COMMANDS["wait_operation_complete"] = ("*WAI", ())
_BARE_COMMANDS["operation_complete"] = ("*OPC", ())
_QUERY_ACTIONS["read_operation_complete"] = ("*OPC?", _as_int)
_QUERY_ACTIONS["self_test"] = ("*TST?", _as_int)
_QUERY_ACTIONS["read_event_status"] = ("*ESR?", _as_int)
_VALUE_COMMANDS["set_event_status_enable"] = ("*ESE {value}", _register_mask, ())
_QUERY_ACTIONS["read_event_status_enable"] = ("*ESE?", _as_int)
_VALUE_COMMANDS["set_service_request_enable"] = ("*SRE {value}", _register_mask, ())
_QUERY_ACTIONS["read_service_request_enable"] = ("*SRE?", _as_int)
_QUERY_ACTIONS["read_status_byte"] = ("*STB?", _as_int)

# Driver-side actions: not instrument commands at all, or not a single one.
_DRIVER_ACTIONS = (
    "clear_clamped_latch",
    "read_ratings",
    "drain_errors",
    # Not verifiable like every other write: it takes the link with it, so it
    # cannot be followed by an error check in the same message.
    "reboot",
    # Two queries rather than one: which quantity a comparator watches decides
    # which of its five level registers is the one in use, so the function is
    # read first and the matching level second.
    *(f"read_threshold_level_{n}" for n in THRESHOLD_COMPARATORS),
)


def _validate_channel_coverage() -> None:
    """Runs once at import. The declared surface in n6974a_channels.py and the
    tables above are two separate structures that must agree exactly: every
    declared channel needs an implementation and vice versa. Nothing else would
    catch a channel added to one and forgotten in the other - it would produce
    an incomplete telemetry frame, or an action that verify_actions() reports
    present while execute() rejects it. This is the static equivalent of what
    verify_channels()/verify_actions() do against a live process."""
    decoded = {stem for bits in STATUS_REGISTERS.values() for stem in bits.values()}
    decoded |= {f"{stem}_event" for stem in decoded}
    raw = {f"{register}_{kind}" for register in STATUS_REGISTERS for kind in ("status", "events")}
    if set(STATUS_CHANNELS) != decoded | raw:
        raise AssertionError(
            "the status bit tables are out of sync with STATUS_CHANNELS - "
            f"missing: {sorted((decoded | raw) - set(STATUS_CHANNELS))}, "
            f"extra: {sorted(set(STATUS_CHANNELS) - (decoded | raw))}"
        )

    instrument_read = set(SETTING_CHANNELS) | set(METER_CHANNELS) | set(TEMPERATURE_CHANNELS) | raw
    if instrument_read != set(_FRAME_CHANNELS):
        raise AssertionError(
            "the frame is out of sync with the declared channels - "
            f"missing: {sorted(instrument_read - set(_FRAME_CHANNELS))}, "
            f"extra: {sorted(set(_FRAME_CHANNELS) - instrument_read)}"
        )

    unqueried = [channel for channel in _FRAME_CHANNELS if channel not in _QUERIES]
    if unqueried:
        raise AssertionError(f"frame channels with no query: {sorted(unqueried)}")

    computed = set(DRIVER_CHANNELS)
    if set(TELEMETRY_CHANNELS) != instrument_read | decoded | computed:
        raise AssertionError(
            "TELEMETRY_CHANNELS does not equal what a frame produces - "
            f"missing: {sorted((instrument_read | decoded | computed) - set(TELEMETRY_CHANNELS))}, "
            f"extra: {sorted(set(TELEMETRY_CHANNELS) - (instrument_read | decoded | computed))}"
        )

    implemented = set(_VALUE_COMMANDS) | set(_BARE_COMMANDS) | set(_QUERY_ACTIONS) | set(_DRIVER_ACTIONS)
    if set(COMMAND_CHANNELS) != implemented:
        raise AssertionError(
            "the command tables are out of sync with COMMAND_CHANNELS - "
            f"missing: {sorted(set(COMMAND_CHANNELS) - implemented)}, "
            f"extra: {sorted(implemented - set(COMMAND_CHANNELS))}"
        )

    overlapping = (set(_VALUE_COMMANDS) & set(_BARE_COMMANDS)) | (set(_VALUE_COMMANDS) & set(_QUERY_ACTIONS))
    overlapping |= set(_BARE_COMMANDS) & set(_QUERY_ACTIONS)
    # A driver action that is ALSO in an instrument table would be silently
    # shadowed by execute()'s driver-action branch. Compared against the
    # instrument tables specifically, since `implemented` already contains the
    # driver actions and would make this vacuous.
    overlapping |= (set(_VALUE_COMMANDS) | set(_BARE_COMMANDS) | set(_QUERY_ACTIONS)) & set(_DRIVER_ACTIONS)
    if overlapping:
        raise AssertionError(f"actions appear in more than one command table: {sorted(overlapping)}")

    readbacks = {c for _, _, cs in _VALUE_COMMANDS.values() for c in cs}
    readbacks |= {c for _, cs in _BARE_COMMANDS.values() for c in cs}
    unknown = readbacks - set(_QUERIES)
    if unknown:
        raise AssertionError(f"commands claim to read back channels with no query: {sorted(unknown)}")

    if set(_CLAMPED) != set(_CLAMP_QUANTITY):
        raise AssertionError("_CLAMPED and _CLAMP_QUANTITY disagree about which actions are clamped")
    for action, (channel, _) in _CLAMPED.items():
        if action not in _VALUE_COMMANDS:
            raise AssertionError(f"{action} is clamped but is not a value command")
        if channel not in _QUERIES:
            raise AssertionError(f"{action} is clamped against {channel}, which has no query")
    if set(_CLAMP_QUANTITY.values()) - set(CLAMPED_QUANTITIES):
        raise AssertionError("a clamped action names a quantity with no clamped_* channel")
    unknown = UNRATED_CHANNELS - {channel for channel, _ in _CLAMPED.values()}
    if unknown:
        raise AssertionError(
            f"UNRATED_CHANNELS names channels nothing clamps, so the exemption does "
            f"nothing: {sorted(unknown)}"
        )


_validate_channel_coverage()


class N6974aBackend(HardwareBackend):
    """Real Keysight N6974A Advanced Power System, over ethernet on port 5025."""

    device = DEVICE_N6974A
    sample_interval_s = SAMPLE_INTERVAL_S

    def __init__(
        self,
        host: str = DEFAULT_N6974A_HOST,
        port: int = DEFAULT_PORT,
        dissipators: int = 0,
        transport: Optional[KeysightSocketTransport] = None,
    ) -> None:
        """
        dissipators: how many Keysight N7909A power dissipator units are
            connected. Required in substance even though it has a default,
            because it sets how much current this supply may sink and therefore
            how hard it can discharge whatever is attached: none means 10% of
            rating, one means 50%, two means 100%. It is checked against the
            instrument at connect - the guide's only way to know is the
            magnitude of `CURRent:LIMit:NEGative? MIN` - and a mismatch is
            fatal, because a dissipator that was cabled after the supply was
            powered on is not recognised and does nothing, which is exactly the
            case a declared count would otherwise paper over.

        transport: substitute the link, for tests. Defaults to a real socket.
        """
        if dissipators not in SINK_FRACTION_BY_DISSIPATORS:
            raise HardwareError(
                f"dissipators must be one of {sorted(SINK_FRACTION_BY_DISSIPATORS)}, got {dissipators!r} - "
                f"this {RATED_POWER_W / 1000:.0f} kW model takes at most {MAX_DISSIPATORS} N7909A "
                "units, one per kW of sinking capability"
            )
        self._transport = transport if transport is not None else KeysightSocketTransport(host, port)
        self._dissipators = dissipators
        self._identity: Dict[str, str] = {}
        self._options: str = ""
        self._limits: Dict[str, Tuple[float, float]] = {}
        self._sink_ceiling_a: Optional[float] = None
        self._clamped: Dict[str, bool] = {quantity: False for quantity in CLAMPED_QUANTITIES}
        self._clamped_request: Dict[str, Optional[float]] = {quantity: None for quantity in CLAMPED_QUANTITIES}
        self._consecutive_frame_failures = 0
        self._teardown_requested = False
        """Set by disconnect(). Distinguishes a link this driver closed on
        purpose from one that closed under it - see _read_frame."""

    @property
    def is_connected(self) -> bool:
        """Connection state is the open socket itself, not a flag."""
        return self._transport.is_open

    # --- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        # Connecting twice is the normal path, not a caller error: runner.run()
        # connects when the driver process starts, and a client then calls
        # `connect` over the wire as every testbed and demo here does.
        if self.is_connected:
            logger.debug("already connected to %s, ignoring redundant connect", self._transport.address)
            return

        # Cleared only once the socket is actually open. Clearing it first would
        # leave the telemetry loop reading "the link is closed and this driver
        # did not close it" for the whole ~400 ms handshake, which is 20 frames -
        # enough to hit MAX_CONSECUTIVE_FRAME_FAILURES and take the process down
        # on a reconnect that was going to succeed.
        await self._transport.open()
        self._teardown_requested = False
        try:
            await self._confirm_data_format()
            await self._confirm_identity()
            # Clearing the status structure and this session's error queue is
            # the only state connect() changes. It touches no output, setpoint
            # or protection level, and it matters because an error left by a
            # previous client of this session would otherwise be attributed to
            # this driver's first write.
            await self._transport.command_then_query("*CLS", [ERROR_QUERY])
            # *CLS clears this connection's own queue, so the drain that follows
            # is not redundant: the guide describes a separate global queue for
            # power-on and hardware errors, which is read only once the
            # interface-specific one is empty and which *CLS is not documented to
            # clear. An N7909A that is cabled but not working reports itself
            # there, so anything found is worth logging before a run starts.
            await self._drain_errors("at connect")
            await self._read_limits()
            await self._verify_dissipators()
            await self._verify_declared_channels_exist()
            # Under the lock, as that method requires: on a reconnect the
            # telemetry loop is already running and starts locking for its own
            # frames the moment the socket is open, so an unlocked compound
            # message here would put two outstanding messages on one link.
            async with self._transport.transaction():
                first_frame = await self._read_frame_in_transaction()
            await self._log_adopted_state(first_frame)
        except Exception:
            # Connect failed partway. Leave no socket behind holding one of the
            # instrument's six connection slots against the next attempt.
            await self._transport.close()
            raise

    async def _confirm_data_format(self) -> None:
        """Confirm the reply format is ASCII before parsing anything.

        `FORMat REAL` makes block-data queries answer with binary this
        line-oriented transport cannot carry. Checked first because it is the
        cheapest way to rule out an unreadable reply - see EXPECTED_DATA_FORMAT
        for how narrow that class actually is."""
        reply = await self._transport.query("FORM?")
        if _as_str(reply).upper() != EXPECTED_DATA_FORMAT:
            raise HardwareError(
                f"{self._transport.address} is set to FORMat {reply!r}, not {EXPECTED_DATA_FORMAT} - "
                "this driver reads line-oriented ASCII replies and cannot parse binary blocks. "
                "Send 'FORM ASC' from another client, or power-cycle the instrument"
            )

    async def _confirm_identity(self) -> None:
        """Confirm this really is an N6974A before streaming anything from it.

        Reachability is not identity: this instrument's address is link-local
        and self-assigned, so it can move and leave something else answering on
        port 5025. The whole N6900/N7900 family shares this command set, so a
        sibling model would answer every query here and silently report against
        different ratings and a different dissipator mapping."""
        reply = await self._transport.query("*IDN?")
        fields = [field.strip() for field in reply.split(",")]
        if len(fields) < 4:
            raise HardwareError(f"unrecognised *IDN? reply from {self._transport.address}: {reply!r}")
        manufacturer, model, serial, firmware = fields[0], fields[1], fields[2], fields[3]
        if model.upper() != EXPECTED_MODEL:
            raise HardwareError(
                f"{self._transport.address} answered *IDN? as {model!r}, expected {EXPECTED_MODEL!r} "
                f"(full reply: {reply!r}) - this address is link-local and may have moved to "
                "another instrument; pass the correct --host"
            )
        self._identity = {
            "manufacturer": manufacturer,
            "model": model,
            "serial_number": serial,
            "firmware": firmware,
        }
        self._options = _as_str(await self._transport.query("*OPT?"))
        logger.info(
            "connected to %s %s serial %s firmware %s at %s (options: %s)",
            manufacturer, model, serial, firmware, self._transport.address, self._options or "none",
        )
        if self._options not in ("0", ""):
            # Not an error: extra options only add capability. Worth recording,
            # because the declared surface omits the subsystems they gate, so a
            # unit with options installed has features this driver does not
            # reach.
            logger.warning(
                "this unit reports installed options %r - the declared channel surface omits the "
                "subsystems options gate (lists, Arb, external datalogging, the digitizer's "
                "sample rate, the low current range, disconnect relays), so they are unavailable "
                "through this driver even though the instrument supports them", self._options,
            )

    async def _read_limits(self) -> None:
        """Read the instrument's own bound for every clamped parameter.

        The instrument is the authority on its own limits and reports them
        differently from the nameplate - 81.6 V and 25.5 A against 80 V and
        25 A - so they are asked for rather than written down. What is read here
        stays as reported: `_verify_dissipators` derives the expected sink floor
        from it, and `_clamp` narrows setpoints to the rating separately. This
        also picks up the negative-current floor, which is the one number that
        reflects how many N7909A dissipators the supply recognised at
        power-on."""
        for action, (channel, _) in _CLAMPED.items():
            query = _QUERIES[channel][0]
            base = query.rstrip("?")
            low, high = await self._transport.query_all([f"{base}? MIN", f"{base}? MAX"])
            self._limits[channel] = (_as_float(low), _as_float(high))
        logger.info(
            "instrument limits: %s",
            ", ".join(f"{channel} {low:g}..{high:g}" for channel, (low, high) in sorted(self._limits.items())),
        )

    async def _verify_dissipators(self) -> None:
        """Check the declared N7909A count against the instrument.

        There is no query for how many dissipators are attached. Per the guide,
        the only indication is the magnitude of `CURRent:LIMit:NEGative? MIN`,
        which is 10%, 50% or 100% of the maximum programmable current according
        to whether none, one or two are recognised. A mismatch is fatal rather
        than a warning: the count decides how hard this supply may discharge
        whatever is attached to it, and the most likely cause of a mismatch is a
        dissipator that is cabled but was not present when the supply was
        powered on - in which case it is recognised as absent and does nothing,
        while a test believes it has full sinking capability."""
        floor, _ = self._limits["current_limit_negative"]
        _, max_current = self._limits["current_limit"]
        detected = [
            count for count, fraction in SINK_FRACTION_BY_DISSIPATORS.items()
            if abs(abs(floor) - fraction * max_current) <= SINK_TOLERANCE_A
        ]
        expected_a = SINK_FRACTION_BY_DISSIPATORS[self._dissipators] * max_current
        if detected != [self._dissipators]:
            raise HardwareError(
                f"declared dissipators={self._dissipators} but the instrument reports a negative "
                f"current floor of {floor:g} A, which is "
                + (f"the {detected[0]}-dissipator value" if detected else "no recognised value")
                + f" ({expected_a:g} A was expected for {self._dissipators}). An N7909A is only "
                "recognised at power-on, so one cabled to a running supply reads as absent - "
                "power-cycle the supply with it connected, or correct the declared count"
            )
        self._sink_ceiling_a = expected_a
        logger.info(
            "verified %d N7909A dissipator(s): the instrument will sink to %g A (%.0f%% of %g A)",
            self._dissipators, floor, SINK_FRACTION_BY_DISSIPATORS[self._dissipators] * 100, max_current,
        )

    async def _verify_declared_channels_exist(self) -> None:
        """Issue every readable query once, individually, and name the ones that
        do not answer.

        Individually is the whole point. A command this unit does not implement
        is answered with silence and *discards the entire message it was part
        of*, so one absent channel inside the compound frame would cost a read
        timeout and a link resynchronisation on every frame for the whole run,
        while looking like nothing worse than a slow instrument. Probed one at a
        time it is a setup-time error naming the channel and the mnemonic.

        EVERY readable query, not only the ones a frame uses. The frame's 60 are
        the ones that would break a run outright, but the rest - comparator
        levels, digital pin functions and polarities, signal expressions, the
        status enable/PTR/NTR registers, trigger sources - are read back after
        the writes that change them. An absent one there would appear mid-run as
        a five-second stall and a reopened link inside `_write_checked`, reported
        as a failed write even though the write itself took effect. Probing all
        of them costs about a tenth of a second, once.

        The error queue is read after each probe, so a channel that fails can be
        reported with the instrument's own explanation - which distinguishes an
        uninstalled option (`+302`) from a command this model never had
        (`+310`) from a typo (`-113`).

        Read-only by construction: every probe is a query, never a write. The
        query-only *actions* are deliberately not probed: several are MEASure
        acquisitions costing 21 ms each, and `*TST?` runs a 5 s self-test, so
        sweeping them would add seconds to every connect to check commands that
        no telemetry frame or readback depends on.

        ONE ACQUISITION IS TAKEN FIRST, and the sweep does not work without it.
        A FETCh returns previously acquired data and never initiates its own
        acquisition, so on an instrument that has not measured since power-on
        `FETCh:CURRent?` and `FETCh:POWer?` answer with silence - which is
        exactly what an absent command looks like from here. Probing in channel
        order would then report both as unimplemented and refuse to connect,
        because `current` and `power` sort ahead of the `voltage` query that does
        the acquiring. Priming makes the sweep order irrelevant rather than
        load-bearing, and costs the same 21 ms every telemetry frame already
        spends."""
        primer = _QUERIES["voltage"][0]
        try:
            await self._transport.query(primer)
        except HardwareError as exc:
            raise HardwareError(
                f"{primer} did not answer on {self._transport.address}, so no acquisition could be "
                "taken to prime the FETCh queries. The measurement system or the link is unhealthy "
                "rather than a channel being absent"
            ) from exc

        missing: List[Tuple[str, str, str]] = []
        for channel in sorted(_QUERIES):
            query = _QUERIES[channel][0]
            try:
                await self._transport.query(query)
            except HardwareError:
                reason = "no reply and no error reported"
                try:
                    entry = await self._transport.query(ERROR_QUERY)
                    if not entry.startswith(NO_ERROR_PREFIXES):
                        reason = entry
                except HardwareError:
                    pass
                missing.append((channel, query, reason))

        if not missing:
            logger.info(
                "verified all %d readable channels answer on this instrument (%d of them in every "
                "frame, the rest read back after a write)", len(_QUERIES), len(_FRAME_CHANNELS),
            )
            return

        if len(missing) == len(_QUERIES):
            raise HardwareError(
                f"no declared channel answered on {self._transport.address} - the link is "
                "unresponsive rather than the channels being absent"
            )

        detail = "\n".join(f"  {channel} -> {query}: {reason}" for channel, query, reason in missing)
        raise MissingChannelError(
            f"{len(missing)} declared channel(s) are not implemented by this instrument "
            f"(firmware {self._identity.get('firmware')}, options {self._options or 'none'}):\n{detail}\n"
            "An unavailable command is answered with silence and discards the whole message it is "
            "part of, so leaving one declared would break every telemetry frame. Fix the mnemonic, "
            "or remove the channel from hardware/n6974a/n6974a_channels.py and its query here."
        )

    async def _log_adopted_state(self, frame: Dict[str, Any]) -> None:
        """Record the state this driver inherited.

        connect() is passive: it neither enables the output nor disables one it
        finds already on, and it sets no protection. That makes what it *found*
        worth recording, so a run afterwards can show what it began against."""
        if frame.get("output_enabled"):
            logger.warning(
                "adopting an energized supply: output ON in %s priority at %g V / %g A limit, "
                "measuring %g V / %g A (connect is passive, leaving as found)",
                frame.get("priority_mode"), frame.get("setpoint_voltage"), frame.get("current_limit"),
                frame.get("voltage"), frame.get("current"),
            )
        else:
            logger.info(
                "output is off at connect; %s priority, %g V / %g A limit, OVP %g V, OCP %s",
                frame.get("priority_mode"), frame.get("setpoint_voltage"), frame.get("current_limit"),
                frame.get("ovp_level"), "on" if frame.get("ocp_enabled") else "off",
            )
        if frame.get("protection_mode") == "LOWZ":
            logger.info(
                "protection_mode is LOWZ: a protection event will actively sink the load's energy "
                "for 2 ms while shutting down. Use set_protection_mode('HIGHZ') for a load that "
                "stores energy, such as a battery or a large capacitor"
            )
        if frame.get("sense_fault"):
            logger.warning(
                "sense-lead fault present at connect (questionable status bit 13): the remote sense "
                "leads are open. The output still regulates - the instrument has fallen back to "
                "local sensing - but the voltage at the output terminals will be about 1% above the "
                "programmed value, so a measurement or a Bound tighter than that will not mean what "
                "it appears to. Connect the sense leads to the load, or strap them at the output "
                "terminals, to clear it. To make an open sense lead abort a run instead, route it "
                "into a protection: set_signal_expression(n, \"OpenSense\") then "
                "set_user_protection_source(EXPR<n>) and set_user_protection_state(True). "
                "Leaving as found"
            )
        tripped = sorted(name for name, value in frame.items() if name.startswith("tripped_") and value)
        if tripped:
            logger.warning(
                "protection is latched at connect: %s - the output stays disabled until "
                "clear_protection is called with the cause removed", ", ".join(tripped),
            )

    async def disconnect(self) -> None:
        """Close the link, leaving the output exactly as it is.

        connect() adopts whatever state it finds rather than asserting one, so
        teardown has no basis for deciding that an energized output was this
        driver's to switch off - it may be holding a bias on a DUT, a soak, or a
        battery under test. Nothing this driver arms needs releasing either,
        because it arms nothing.

        Tolerates an already-unreachable instrument: this runs on the teardown
        path, where raising would mask the failure already propagating."""
        # Set before the lock is taken, so a telemetry frame already waiting on it
        # sees the intent rather than treating the closed socket as a fault.
        self._teardown_requested = True
        if not self._transport.is_open:
            return
        # Closing inside a transaction is what stops the socket disappearing
        # underneath a telemetry frame that is already reading. Without it, a
        # `disconnect` arriving over the command wire races the streaming loop,
        # whose read then fails - and runner.run() rightly treats a server task
        # dying on its own as fatal, so an orderly teardown would exit non-zero.
        async with self._transport.transaction():
            logger.info("disconnecting from %s, leaving the output as it is", self._transport.address)
            await self._transport.close()

    async def get_status(self) -> dict:
        self._require_connected()
        output_enabled, priority_mode = await self._transport.query_all(["OUTP?", "FUNC?"])
        return {
            "connected": True,
            "address": self._transport.address,
            **self._identity,
            "options": self._options or "none",
            "dissipators": self._dissipators,
            "sink_ceiling_a": self._sink_ceiling_a,
            "output_enabled": _as_bool(output_enabled),
            "priority_mode": _as_str(priority_mode),
            "limits": {channel: list(bounds) for channel, bounds in self._limits.items()},
            "link_resynchronisations": self._transport.resynchronisations,
        }

    async def list_actions(self) -> List[str]:
        return list(COMMAND_CHANNELS)

    # --- telemetry ---------------------------------------------------------

    async def stream_samples(self) -> AsyncIterator[dict]:
        while True:
            frame = await self._read_frame()
            if frame is not None:
                yield frame
            await asyncio.sleep(SAMPLE_INTERVAL_S)

    async def _read_frame(self) -> Optional[Dict[str, Any]]:
        """One frame; None when this driver is tearing down, or when a single
        read failed.

        The whole frame is one compound message inside one transaction, for two
        reasons. A command cannot interleave and leave a frame half from before
        it and half from after. And the teardown check happens *inside* the lock,
        so a `disconnect` cannot close the socket between the check and the read.

        A failed read is not immediately fatal. Where the transport raised it has
        already reopened the link, so the frame is lost but the next one will
        work; where a reply merely failed to parse the link was never in doubt.
        Either way one bad frame is worth a gap in the record rather than the end
        of a run. MAX_CONSECUTIVE_FRAME_FAILURES in a row is a different
        matter - then the instrument, the network or this driver's idea of the
        reply format is genuinely broken, and raising lets runner.run() bring the
        process down.

        A closed link is only silently accepted when teardown asked for it.
        Otherwise it counts as a failure like any other, because the transport
        closes the socket itself when it resynchronises and leaves it closed if
        reopening failed: an instrument switched off mid-run would otherwise
        leave this yielding nothing forever, with the driver alive, publishing
        no telemetry and never reporting a fault."""
        if self._teardown_requested:
            return None
        try:
            async with self._transport.transaction():
                if self._teardown_requested:
                    return None
                if not self.is_connected:
                    raise HardwareError(
                        f"the link to {self._transport.address} is closed and this driver did not "
                        "close it - a resynchronisation could not reopen it"
                    )
                frame = await self._read_frame_in_transaction()
        except HardwareError as exc:
            self._consecutive_frame_failures += 1
            if self._consecutive_frame_failures >= MAX_CONSECUTIVE_FRAME_FAILURES:
                raise HardwareError(
                    f"{self._consecutive_frame_failures} telemetry frames in a row failed on "
                    f"{self._transport.address}: {exc}"
                ) from exc
            logger.warning(
                "dropping telemetry frame %d of at most %d after a failed read: %s",
                self._consecutive_frame_failures, MAX_CONSECUTIVE_FRAME_FAILURES, exc,
            )
            return None
        self._consecutive_frame_failures = 0
        return frame

    async def _read_frame_in_transaction(self) -> Dict[str, Any]:
        """Read and decode one frame. Caller must hold the transaction lock."""
        replies = await self._transport.query_all_in_transaction(_FRAME_QUERIES)
        frame: Dict[str, Any] = {}
        for channel, reply in zip(_FRAME_CHANNELS, replies):
            frame[channel] = _QUERIES[channel][1](reply)
        for register, bits in STATUS_REGISTERS.items():
            frame.update(self._decode_register(bits, frame[f"{register}_status"], frame[f"{register}_events"]))
        for quantity in CLAMPED_QUANTITIES:
            frame[f"clamped_{quantity}"] = self._clamped[quantity]
            frame[f"clamped_{quantity}_request"] = self._clamped_request[quantity]
        frame["link_resynchronisations"] = self._transport.resynchronisations
        return frame

    @staticmethod
    def _decode_register(bits: Dict[int, str], condition: int, events: int) -> Dict[str, bool]:
        """Expand one register's condition and event readings into their per-bit
        channels.

        The condition bit says the state holds right now; the event bit says it
        began at some point since the previous frame, which is how a protection
        that trips and clears between two frames still leaves a trace. A
        condition that merely persists does not re-set its event bit, so an
        event channel is an edge and not a level."""
        decoded: Dict[str, bool] = {}
        for bit, stem in bits.items():
            decoded[stem] = bool(condition & (1 << bit))
            decoded[f"{stem}_event"] = bool(events & (1 << bit))
        return decoded

    # --- commands ----------------------------------------------------------

    async def execute(self, action: str, **params: Any) -> Any:
        self._require_connected()

        if action in _DRIVER_ACTIONS:
            return await self._execute_driver_action(action)

        timeout_s = SLOW_COMMAND_TIMEOUT_S if action in SLOW_ACTIONS else None

        if action in _QUERY_ACTIONS:
            query, parser = _QUERY_ACTIONS[action]
            return parser(await self._transport.query(query, timeout_s=timeout_s))

        if action in _BARE_COMMANDS:
            command, readback = _BARE_COMMANDS[action]
            return await self._write_checked(command, readback, timeout_s=timeout_s)

        if action in _VALUE_COMMANDS:
            template, cast, readback = _VALUE_COMMANDS[action]
            if "value" not in params:
                raise HardwareError(f"action {action!r} requires a 'value' parameter")
            try:
                value = cast(params["value"])
            except HardwareError:
                raise
            except (TypeError, ValueError) as exc:
                raise HardwareError(f"action {action!r} got an unusable value {params['value']!r}") from exc
            value = self._clamp(action, value)
            command = template.format(value=value)
            if action == "set_priority_mode":
                # Guard and write under one lock. Read separately, the output
                # could be enabled between the check and the switch - by another
                # command, the front panel, or the second socket client this
                # driver explicitly assumes may act - which is precisely the
                # hazard the guard exists to prevent.
                async with self._transport.transaction():
                    if _as_bool(await self._transport.query_in_transaction("OUTP?")):
                        raise HardwareError(
                            f"refusing to set the priority mode to {value} while the output is on: "
                            "the instrument would switch the output off and revert every output "
                            "setting to its reset value. Disable the output first"
                        )
                    applied = await self._write_checked_in_transaction(command, readback)
            else:
                applied = await self._write_checked(command, readback)
            self._warn_if_readback_disagrees(action, command, value, applied)
            if action == "recall_state":
                self._check_recalled_setpoints(applied)
            return applied

        raise HardwareError(f"unknown action: {action}")

    async def _execute_driver_action(self, action: str) -> Any:
        """The actions that are not a single instrument command: the two that
        touch driver-side state, the two that need more than one query, and the
        one write that cannot be verified."""
        if action == "clear_clamped_latch":
            previous = {
                quantity: {"clamped": self._clamped[quantity], "request": self._clamped_request[quantity]}
                for quantity in CLAMPED_QUANTITIES
            }
            self._clamped = {quantity: False for quantity in CLAMPED_QUANTITIES}
            self._clamped_request = {quantity: None for quantity in CLAMPED_QUANTITIES}
            return previous
        if action == "read_ratings":
            return {
                "limits": {channel: list(bounds) for channel, bounds in self._limits.items()},
                "dissipators": self._dissipators,
                "sink_ceiling_a": self._sink_ceiling_a,
            }
        if action == "drain_errors":
            return await self._drain_errors("on request")
        if action == "reboot":
            return await self._reboot()
        if action.startswith("read_threshold_level_"):
            comparator = int(action.rsplit("_", 1)[1])
            # Both queries under one transaction: the function decides which
            # level register to read, so a command interleaving between them
            # could pair a function with a level belonging to a different one.
            async with self._transport.transaction():
                function = _as_str(await self._transport.query_in_transaction(
                    _QUERIES[f"threshold_function_{comparator}"][0]))
                channel = _THRESHOLD_LEVEL_CHANNEL.get(function.upper())
                if channel is None:
                    raise HardwareError(
                        f"comparator {comparator} reports an unrecognised function {function!r}; "
                        f"expected one of {', '.join(sorted(_THRESHOLD_LEVEL_CHANNEL))}"
                    )
                query, parser = _QUERIES[f"{channel}_{comparator}"]
                level = parser(await self._transport.query_in_transaction(query))
            return {"function": function, "level": level}
        raise HardwareError(f"unknown action: {action}")

    async def _reboot(self) -> Dict[str, Any]:
        """Reboot the instrument and put this driver into teardown.

        Every other write travels with a `SYSTem:ERRor?` check, which needs a
        reply. This one cannot have it: the instrument drops the link as it
        restarts, so the check would stall for the full read timeout, be reported
        as a failed command although the reboot happened, and then leave the
        transport trying and failing to reopen against a unit that needs ~30 s to
        come back - three frames later the telemetry task raises and the process
        exits.

        So it is sent unverified and the link is closed deliberately, which puts
        this driver in the same state as an orderly disconnect: the telemetry
        stream stops rather than reporting a fault it cannot fix. The caller owns
        the rest, and `connect()` is what resumes streaming."""
        logger.warning(
            "rebooting %s: the link is being closed on purpose, telemetry stops here, and the "
            "instrument needs about 30 seconds before it will answer again. Call connect() to "
            "resume - nothing else in this driver will do it", self._transport.address,
        )
        await self._transport.write_no_reply("SYST:REB")
        self._teardown_requested = True
        await self._transport.close()
        return {
            "rebooting": True,
            "link_closed": True,
            "reconnect_required": True,
            "expect_ready_after_s": 30,
        }

    def _clamp(self, action: str, value: Any) -> Any:
        """Hold a commanded value inside what the instrument and the declared
        dissipator count allow, recording it if that changes the value.

        Clamping rather than refusing means a caller always gets as close as the
        hardware can go. What makes that safe to record rather than hide: the
        applied value is returned to the caller, the difference is logged, and a
        sticky channel keeps it visible in the recorded run for the rest of the
        test."""
        limited = _CLAMPED.get(action)
        if limited is None:
            return value
        channel, sink_capped = limited
        low, high = self._limits[channel]
        if channel not in UNRATED_CHANNELS:
            # The instrument reports a range wider than the model's rating, so
            # the rating binds as well.
            ceiling = (
                CEILING_VOLTAGE_V if _CLAMP_QUANTITY[action] == "voltage" else CEILING_CURRENT_A
            )
            low, high = max(low, -ceiling), min(high, ceiling)
        if sink_capped and self._sink_ceiling_a is not None:
            # The instrument's own floor already reflects the dissipators it
            # recognised, and connect() has confirmed the two agree. Taking the
            # tighter of them anyway keeps the declared count load-bearing
            # rather than merely checked.
            low = max(low, -self._sink_ceiling_a)
        requested = float(value)
        applied = min(max(requested, low), high)
        if applied == requested:
            return value
        # A signed current action clamped at a negative floor was heading into
        # the sink direction, whichever quantity the action nominally sets, so
        # that is where it is recorded.
        quantity = _CLAMP_QUANTITY[action]
        if quantity != "voltage" and applied == low and low < 0:
            quantity = "sink_current"
        self._clamped[quantity] = True
        self._clamped_request[quantity] = requested
        unit = "V" if quantity == "voltage" else "A"
        logger.warning(
            "clamped %s: asked %g %s, applying %g %s (the permitted %s range is %g..%g %s%s)",
            action, requested, unit, applied, unit, channel, low, high, unit,
            f", with the sink floor held to {self._dissipators} dissipator(s)" if sink_capped else "",
        )
        return applied

    async def _write_checked(
        self, command: str, readback: Sequence[str], timeout_s: Optional[float] = None
    ) -> Any:
        """Send a command, confirm the instrument accepted it, then read back
        whatever it changed - two messages, one transaction.

        Returns the readback: a single value when the command changes one
        channel, a dict when it changes several, and None when it changes
        nothing readable. That is what makes a clamped setpoint honest - the
        caller is told what the instrument actually holds, not what was asked
        for, which is also why the readback cannot share the write's message
        (see the module docstring: it would answer one step stale).

        The error query travels with the command, and last within that message,
        because its answer may contain a semicolon (see
        transport.VALUE_SEPARATOR). A non-zero error raises before anything is
        read back, and the queue is drained so nothing is left to be blamed on
        the next command.

        One transaction start to finish, so the error read here cannot have been
        set by another caller's command, no telemetry frame can interleave
        between the write and its readback, and the value read back cannot have
        been changed by another command in between."""
        async with self._transport.transaction():
            return await self._write_checked_in_transaction(command, readback, timeout_s)

    async def _write_checked_in_transaction(
        self, command: str, readback: Sequence[str], timeout_s: Optional[float] = None
    ) -> Any:
        """As _write_checked(), without taking the lock. For a caller that needs
        the write to be indivisible from something it did first - the
        priority-mode guard reads the output state and must know it still holds
        when the switch lands."""
        entry = (await self._transport.command_then_query_in_transaction(
            command, [ERROR_QUERY], timeout_s=timeout_s
        ))[0]
        if not entry.startswith(NO_ERROR_PREFIXES):
            drained = await self._drain_errors_in_transaction(f"after {command!r}")
            raise HardwareError(
                f"{command!r} was refused: {entry}"
                + (f" (also queued: {'; '.join(drained)})" if drained else "")
            )
        if not readback:
            return None
        queries = [_QUERIES[channel][0] for channel in readback]
        replies = await self._transport.query_all_in_transaction(queries)
        values = {
            channel: _QUERIES[channel][1](reply) for channel, reply in zip(readback, replies)
        }
        if len(values) == 1:
            return next(iter(values.values()))
        return values

    @staticmethod
    def _warn_if_readback_disagrees(action: str, command: str, commanded: Any, applied: Any) -> None:
        """Log when the instrument holds something other than what was asked
        for, after clamping has already been accounted for.

        The case this exists for: a parameter belonging to the other priority
        mode is accepted with no error and silently does not take effect. The
        tolerance is relative, because several parameters legitimately quantise
        the value they are given - NPLCycles asked for 1 reports 0.999936 - and
        that rounding is not worth a warning while a value that did not take at
        all always is."""
        if isinstance(applied, bool) or isinstance(commanded, bool):
            # A boolean readback is exact, and bool is a subclass of int, so it
            # would otherwise be compared numerically for no reason.
            return
        if not isinstance(applied, (int, float)) or not isinstance(commanded, (int, float)):
            return
        tolerance = max(abs(float(commanded)) * 1e-3, 1e-4)
        if abs(applied - float(commanded)) <= tolerance:
            return
        logger.warning(
            "%s sent %r but the instrument reports %g - the command was accepted without an error "
            "and did not take effect. A parameter belonging to the other priority mode does this: "
            "check priority_mode", action, command, applied,
        )

    async def _drain_errors(self, reason: str) -> List[str]:
        """Read the error queue empty, taking the link lock. See
        _drain_errors_in_transaction for the rest."""
        async with self._transport.transaction():
            return await self._drain_errors_in_transaction(reason)

    async def _drain_errors_in_transaction(self, reason: str) -> List[str]:
        """Read the error queue empty and return whatever was in it.

        The queue holds up to 20 entries and belongs to this connection, not to
        the instrument - measured: a second socket client does not see these
        entries, and reopening the link discards them. Draining
        matters because an entry left behind would be read by the next write's
        check and reported against a command that succeeded."""
        drained: List[str] = []
        for _ in range(21):
            entry = await self._transport.query_in_transaction(ERROR_QUERY)
            if entry.startswith(NO_ERROR_PREFIXES):
                break
            drained.append(entry)
        if drained:
            logger.warning("drained %d error(s) from the queue (%s): %s", len(drained), reason, "; ".join(drained))
        return drained

    def _check_recalled_setpoints(self, applied: Any) -> None:
        """A recalled state can carry any setpoint, and there is no way to know
        what a store holds before recalling it. So the limits are checked after
        the fact: the values are already applied by the time this raises, and
        the message says so, because a test aborting into teardown is a better
        outcome than a supply left holding a setpoint beyond what the declared
        dissipator count can absorb."""
        if not isinstance(applied, dict):
            return
        floor = -self._sink_ceiling_a if self._sink_ceiling_a is not None else None
        offending = []
        for channel in ("setpoint_voltage", "current_limit", "current_limit_negative"):
            value = applied.get(channel)
            if value is None or channel not in self._limits:
                continue
            low, high = self._limits[channel]
            if floor is not None and channel == "current_limit_negative":
                low = max(low, floor)
            if not low <= value <= high:
                offending.append(f"{channel}={value:g} (allowed {low:g}..{high:g})")
        if offending:
            raise HardwareError(
                "the recalled state is outside this instrument's allowed range: "
                + ", ".join(offending)
                + " - THESE VALUES ARE NOW APPLIED, and the output may be energized; the recall "
                "cannot be clamped because a store's contents are unknown until it is loaded"
            )

