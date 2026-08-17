"""Real backend for the TTi CPX400DP dual-output bench power supply, over
ethernet. See cpx400dp_channels.py for the declared channel surface and
transport.py for the line protocol.

This is the second real (non-simulated) device in this codebase and the first
reached over a network rather than a local bus. There is deliberately no mock
backend: unlike the ODrive, whose risk was wrong attribute paths, this driver's
risk is response parsing, and a HardwareBackend-level mock would replace
exactly the code most likely to be wrong. Tests substitute a fake *transport*
instead, so the real parsing runs against recorded instrument replies
(tests/test_cpx400dp.py).

CONNECT AND DISCONNECT ARE PASSIVE. connect() opens the link, confirms the
instrument's identity, clears its own error registers, verifies every declared
channel answers, and reads the cached tier. It does not enable an output, does
not disable one it finds already on, and does not touch protection levels. It
logs the output state it adopted, so a run records what it inherited.
disconnect() closes the link and releases an interface lock if this driver took
one - it does NOT switch outputs off. The framework never assumes a supply's
output should be on because it connected, and equally does not assume it may
de-energize something a person deliberately energized. Nothing here survives
SIGKILL; energized-state safety belongs to the instrument's own OVP/OCP, which
this driver exposes but never sets on its own.

EVERY WRITE IS CHECKED, because the instrument accepts writes it then discards.
`V2 999` (above the 60 V maximum) leaves the setpoint untouched, answers nothing
at all, and reports itself only as `EER?` = 100. Both registers are read after
every command, because they catch different failures - a range error sets `EER?`
and `*ESR?` bit 4, while a syntax error sets only `*ESR?` bit 5. They are read
after the command inside one transaction, so the answer cannot be another
caller's.

The registers survive the socket: they belong to the interface instance rather
than the connection, so a driver starting fresh can read an error left by a
process that died earlier. connect() issues `*CLS` for that reason.

BEHAVIOUR OF THIS INSTRUMENT worth knowing before writing a test against it:
  - ~2.4 ms per query round-trip, and the instrument intermittently stalls for a
    few hundred milliseconds - see SAMPLE_INTERVAL_S.
  - `RANGE<n>?`, `SENSE<n>?`, `DAMPING<n>?` and `EXR?` are not implemented on
    firmware 2.03-4.12; they belong to other TTi models. An unimplemented
    mnemonic is answered with silence, so it costs a full read timeout, which is
    what _verify_declared_channels_exist() catches once at connect rather than on
    every frame of a run.
  - An output ramps rather than stepping: a quarter-second after enabling at a
    5 V setpoint the readback is still short of it. A step asserting voltage
    immediately after enabling an output reads low.
  - Switching an output off does not mean zero volts - the terminals decay
    through the output capacitance.
  - An OVP or OCP trip switches the output off by itself, within one frame
    period, which is why `output_enabled_<n>` is polled rather than cached with
    the other settings. `TRIPRST` does not clear either trip; see
    cpx400dp_channels.py for what does, and note the two trips differ.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

import asyncio
import time

from ..backend import HardwareBackend, HardwareError, MissingChannelError
from protocol.wire import DEVICE_CPX400DP

from .cpx400dp_channels import (
    CACHED_CHANNELS,
    COMMAND_CHANNELS,
    METER_CHANNELS,
    METER_RATE_HZ,
    OUTPUTS,
    STATE_CHANNELS,
    TELEMETRY_CHANNELS,
)
from .transport import CONNECT_DRAIN_TIMEOUT_S, DEFAULT_PORT, TtiSocketTransport

logger = logging.getLogger(__name__)

DEFAULT_CPX400DP_HOST = "169.254.229.133"
"""The instrument on this stand. NOT a stable address: `NETCONFIG?` reports
DHCP, but the segment it is on has no DHCP server, so it self-assigned a
link-local address. It moves if a DHCP server appears or on an address
collision. A testbed is expected to pass an explicit host from its own config
rather than rely on this; it exists so `python -m hardware.cpx400dp.main` works
standalone. connect() verifies the model in `*IDN?` precisely because a moving
address could otherwise point this driver at a different instrument.

An mDNS name works too and is a stabler identity: the instrument advertises
itself as `t<serial number>.local`, which follows it when its address changes. It
needs an mDNS responder on the host - macOS has one built in, a Windows or
CentOS stand may not."""

SAMPLE_INTERVAL_S = 0.02
"""Sleep *between* frames, not the frame period - and on this instrument the
difference is most of the number.

Most frames cost 4 query round-trips (the state tier); roughly every fifth also
pays 4 more for the meter tier. A state-only frame is ~9 ms, a frame including
the meters ~19 ms, and the instrument intermittently stalls for a few hundred
milliseconds - so the frame period is not steady, and a consumer should read
frame `t` rather than assume a fixed one.

The right value depends on what a test needs from the sample rate, which is a
test-engineering decision rather than a driver one. Turning it does not trade
against measurement freshness: the meter tier runs at the instrument's own rate
regardless. Whatever it is set to, the publisher's high-water mark is sized from
it (see protocol/wire.py's hwm_for_interval)."""

METER_INTERVAL_S = 1.0 / METER_RATE_HZ * 0.8
"""How often the meter tier is re-read: 200 ms, i.e. 5 Hz against the
instrument's specified 4 Hz reading rate.

Deliberately faster than the instrument reports, not equal to it. Polling at
exactly the refresh rate would beat against the instrument's own unsynchronised
update and could sit just behind it indefinitely, ageing every reading by
almost a full period. A 25% margin keeps the worst-case staleness near one
meter period without spending meaningfully more of the link."""

EXPECTED_MODEL = "CPX400DP"
"""Checked against the model field of `*IDN?` at connect."""

# --- Response parsing ------------------------------------------------------
# The instrument uses three different reply shapes, and the mnemonic it echoes
# is not always the one that was sent:
#   V1?    -> 'V1 20.19'    mnemonic, space, value
#   V1O?   -> '-0.005V'     value with a unit suffix, no mnemonic
#   OP1?   -> '0'           bare integer
#   OVP1?  -> 'VP1 66.00'   echoes VP1, NOT OVP1
#   OCP1?  -> 'CP1 22.00'   echoes CP1, NOT OCP1
# A parser that simply stripped the query's own mnemonic would break on exactly
# the two protection channels, which is why each channel carries its own.


def _prefixed(mnemonic: str) -> Callable[[str], float]:
    """Parse `'<mnemonic> <value>'`, e.g. `'V1 20.19'` -> 20.19."""

    def parse(reply: str) -> float:
        head, _, value = reply.partition(" ")
        if head.upper() != mnemonic.upper() or not value:
            raise HardwareError(f"expected a {mnemonic!r} reply, got {reply!r}")
        return float(value)

    return parse


def _suffixed(unit: str) -> Callable[[str], float]:
    """Parse `'<value><unit>'`, e.g. `'-0.005V'` -> -0.005."""

    def parse(reply: str) -> float:
        if not reply.upper().endswith(unit.upper()):
            raise HardwareError(f"expected a value ending in {unit!r}, got {reply!r}")
        return float(reply[: -len(unit)])

    return parse


def _as_int(reply: str) -> int:
    try:
        return int(reply)
    except ValueError as exc:
        raise HardwareError(f"expected an integer, got {reply!r}") from exc


def _as_float(reply: str) -> float:
    try:
        return float(reply)
    except ValueError as exc:
        raise HardwareError(f"expected a number, got {reply!r}") from exc


def _as_str(reply: str) -> str:
    return reply


# --- Query tables ----------------------------------------------------------
# channel -> (query mnemonic, parser). Split by tier: the streaming table is
# re-read every frame, the cached table only at connect and after a write that
# changes it. See cpx400dp_channels.py for why the split exists.

_STATE_QUERIES: Dict[str, Tuple[str, Callable[[str], Any]]] = {}
_METER_QUERIES: Dict[str, Tuple[str, Callable[[str], Any]]] = {}
for _n in OUTPUTS:
    _STATE_QUERIES[f"output_enabled_{_n}"] = (f"OP{_n}?", _as_int)
    _STATE_QUERIES[f"limit_status_{_n}"] = (f"LSR{_n}?", _as_int)
    _METER_QUERIES[f"voltage_{_n}"] = (f"V{_n}O?", _suffixed("V"))
    _METER_QUERIES[f"current_{_n}"] = (f"I{_n}O?", _suffixed("A"))

_CACHED_QUERIES: Dict[str, Tuple[str, Callable[[str], Any]]] = {}
for _n in OUTPUTS:
    _CACHED_QUERIES[f"setpoint_voltage_{_n}"] = (f"V{_n}?", _prefixed(f"V{_n}"))
    _CACHED_QUERIES[f"setpoint_current_{_n}"] = (f"I{_n}?", _prefixed(f"I{_n}"))
    _CACHED_QUERIES[f"ovp_{_n}"] = (f"OVP{_n}?", _prefixed(f"VP{_n}"))  # replies VP<n>, not OVP<n>
    _CACHED_QUERIES[f"ocp_{_n}"] = (f"OCP{_n}?", _prefixed(f"CP{_n}"))  # replies CP<n>, not OCP<n>
    _CACHED_QUERIES[f"delta_voltage_{_n}"] = (f"DELTAV{_n}?", _prefixed(f"DELTAV{_n}"))
    _CACHED_QUERIES[f"delta_current_{_n}"] = (f"DELTAI{_n}?", _prefixed(f"DELTAI{_n}"))
    _CACHED_QUERIES[f"limit_status_enable_{_n}"] = (f"LSE{_n}?", _as_int)
_CACHED_QUERIES["config_mode"] = ("CONFIG?", _as_int)
_CACHED_QUERIES["tracking_ratio"] = ("RATIO?", _as_float)
_CACHED_QUERIES["ip_address"] = ("IPADDR?", _as_str)
_CACHED_QUERIES["netmask"] = ("NETMASK?", _as_str)
_CACHED_QUERIES["net_config"] = ("NETCONFIG?", _as_str)

# --- Limit status register decoding ---------------------------------------
# LSR<n>? bit -> channel name stem. Bits 5 and 7 are documented as reserved.
# Bits 0/1/4 are modes; bits 2/3/6 are trips. See cpx400dp_channels.py for why
# the modes are named as states despite the manual calling them events.
_LIMIT_STATUS_BITS: Dict[int, str] = {
    0: "in_cv",
    1: "in_cc",
    2: "tripped_ov",
    3: "tripped_oc",
    4: "in_power_limit",
    6: "tripped_latching",
}

# --- Execution Error Register codes ---------------------------------------
# From the manual's Error Messages section. Reported by EER?.
_EER_MEANINGS: Dict[int, str] = {
    100: "range error - the value is not allowed for this parameter",
    101: "the recalled setup store contains corrupted data",
    102: "the recalled setup store is empty",
    103: "the second output is not available (single-channel instrument, or parallel mode)",
    104: "command not valid while the output is on (e.g. changing CONFIG with output 2 on)",
    200: "read only - this interface does not hold write privileges (see interface_lock)",
}

# --- Standard Event Status Register bits -----------------------------------
_ESR_POWER_ON = 1 << 7
_ESR_COMMAND_ERROR = 1 << 5
_ESR_EXECUTION_ERROR = 1 << 4
_ESR_VERIFY_TIMEOUT = 1 << 3
_ESR_QUERY_ERROR = 1 << 2

# --- Command tables --------------------------------------------------------
# action -> (command template, value cast, cached channels to re-read after).
# The template's `{value}` is filled from the action's `value` parameter.
_VALUE_COMMANDS: Dict[str, Tuple[str, Callable[[Any], Any], Tuple[str, ...]]] = {}
# action -> (command, cached channels to re-read after). No parameters.
_BARE_COMMANDS: Dict[str, Tuple[str, Tuple[str, ...]]] = {}
# action -> (query, parser). Returns a value to the caller; changes nothing.
_QUERY_COMMANDS: Dict[str, Tuple[str, Callable[[str], Any]]] = {}

for _n in OUTPUTS:
    _all_cached_for_output = tuple(f"{stem}_{_n}" for stem in (
        "setpoint_voltage", "setpoint_current", "ovp", "ocp",
        "delta_voltage", "delta_current", "limit_status_enable",
    ))
    _VALUE_COMMANDS[f"set_voltage_{_n}"] = (f"V{_n} {{value}}", float, (f"setpoint_voltage_{_n}",))
    _VALUE_COMMANDS[f"set_voltage_verify_{_n}"] = (f"V{_n}V {{value}}", float, (f"setpoint_voltage_{_n}",))
    _VALUE_COMMANDS[f"set_current_{_n}"] = (f"I{_n} {{value}}", float, (f"setpoint_current_{_n}",))
    _VALUE_COMMANDS[f"set_ovp_{_n}"] = (f"OVP{_n} {{value}}", float, (f"ovp_{_n}",))
    _VALUE_COMMANDS[f"set_ocp_{_n}"] = (f"OCP{_n} {{value}}", float, (f"ocp_{_n}",))
    _VALUE_COMMANDS[f"set_delta_voltage_{_n}"] = (f"DELTAV{_n} {{value}}", float, (f"delta_voltage_{_n}",))
    _VALUE_COMMANDS[f"set_delta_current_{_n}"] = (f"DELTAI{_n} {{value}}", float, (f"delta_current_{_n}",))
    _VALUE_COMMANDS[f"enable_output_{_n}"] = (f"OP{_n} {{value}}", int, ())
    _VALUE_COMMANDS[f"save_setup_{_n}"] = (f"SAV{_n} {{value}}", int, ())
    _VALUE_COMMANDS[f"recall_setup_{_n}"] = (f"RCL{_n} {{value}}", int, _all_cached_for_output)
    _VALUE_COMMANDS[f"set_limit_status_enable_{_n}"] = (f"LSE{_n} {{value}}", int, (f"limit_status_enable_{_n}",))

    _BARE_COMMANDS[f"increment_voltage_{_n}"] = (f"INCV{_n}", (f"setpoint_voltage_{_n}",))
    _BARE_COMMANDS[f"increment_voltage_verify_{_n}"] = (f"INCV{_n}V", (f"setpoint_voltage_{_n}",))
    _BARE_COMMANDS[f"decrement_voltage_{_n}"] = (f"DECV{_n}", (f"setpoint_voltage_{_n}",))
    _BARE_COMMANDS[f"decrement_voltage_verify_{_n}"] = (f"DECV{_n}V", (f"setpoint_voltage_{_n}",))
    _BARE_COMMANDS[f"increment_current_{_n}"] = (f"INCI{_n}", (f"setpoint_current_{_n}",))
    _BARE_COMMANDS[f"decrement_current_{_n}"] = (f"DECI{_n}", (f"setpoint_current_{_n}",))

    _QUERY_COMMANDS[f"read_limit_status_{_n}"] = (f"LSR{_n}?", _as_int)

_ALL_CACHED = tuple(CACHED_CHANNELS)
_VALUE_COMMANDS["enable_all_outputs"] = ("OPALL {value}", int, ())
_VALUE_COMMANDS["set_config_mode"] = ("CONFIG {value}", int, ("config_mode",))
_VALUE_COMMANDS["set_tracking_ratio"] = ("RATIO {value}", float, ("tracking_ratio",))
_VALUE_COMMANDS["set_event_status_enable"] = ("*ESE {value}", int, ())
_VALUE_COMMANDS["set_service_request_enable"] = ("*SRE {value}", int, ())
_VALUE_COMMANDS["set_parallel_poll_enable"] = ("*PRE {value}", int, ())

_BARE_COMMANDS["trip_reset"] = ("TRIPRST", ())
_BARE_COMMANDS["go_local"] = ("LOCAL", ())
_BARE_COMMANDS["clear_status"] = ("*CLS", ())
_BARE_COMMANDS["reset"] = ("*RST", _ALL_CACHED)
_BARE_COMMANDS["operation_complete"] = ("*OPC", ())
_BARE_COMMANDS["wait_operation_complete"] = ("*WAI", ())
_BARE_COMMANDS["trigger"] = ("*TRG", ())

_QUERY_COMMANDS["interface_lock"] = ("IFLOCK", _as_int)
_QUERY_COMMANDS["interface_unlock"] = ("IFUNLOCK", _as_int)
_QUERY_COMMANDS["read_interface_lock"] = ("IFLOCK?", _as_int)
_QUERY_COMMANDS["read_execution_error"] = ("EER?", _as_int)
_QUERY_COMMANDS["read_query_error"] = ("QER?", _as_int)
_QUERY_COMMANDS["read_event_status"] = ("*ESR?", _as_int)
_QUERY_COMMANDS["read_event_status_enable"] = ("*ESE?", _as_int)
_QUERY_COMMANDS["read_service_request_enable"] = ("*SRE?", _as_int)
_QUERY_COMMANDS["read_status_byte"] = ("*STB?", _as_int)
_QUERY_COMMANDS["read_parallel_poll_enable"] = ("*PRE?", _as_int)
_QUERY_COMMANDS["read_individual_status"] = ("*IST?", _as_int)
_QUERY_COMMANDS["read_operation_complete"] = ("*OPC?", _as_int)
_QUERY_COMMANDS["self_test"] = ("*TST?", _as_int)
_QUERY_COMMANDS["read_identity"] = ("*IDN?", _as_str)
_QUERY_COMMANDS["read_bus_address"] = ("ADDRESS?", _as_int)

# Handled in execute() rather than by a table: not instrument commands at all.
_DRIVER_COMMANDS = {f"clear_limit_status_latch_{n}" for n in OUTPUTS}

# Actions whose commanded value is checked against the optional driver-side
# ceiling before anything reaches the wire. Maps action -> which ceiling.
_VOLTAGE_LIMITED = {f"set_voltage_{n}" for n in OUTPUTS} | {f"set_voltage_verify_{n}" for n in OUTPUTS}
_CURRENT_LIMITED = {f"set_current_{n}" for n in OUTPUTS}
# Steps have no commanded value, so their resulting setpoint is predicted from
# the cached setpoint and step size. Maps action -> (cached setpoint, cached
# step, sign, which ceiling).
_STEP_LIMITED: Dict[str, Tuple[str, str, int, str]] = {}
for _n in OUTPUTS:
    for _action in (f"increment_voltage_{_n}", f"increment_voltage_verify_{_n}"):
        _STEP_LIMITED[_action] = (f"setpoint_voltage_{_n}", f"delta_voltage_{_n}", +1, "voltage")
    _STEP_LIMITED[f"increment_current_{_n}"] = (f"setpoint_current_{_n}", f"delta_current_{_n}", +1, "current")


def _validate_channel_coverage() -> None:
    """Runs once at import. The declared surface in cpx400dp_channels.py and
    the tables above are two separate structures that must agree exactly: every
    declared channel needs an implementation and vice versa. Nothing else would
    catch a channel added to one and forgotten in the other - it would simply
    produce an incomplete telemetry frame, or an action that verify_actions()
    reports present while execute() rejects it. This is the static equivalent
    of what verify_channels()/verify_actions() do against a live process."""
    decoded = {f"{stem}_{n}" for stem in _LIMIT_STATUS_BITS.values() for n in OUTPUTS}
    decoded |= {f"limit_status_latched_{n}" for n in OUTPUTS}
    implemented_state = set(_STATE_QUERIES) | decoded
    if set(STATE_CHANNELS) != implemented_state:
        raise AssertionError(
            "_STATE_QUERIES is out of sync with STATE_CHANNELS - "
            f"missing: {sorted(set(STATE_CHANNELS) - implemented_state)}, "
            f"extra: {sorted(implemented_state - set(STATE_CHANNELS))}"
        )

    if set(METER_CHANNELS) != set(_METER_QUERIES):
        raise AssertionError(
            "_METER_QUERIES is out of sync with METER_CHANNELS - "
            f"missing: {sorted(set(METER_CHANNELS) - set(_METER_QUERIES))}, "
            f"extra: {sorted(set(_METER_QUERIES) - set(METER_CHANNELS))}"
        )

    if set(CACHED_CHANNELS) != set(_CACHED_QUERIES):
        raise AssertionError(
            "_CACHED_QUERIES is out of sync with CACHED_CHANNELS - "
            f"missing: {sorted(set(CACHED_CHANNELS) - set(_CACHED_QUERIES))}, "
            f"extra: {sorted(set(_CACHED_QUERIES) - set(CACHED_CHANNELS))}"
        )

    implemented_commands = set(_VALUE_COMMANDS) | set(_BARE_COMMANDS) | set(_QUERY_COMMANDS) | _DRIVER_COMMANDS
    if set(COMMAND_CHANNELS) != implemented_commands:
        raise AssertionError(
            "the command tables are out of sync with COMMAND_CHANNELS - "
            f"missing: {sorted(set(COMMAND_CHANNELS) - implemented_commands)}, "
            f"extra: {sorted(implemented_commands - set(COMMAND_CHANNELS))}"
        )

    overlapping = (set(_VALUE_COMMANDS) & set(_BARE_COMMANDS)) | (set(_VALUE_COMMANDS) & set(_QUERY_COMMANDS))
    overlapping |= set(_BARE_COMMANDS) & set(_QUERY_COMMANDS)
    if overlapping:
        raise AssertionError(f"actions appear in more than one command table: {sorted(overlapping)}")

    refreshed = {c for _, _, cs in _VALUE_COMMANDS.values() for c in cs}
    refreshed |= {c for _, cs in _BARE_COMMANDS.values() for c in cs}
    unknown = refreshed - set(CACHED_CHANNELS)
    if unknown:
        raise AssertionError(f"commands claim to refresh channels that are not cached: {sorted(unknown)}")


_validate_channel_coverage()


class Cpx400dpBackend(HardwareBackend):
    """Real TTi CPX400DP dual-output bench supply, over ethernet on port 9221."""

    device = DEVICE_CPX400DP
    sample_interval_s = SAMPLE_INTERVAL_S

    def __init__(
        self,
        host: str = DEFAULT_CPX400DP_HOST,
        port: int = DEFAULT_PORT,
        max_voltage: Optional[float] = None,
        max_current: Optional[float] = None,
        take_interface_lock: bool = False,
        transport: Optional[TtiSocketTransport] = None,
    ) -> None:
        """
        max_voltage/max_current: an optional driver-side ceiling on commanded
            setpoints. `None` (the default) means the instrument's own limits
            are the only ones, keeping default behaviour fully passive. When
            set, a setpoint above the ceiling raises HardwareError before
            anything reaches the wire. This catches the failure the instrument
            cannot: a value that is perfectly in range for the supply and
            catastrophic for the load - a typo commanding 48 V into a 12 V DUT
            is accepted happily, and every error register reads clean. A
            testbed constructing this backend knows what its DUT can take.

        take_interface_lock: request exclusive control (IFLOCK) at connect and
            release it at disconnect. Off by default, keeping connect passive.
            Turning it on stops the web interface or a VXI-11 client changing
            setpoints mid-run. The lock binds to the interface instance rather
            than the connection, so a driver that dies without releasing it
            leaves it held - the next connection inherits ownership and can
            release it, but until then other interfaces are refused writes with
            EER 200.

        transport: substitute the link, for tests. Defaults to a real socket.
        """
        self._transport = transport if transport is not None else TtiSocketTransport(host, port)
        self._max_voltage = max_voltage
        self._max_current = max_current
        self._take_interface_lock = take_interface_lock
        self._holds_interface_lock = False
        self._identity: Dict[str, str] = {}
        self._cached: Dict[str, Any] = {}
        self._meters: Dict[str, Any] = {}
        self._meters_read_at: float = 0.0
        self._latched_limit_status: Dict[int, int] = {n: 0 for n in OUTPUTS}

    @property
    def is_connected(self) -> bool:
        """Connection state is the open socket itself, not a flag."""
        return self._transport.is_open

    # --- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        # Connecting twice is the normal path, not a caller error: runner.run()
        # connects when the driver process starts, and a client then calls
        # `connect` over the wire as every testbed and demo here does. On this
        # instrument a second connect is not merely redundant but fatal - it
        # holds a single raw socket, so opening another is refused and the
        # already-working link would be reported as a failure.
        if self.is_connected:
            logger.debug("already connected to %s, ignoring redundant connect", self._transport.address)
            return

        await self._transport.open()
        try:
            # Throw away anything a previous client left behind before trusting
            # a reply. A stale reply outlives the connection that abandoned it,
            # so without this a driver killed mid-transaction leaves the next
            # startup reading every answer one behind.
            await self._transport.drain("at connect", timeout_s=CONNECT_DRAIN_TIMEOUT_S)
            await self._confirm_identity()
            # The error registers outlive the socket, so a previous process's
            # failure would otherwise be attributed to this driver's first
            # write. Clearing is the only state connect() changes, and it
            # touches no output, setpoint or protection level.
            await self._transport.write("*CLS")
            if self._take_interface_lock:
                await self._acquire_interface_lock()
            await self._verify_declared_channels_exist()
            await self._refresh_cached(CACHED_CHANNELS)
            # Prime the meter tier, so the very first frame carries real
            # readings rather than blanks for its first METER_INTERVAL_S.
            async with self._transport.transaction():
                await self._read_meters_in_transaction()
            await self._log_adopted_state()
        except Exception:
            # Connect failed partway. Leave no socket behind holding the
            # instrument's single connection slot against the next attempt.
            await self._transport.close()
            raise

    async def _confirm_identity(self) -> None:
        """Confirm this really is a CPX400DP before streaming anything from it.

        Reachability is not identity: this instrument's address is link-local
        and self-assigned, so it can move and leave something else answering on
        port 9221. Without this check the driver would publish another device's
        replies under `cpx400dp`, or hang on mnemonics it does not implement."""
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
        logger.info(
            "connected to %s %s serial %s firmware %s at %s",
            manufacturer, model, serial, firmware, self._transport.address,
        )

    async def _verify_declared_channels_exist(self) -> None:
        """Issue every declared telemetry query once, and name the ones that do
        not answer.

        This matters more here than on a device that raises for a missing
        attribute. An unimplemented mnemonic is answered with *silence*, so a
        declared-but-unsupported channel would stall the telemetry stream by
        the full read timeout on every frame, for the entire run, while looking
        like nothing worse than a slow device. Probed once, at connect, so it
        is a setup-time error - the same reason OdriveBackend probes its
        declared attribute paths.

        Read-only by construction: every probe is a query, never a write."""
        missing: List[Tuple[str, str]] = []
        first_error: Optional[HardwareError] = None
        queries = {**_STATE_QUERIES, **_METER_QUERIES, **_CACHED_QUERIES}
        for channel, (query, _parser) in sorted(queries.items()):
            try:
                await self._transport.query(query)
            except HardwareError as exc:
                missing.append((channel, query))
                first_error = first_error or exc

        if not missing:
            logger.info("verified all %d declared telemetry channels answer on this instrument", len(queries))
            return

        if len(missing) == len(queries):
            # Nothing answered at all - that is a dead link, not a firmware
            # that happens to implement none of its own command set. Report the
            # real cause rather than a list of every channel.
            raise HardwareError(
                f"no declared channel answered on {self._transport.address} - the link is "
                f"unresponsive rather than the channels being absent"
            ) from first_error

        detail = "\n".join(f"  {channel} -> {query}" for channel, query in missing)
        raise MissingChannelError(
            f"{len(missing)} declared channel(s) are not implemented by this instrument "
            f"(firmware {self._identity.get('firmware')}):\n{detail}\n"
            "An unimplemented mnemonic is answered with silence, so leaving one declared would "
            "stall every telemetry frame by the full read timeout. Fix the mnemonic, or remove "
            "the channel from hardware/cpx400dp/cpx400dp_channels.py and its table entry here."
        )

    async def _log_adopted_state(self) -> None:
        """Record the output state this driver inherited.

        connect() is passive: it neither enables an output nor disables one it
        finds already on. That makes what it *found* worth recording, so a run
        afterwards can show whether it began against a live supply."""
        states = {}
        for n in OUTPUTS:
            reply = await self._transport.query(f"OP{n}?")
            states[n] = _as_int(reply)
        live = [n for n, on in states.items() if on]
        if live:
            detail = ", ".join(
                f"output {n} ON at {self._cached.get(f'setpoint_voltage_{n}')} V / "
                f"{self._cached.get(f'setpoint_current_{n}')} A"
                for n in live
            )
            logger.warning("adopting an already-energized supply: %s (connect is passive, leaving as found)", detail)
        else:
            logger.info("both outputs are off at connect")

    async def _acquire_interface_lock(self) -> None:
        reply = await self._transport.query("IFLOCK")
        if _as_int(reply) != 1:
            raise HardwareError(
                "could not acquire the interface lock (IFLOCK answered "
                f"{reply!r}) - another interface holds it, or this interface has been "
                "disabled from taking control via the instrument's web page"
            )
        self._holds_interface_lock = True
        logger.info("holding the instrument's interface lock")

    async def disconnect(self) -> None:
        """Close the link, leaving the instrument's outputs exactly as they are.

        Deliberately asymmetric with what a motor controller's disconnect does.
        connect() adopts whatever output state it finds rather than asserting
        one, so teardown has no basis for deciding that an energized output was
        this driver's to switch off - it may be holding a bias on a DUT, a soak,
        or a battery under test. Releasing an interface lock this driver took is
        different: that IS ours, and holding it would refuse writes from every
        other interface until something reconnects.

        Tolerates an already-unreachable instrument: this runs on the teardown
        path, where raising would mask the failure already propagating."""
        if not self._transport.is_open:
            return
        # Released before the lock is taken, because query() takes that same
        # lock and it is not reentrant.
        if self._holds_interface_lock:
            try:
                await self._transport.query("IFUNLOCK")
            except HardwareError as exc:
                logger.warning("could not release the interface lock, continuing teardown: %s", exc)
            self._holds_interface_lock = False
        # Closing inside a transaction is what stops the socket disappearing
        # underneath a telemetry frame that is already reading. Without it, a
        # `disconnect` arriving over the command wire races the streaming loop,
        # whose read then fails - and runner.run() rightly treats a server task
        # dying on its own as fatal, so an orderly teardown would exit non-zero.
        async with self._transport.transaction():
            logger.info("disconnecting from %s, leaving outputs as they are", self._transport.address)
            await self._transport.close()

    async def get_status(self) -> dict:
        self._require_connected()
        return {
            "connected": True,
            "address": self._transport.address,
            **self._identity,
            "interface_lock_held": self._holds_interface_lock,
            "output_enabled_1": _as_int(await self._transport.query("OP1?")),
            "output_enabled_2": _as_int(await self._transport.query("OP2?")),
            "config_mode": self._cached.get("config_mode"),
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
        """One frame. None when disconnected.

        Four queries for the state tier every time, four more for the meter
        tier only when METER_INTERVAL_S has elapsed, and the cached tier carried
        from memory at no round-trip cost. The meter tier is held between reads
        because the instrument refreshes it 4 times a second whatever this
        driver does, so re-reading it per frame returns an unchanged register.

        The whole frame is read inside one transaction, for two reasons. A
        command cannot interleave and leave a frame half from before it and half
        from after. And the connected check happens *inside* the lock, so a
        `disconnect` cannot close the socket between the check and the read -
        that race is a teardown that exits the process non-zero, since
        runner.run() treats a telemetry task raising as a real device failure,
        which is exactly what it should do for an unexpected one."""
        state: Dict[str, Any] = {}
        async with self._transport.transaction():
            if not self.is_connected:
                return None
            for channel, (query, parser) in _STATE_QUERIES.items():
                state[channel] = parser(await self._transport.query_in_transaction(query))
            if self._meter_is_due():
                await self._read_meters_in_transaction()

        frame: Dict[str, Any] = dict(self._cached)
        frame.update(self._meters)
        for n in OUTPUTS:
            frame[f"output_enabled_{n}"] = bool(state[f"output_enabled_{n}"])
            frame.update(self._decode_limit_status(n, state[f"limit_status_{n}"]))
        return frame

    def _meter_is_due(self) -> bool:
        return (time.monotonic() - self._meters_read_at) >= METER_INTERVAL_S

    async def _read_meters_in_transaction(self) -> None:
        """Re-read the meter tier. Caller must hold the transaction lock."""
        for channel, (query, parser) in _METER_QUERIES.items():
            self._meters[channel] = parser(await self._transport.query_in_transaction(query))
        self._meters_read_at = time.monotonic()

    def _decode_limit_status(self, output: int, value: int) -> Dict[str, Any]:
        """Expand one LSR<n>? reading into its per-bit channels, and fold it
        into that output's sticky latch.

        The latch exists because the register clears on read. The mode bits
        (CV/CC/power limit) re-set immediately and so survive being read, but a
        trip shorter than one frame period would otherwise be consumed by the
        poll that saw it and never appear anywhere. The latch keeps every bit
        ever seen until clear_limit_status_latch_<n>."""
        self._latched_limit_status[output] |= value
        decoded: Dict[str, Any] = {
            f"limit_status_{output}": value,
            f"limit_status_latched_{output}": self._latched_limit_status[output],
        }
        for bit, stem in _LIMIT_STATUS_BITS.items():
            decoded[f"{stem}_{output}"] = bool(value & (1 << bit))
        return decoded

    # --- commands ----------------------------------------------------------

    async def execute(self, action: str, **params: Any) -> Any:
        self._require_connected()

        if action in _DRIVER_COMMANDS:
            output = int(action.rsplit("_", 1)[1])
            previous = self._latched_limit_status[output]
            self._latched_limit_status[output] = 0
            return previous

        if action in _QUERY_COMMANDS:
            query, parser = _QUERY_COMMANDS[action]
            result = parser(await self._transport.query(query))
            if action == "interface_lock":
                self._holds_interface_lock = result == 1
            elif action == "interface_unlock":
                # Only a 0 means released. IFUNLOCK answers -1 when this
                # interface has no authority to release the lock, and clearing
                # the flag on that would make disconnect() skip the release of
                # a lock still held.
                self._holds_interface_lock = self._holds_interface_lock and result != 0
            elif action.startswith("read_limit_status_"):
                # Read-and-clear, so these bits are consumed here and the next
                # streaming frame will not see them. Fold them into the latch
                # anyway, or calling this action would quietly punch a hole in
                # the very record the latch exists to keep.
                self._latched_limit_status[int(action.rsplit("_", 1)[1])] |= result
            return result

        if action in _VALUE_COMMANDS:
            template, cast, refresh = _VALUE_COMMANDS[action]
            if "value" not in params:
                raise HardwareError(f"action {action!r} requires a 'value' parameter")
            try:
                value = cast(params["value"])
            except (TypeError, ValueError) as exc:
                raise HardwareError(f"action {action!r} got an unusable value {params['value']!r}") from exc
            self._check_ceiling(action, value)
            await self._write_checked(template.format(value=value), refresh)
            if action.startswith("recall_setup_"):
                self._check_recalled_setpoints(action)
            return None

        if action in _BARE_COMMANDS:
            command, refresh = _BARE_COMMANDS[action]
            self._check_step_ceiling(action)
            await self._write_checked(command, refresh)
            if action == "reset":
                # *RST's documented defaults include "Lock cancelled", so the
                # instrument has dropped a lock this driver thinks it holds.
                # Left stale, disconnect() would try to release a lock that no
                # longer exists and log a spurious teardown warning.
                self._holds_interface_lock = False
            return None

        raise HardwareError(f"unknown action: {action}")

    def _check_ceiling(self, action: str, value: float) -> None:
        """Reject a commanded setpoint above the optional driver-side ceiling,
        before it reaches the wire.

        Deliberately does not constrain set_ovp/set_ocp. Those are protection
        thresholds, not output setpoints: a trip level above the ceiling
        endangers nothing, and refusing to *raise* a protection threshold would
        block a legitimate configuration rather than prevent a hazard."""
        if action in _VOLTAGE_LIMITED and self._max_voltage is not None and value > self._max_voltage:
            raise HardwareError(
                f"{action} refused: {value} V exceeds this backend's max_voltage of {self._max_voltage} V"
            )
        if action in _CURRENT_LIMITED and self._max_current is not None and value > self._max_current:
            raise HardwareError(
                f"{action} refused: {value} A exceeds this backend's max_current of {self._max_current} A"
            )

    def _check_step_ceiling(self, action: str) -> None:
        """Reject an increment whose predicted result would exceed the ceiling.

        Predicted, not known: a step command carries no value, so the result is
        computed from the cached setpoint and step size. That is exact as long
        as this driver is the only thing writing them, which is the same
        assumption the cached tier already rests on."""
        limited = _STEP_LIMITED.get(action)
        if limited is None:
            return
        setpoint_channel, step_channel, sign, quantity = limited
        ceiling = self._max_voltage if quantity == "voltage" else self._max_current
        if ceiling is None:
            return
        setpoint, step = self._cached.get(setpoint_channel), self._cached.get(step_channel)
        if setpoint is None or step is None:
            return
        predicted = setpoint + sign * step
        if predicted > ceiling:
            unit = "V" if quantity == "voltage" else "A"
            raise HardwareError(
                f"{action} refused: would set {predicted} {unit} "
                f"({setpoint} + {step}), exceeding this backend's ceiling of {ceiling} {unit}"
            )

    def _check_recalled_setpoints(self, action: str) -> None:
        """A recalled store can carry any setpoint, and there is no way to know
        what it holds before recalling it. So the ceiling is enforced after the
        fact: the values are already applied by the time this raises, and the
        message says so, because a test aborting into teardown is a better
        outcome than a silently over-ceiling supply."""
        output = int(action.rsplit("_", 1)[1])
        voltage = self._cached.get(f"setpoint_voltage_{output}")
        current = self._cached.get(f"setpoint_current_{output}")
        if self._max_voltage is not None and voltage is not None and voltage > self._max_voltage:
            raise HardwareError(
                f"{action} recalled {voltage} V, above this backend's max_voltage of "
                f"{self._max_voltage} V - the setpoint IS NOW APPLIED on output {output}"
            )
        if self._max_current is not None and current is not None and current > self._max_current:
            raise HardwareError(
                f"{action} recalled {current} A, above this backend's max_current of "
                f"{self._max_current} A - the setpoint IS NOW APPLIED on output {output}"
            )

    async def _write_checked(self, command: str, refresh: Tuple[str, ...]) -> None:
        """Send a command, confirm the instrument accepted it, then re-read
        whatever cached channels it changed - all in one transaction.

        The check is not belt-and-braces. This instrument accepts and then
        silently discards a write it dislikes: `V2 999` leaves the setpoint
        where it was, sends nothing back, and reports itself only in `EER?`.
        Both registers are read because they catch different things - a range
        error sets EER? and *ESR? bit 4, while an unrecognised mnemonic sets
        only *ESR? bit 5 and leaves EER? at zero.

        One transaction start to finish, so the registers read here cannot have
        been set by another caller's command, and the refreshed values cannot
        be from before this write."""
        async with self._transport.transaction():
            await self._transport.write_in_transaction(command)
            eer = _as_int(await self._transport.query_in_transaction("EER?"))
            esr = _as_int(await self._transport.query_in_transaction("*ESR?"))
            self._raise_for_errors(command, eer, esr)
            for channel in refresh:
                query, parser = _CACHED_QUERIES[channel]
                self._cached[channel] = parser(await self._transport.query_in_transaction(query))

    def _raise_for_errors(self, command: str, eer: int, esr: int) -> None:
        """Turn the two registers into an exception, a warning, or nothing."""
        if eer != 0:
            meaning = _EER_MEANINGS.get(eer, "unrecognised error code")
            raise HardwareError(f"{command!r} was refused: EER {eer} - {meaning}")
        if esr & _ESR_COMMAND_ERROR:
            raise HardwareError(
                f"{command!r} was not understood by the instrument (*ESR? command-error bit set) - "
                "the mnemonic or its syntax is wrong for this firmware"
            )
        if esr & _ESR_QUERY_ERROR:
            raise HardwareError(f"{command!r} produced a query error (*ESR? query-error bit set)")
        if esr & _ESR_EXECUTION_ERROR:
            # Reached only if EER? was somehow already consumed; the error
            # number is gone, so all that can be reported is that there was one.
            raise HardwareError(
                f"{command!r} caused an execution error (*ESR? bit 4) with no code left in EER?"
            )
        if esr & _ESR_VERIFY_TIMEOUT:
            # Not an error: the command completed, the output just did not
            # settle within the instrument's 5 s verify window. A large output
            # capacitor does this.
            logger.warning(
                "%r timed out verifying: the output did not reach the commanded value within the "
                "instrument's 5 s window", command,
            )
        if esr & _ESR_POWER_ON:
            logger.info("instrument reports it has been power-cycled since its status was last read")

    async def _refresh_cached(self, channels: Tuple[str, ...] | List[str]) -> None:
        """Re-read cached channels from the instrument."""
        async with self._transport.transaction():
            for channel in channels:
                query, parser = _CACHED_QUERIES[channel]
                self._cached[channel] = parser(await self._transport.query_in_transaction(query))
