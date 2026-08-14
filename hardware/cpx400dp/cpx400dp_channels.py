"""Declared telemetry/command surface for the TTi CPX400DP dual-output bench
power supply - the source of truth Cpx400dpBackend implements, and what a
testbed or test case verifies against the live driver process
(TelemetryClient.verify_channels() / CommandClient.verify_actions()).

Kept separate from cpx400dp_backend.py so a channel/action list can be read,
or imported elsewhere, without pulling in the backend's asyncio and socket
implementation - the same split odrive_channels.py makes.

Command coverage is complete: every command in the CPX400DP instruction
manual's Command List is here. Telemetry coverage is deliberately not a
mirror of it, for a reason particular to this instrument - see the three
tiers below.

NAMING. Channel names are readable rather than literal, because TTi's
mnemonics (`V1O`, `CP2`, `LSR1`) are too terse to use directly - the exact
mnemonic each channel is read from appears in the trailing comment, the same
way odrive_channels.py annotates each channel with its real attribute path.
Names are quantity-first with a numeric output suffix (`voltage_1`, not
`output1_voltage`), so the two outputs' readings for one quantity sit adjacent
in a wide CSV, which is the comparison a person actually makes. `1` is the
Master (left-hand) output and `2` the Slave (right-hand), per the manual.

Measured values and setpoints are separate channels: `voltage_1` is what the
output is really producing (`V1O?`), `setpoint_voltage_1` is what was asked
for (`V1?`). That split is what makes "did the supply reach what I commanded"
answerable from a recorded run alone.

THE FOUR TIERS. Every channel below is telemetry - all of them appear in every
published frame - but they are *acquired* four different ways, and the
distinctions are load-bearing rather than cosmetic:

  1. STATE_CHANNELS - re-read every frame. Output on/off and the limit status
     register: instrument state, which changes at the speed of the events that
     cause it rather than at any metering rate. Polling these fast is what
     caught an OVP trip within a single frame period. 4 queries per frame.

  2. METER_CHANNELS - measured voltage and current, re-read at METER_RATE_HZ
     and carried between reads. These are measurements of the physical world,
     but the instrument's specification puts a hard ceiling on how often they
     can change: METER SPECIFICATIONS gives "Dual 4 digit meters with 10mm
     (0.39") LEDs. Reading rate 4 Hz", with meter resolution 10 mV / 10 mA and
     accuracy 0.1% of reading +-2 digits (voltage) and 0.3% +-2 digits
     (current). Polling them at frame rate re-reads a register the instrument
     refreshes four times a second - measured directly here, where a
     continuously decaying output read back as a staircase, each value held
     across 6-10 consecutive polls before stepping.

     So these are read at a rate the instrument can actually support, and held
     in between. A consequence worth knowing when reading a recorded run: a
     repeated value in consecutive rows may be a held reading rather than a
     re-measured one. That was already true before the driver held anything -
     the instrument itself returned duplicates for the same reason - and frame
     `t` against the known refresh rate is what separates the two.

     Note this ceiling applies to the *reported* measurement, not to the
     supply's behaviour. Regulation and protection are far faster: OVP is
     specified at ~1 ms and tripped inside one 19 ms frame, while OCP is
     "measure-and-compare implemented in firmware" at ~500 ms - about two meter
     updates, which is consistent with the firmware comparison being fed by
     this same measurement path.

  3. CACHED_CHANNELS - read once at connect(), then re-read only after this
     driver issues a command that changes them. These are *settings*: numbers
     that sit still until something writes them, and this driver is the only
     thing that writes them. They are carried in every frame from memory, at no
     round-trip cost, so a recorded row is still self-describing.

     The assumption this tier rests on, stated plainly so it can be revisited:
     no human turns the front-panel knobs during a run. If that stops being
     true, a value here can be stale for the rest of a run with nothing
     indicating it, and the fix is to move the affected channel into tier 1.

  4. Not telemetry at all - the read-and-clear error registers (`EER?`,
     `QER?`, `*ESR?`). These are consumed by the driver's own write
     verification after every command it sends, which is the only way to learn
     that the instrument silently refused a write (measured: `V2 999` leaves
     the setpoint untouched, answers nothing, and reports itself only as
     `EER?` = 100). Streaming them would race that check for the same
     single-copy value. They are reachable as explicit actions in
     COMMAND_CHANNELS instead.

     `LSR<n>?` is also read-and-clear but IS streamed, because it clears and
     immediately re-sets to reflect present state - see the limit-status note
     below.

LIMIT STATUS. `LSR<n>?` is a bit field, streamed once per frame and decoded
into one boolean channel per bit, plus the raw integer and a driver-side
sticky latch.

The manual describes these as edge events ("Set when output *enters* voltage
limit"). Measured against a real CPX400DP (firmware 2.03-4.12), they are
LEVELS: the register clears on read and is set again on the very next read for
as long as the condition holds. Verified for bit 0 (output regulating in CV),
bit 1 (regulating in CC, into a deliberate short), bit 2 (over-voltage trip)
and bit 3 (over-current trip). So every bit here is named as a state
(`in_cv_1`, `tripped_ov_1`), not an event. This contradicts the manual, and
the measurement is the reason.

Bit 6 - the trip class needing a front-panel or AC-power reset - was NOT set by
either an OVP or an OCP trip, so both are soft trips. It remains unverified;
nothing reachable from software provoked it.

`limit_status_latched_<n>` still earns its place even though nothing latches in
the instrument, and now for a sharper reason: since every bit is a level, a
condition that begins and ends between two frames leaves no trace at all. The
latch accumulates every bit ever seen, so a sub-frame trip is still visible.
Clear it with `clear_limit_status_latch_<n>`.

CLEARING A TRIP - and `TRIPRST` is not the answer, despite being documented as
"attempt to clear all trip conditions". Measured, it did nothing in every case
tried. What actually cleared each trip differed by trip:

  - OVP: raising `ovp_<n>` back above the voltage setpoint cleared the bit on
    its own, with no `TRIPRST` at all. Removing the cause is the whole
    mechanism.
  - OCP: raising `ocp_<n>` back above the current being drawn did NOT clear it,
    and neither did `TRIPRST`. It cleared on an explicit
    `enable_output_<n>(False)` - even though the trip had already switched the
    output off.

So a recovery step should remove the cause AND explicitly command the output
off, then re-enable; that sequence covers both. A step that calls `trip_reset`
and assumes recovery will hang on a trip that never clears.

POWER ENVELOPE. This is a PowerFlex supply: 60 V and 20 A are both reachable
but not simultaneously (60 V/7 A, 42 V/10 A, 20 V/20 A, 420 W). Commanding
both is accepted; the instrument then runs unregulated, which shows up here as
`in_power_limit_<n>`.

CURRENT READBACK ACCURACY AT LOW CURRENT. `current_<n>` is not trustworthy to
better than a few tens of milliamps, and not as a fixed offset that could be
subtracted out. With nothing connected and the output off it read 0.019 A on
output 1 and 0.053 A on output 2; while genuinely regulating at a 0.100 A limit
into a short, output 2 read 0.115 A. That is consistent with a 20 A-class
instrument whose readback resolution is simply coarse down here, not with a
calibration offset. A Bound on current near zero, or one distinguishing 100 mA
from 150 mA, will not do what its author intends.
"""
from __future__ import annotations

from typing import List

OUTPUTS = (1, 2)
"""The instrument's two outputs. 1 is the Master (left-hand) output, 2 the
Slave (right-hand), per the manual's <n> nomenclature."""


def _per_output(*names: str) -> List[str]:
    """Expand `("voltage",)` to `["voltage_1", "voltage_2"]`, so an output can
    never be added to one channel and forgotten on another."""
    return [f"{name}_{n}" for name in names for n in OUTPUTS]


METER_RATE_HZ = 4.0
"""The instrument's own measurement reporting rate, from its specification -
"Dual 4 digit meters ... Reading rate 4 Hz". The ceiling on how often
METER_CHANNELS can carry anything new, whatever rate the driver polls at."""


# --- Tier 1: re-read every frame (4 instrument queries) ---------------------
STATE_CHANNELS: List[str] = [
    # Nominally a setting, polled every frame because the INSTRUMENT changes
    # it: an OVP or OCP trip switches the output off with no command from us,
    # and this is the ground truth for that. Measured to trip within a single
    # frame period, which is the whole reason this is not in the cached tier.
    # Note it does not imply zero volts - 2.748 V was still present on the
    # terminals immediately after switching off, decaying through the output
    # capacitance.
    *_per_output("output_enabled"),  # bool - OP<n>?
    # LSR<n>? decoded. One query per output produces all of the below.
    *_per_output("limit_status"),  # bitmask int - LSR<n>? raw, as read this frame
    *_per_output("limit_status_latched"),  # bitmask int - driver-side OR of every LSR<n>? ever read
    *_per_output("in_cv"),  # bool - LSR<n>? bit 0 - output regulating in constant voltage
    *_per_output("in_cc"),  # bool - LSR<n>? bit 1 - output regulating in constant current
    *_per_output("tripped_ov"),  # bool - LSR<n>? bit 2 - over-voltage trip; a level, and it switches the output off itself
    *_per_output("tripped_oc"),  # bool - LSR<n>? bit 3 - over-current trip; likewise (see the trip-clearing note above)
    *_per_output("in_power_limit"),  # bool - LSR<n>? bit 4 - unregulated, against the power envelope (UNVERIFIED)
    *_per_output("tripped_latching"),  # bool - LSR<n>? bit 6 - trip needing a front-panel or AC-power reset (UNVERIFIED; neither OVP nor OCP sets it)
]

# --- Tier 2: re-read at METER_RATE_HZ, held in between (4 queries) ----------
# The instrument refreshes these four times a second; polling them per frame
# re-reads an unchanged register. Meter resolution is 10 mV / 10 mA, so the
# millivolt and milliamp digits these replies carry are finer than the
# measurement behind them.
METER_CHANNELS: List[str] = [
    *_per_output("voltage"),  # V - V<n>O? - volts actually being produced; +-0.1% of reading +-2 digits
    *_per_output("current"),  # A - I<n>O? - amps actually being drawn; +-0.3% of reading +-2 digits (see the accuracy note below)
]

# --- Tier 3: read at connect, refreshed after our own writes ----------------
CACHED_CHANNELS: List[str] = [
    *_per_output("setpoint_voltage"),  # V - V<n>? - voltage commanded, not measured
    *_per_output("setpoint_current"),  # A - I<n>? - current limit; the output switches to CC at this value
    *_per_output("ovp"),  # V - OVP<n>? - over-voltage trip threshold (note the reply mnemonic is VP<n>)
    *_per_output("ocp"),  # A - OCP<n>? - over-current trip threshold (reply mnemonic is CP<n>)
    *_per_output("delta_voltage"),  # V - DELTAV<n>? - step size used by increment_voltage/decrement_voltage
    *_per_output("delta_current"),  # A - DELTAI<n>? - step size used by increment_current/decrement_current
    *_per_output("limit_status_enable"),  # bitmask int - LSE<n>? - which limit bits raise LIM<n> in the status byte
    "config_mode",  # int - CONFIG? - 2 = outputs independent, 0 = output 2 tracks output 1
    "tracking_ratio",  # % - RATIO? - output 2 as a percentage of output 1, only effective in tracking mode
    # Undocumented: these three answer correctly on firmware 2.03-4.12 but
    # appear nowhere in the manual's Command List. Kept because this
    # instrument self-assigns a link-local address, so recording which address
    # a run actually talked to is the forensic detail you want when it moves.
    "ip_address",  # str - IPADDR? - UNDOCUMENTED
    "netmask",  # str - NETMASK? - UNDOCUMENTED
    "net_config",  # str - NETCONFIG? - UNDOCUMENTED (reports DHCP even when it has fallen back to link-local)
]

TELEMETRY_CHANNELS: List[str] = [*STATE_CHANNELS, *METER_CHANNELS, *CACHED_CHANNELS]

# --- Commands --------------------------------------------------------------
# Complete coverage of the manual's Command List.
#
# Indexed commands get one action per output (`set_voltage_1`, `set_voltage_2`)
# rather than one action taking an `output` parameter. That doubles the list,
# and buys the thing this codebase already values: verify_actions() then
# positively confirms BOTH outputs are addressable, instead of only confirming
# that a `set_voltage` action exists. The manual documents error 103 for
# "attempt to read or write a command on the second output when it is not
# available", so the difference is real.
#
# A documented *query* becomes a `read_*` action only when its value is not
# already a telemetry channel above; where it is, the channel is the exposure
# and a duplicate action would be a second way to say the same thing.
COMMAND_CHANNELS: List[str] = [
    # --- Per output: setpoints -------------------------------------------
    *_per_output("set_voltage"),  # V<n> <v> - set output voltage
    *_per_output("set_voltage_verify"),  # V<n>V <v> - set and wait for the output to reach it - BLOCKS UP TO 5 s
    *_per_output("set_current"),  # I<n> <v> - set current limit
    *_per_output("set_ovp"),  # OVP<n> <v> - set over-voltage trip threshold
    *_per_output("set_ocp"),  # OCP<n> <v> - set over-current trip threshold
    *_per_output("set_delta_voltage"),  # DELTAV<n> <v> - step size for increment_voltage/decrement_voltage
    *_per_output("set_delta_current"),  # DELTAI<n> <v> - step size for increment_current/decrement_current
    # --- Per output: stepping --------------------------------------------
    *_per_output("increment_voltage"),  # INCV<n> - raise voltage by delta_voltage
    *_per_output("increment_voltage_verify"),  # INCV<n>V - and wait for it - BLOCKS UP TO 5 s
    *_per_output("decrement_voltage"),  # DECV<n> - lower voltage by delta_voltage
    *_per_output("decrement_voltage_verify"),  # DECV<n>V - and wait for it - BLOCKS UP TO 5 s
    *_per_output("increment_current"),  # INCI<n> - raise current limit by delta_current
    *_per_output("decrement_current"),  # DECI<n> - lower current limit by delta_current
    # --- Per output: output state, stores, status ------------------------
    *_per_output("enable_output"),  # OP<n> <0|1> - switch this output on or off
    *_per_output("save_setup"),  # SAV<n> <store> - save this output's setup to store 0-9
    *_per_output("recall_setup"),  # RCL<n> <store> - recall this output's setup from store 0-9
    *_per_output("set_limit_status_enable"),  # LSE<n> <v> - which limit bits raise LIM<n>
    *_per_output("read_limit_status"),  # LSR<n>? - read and CLEAR; the streaming poll already consumes this
    *_per_output("clear_limit_status_latch"),  # driver-side, not an instrument command - resets limit_status_latched_<n>
    # --- Global output control -------------------------------------------
    "enable_all_outputs",  # OPALL <0|1> - switch both outputs together
    "trip_reset",  # TRIPRST - attempt to clear all trip conditions
    "set_config_mode",  # CONFIG <n> - 2 = independent, 0 = output 2 tracks output 1; error 104 if output 2 is on
    "set_tracking_ratio",  # RATIO <n> - output 2 as a percentage (0-100) of output 1, for tracking mode
    # --- Interface control ------------------------------------------------
    "go_local",  # LOCAL - return to front-panel control; does NOT release an interface lock
    "interface_lock",  # IFLOCK - request exclusive control; returns 1 if acquired, -1 if unavailable
    "interface_unlock",  # IFUNLOCK - release it; returns 0 if released, -1 with EER 200 if not ours
    "read_interface_lock",  # IFLOCK? - 1 = held by us, 0 = no lock, -1 = held elsewhere
    # --- Status and error registers ---------------------------------------
    "clear_status",  # *CLS - clear the status structure
    "read_execution_error",  # EER? - read and CLEAR; the driver's own write check normally consumes this first
    "read_query_error",  # QER? - read and CLEAR
    "read_event_status",  # *ESR? - read and CLEAR; likewise consumed by the write check
    "set_event_status_enable",  # *ESE <v>
    "read_event_status_enable",  # *ESE?
    "set_service_request_enable",  # *SRE <v> - GPIB artifact, of little use over ethernet
    "read_service_request_enable",  # *SRE? - GPIB artifact
    "read_status_byte",  # *STB? - GPIB artifact
    "set_parallel_poll_enable",  # *PRE <v> - GPIB artifact, meaningless over TCP
    "read_parallel_poll_enable",  # *PRE? - GPIB artifact
    "read_individual_status",  # *IST? - GPIB artifact
    # --- Synchronisation --------------------------------------------------
    "operation_complete",  # *OPC - set the operation-complete bit
    "read_operation_complete",  # *OPC? - always answers 1, since all commands are sequential
    "wait_operation_complete",  # *WAI - documented as taking no additional action on this instrument
    # --- Miscellaneous ----------------------------------------------------
    "reset",  # *RST - back to remote defaults: 1 V, 1 A, 10 mV/10 mA steps, OVP 66 V, OCP 22 A, tracking cancelled
    "trigger",  # *TRG - accepted, performs no action (the supply has no trigger capability)
    "self_test",  # *TST? - always answers 0 (the supply has no self-test capability)
    "read_identity",  # *IDN? - manufacturer, model, serial number, firmware revision
    "read_bus_address",  # ADDRESS? - GPIB bus address, usable as a general identifier
]
