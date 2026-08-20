"""Declared telemetry/command surface for the Keysight N6974A Advanced Power
System - the source of truth N6974aBackend implements, and what a testbed or
test case verifies against the live driver process
(TelemetryClient.verify_channels() / CommandClient.verify_actions()).

Kept separate from n6974a_backend.py so a channel/action list can be read, or
imported elsewhere, without pulling in the backend's asyncio and socket
implementation - the same split odrive_channels.py and cpx400dp_channels.py
make.

THE INSTRUMENT. 80 V, 25 A, 2000 W, single output, two-quadrant: it sources and
it sinks. Ratings are read from the instrument at connect rather than written
down here (`VOLTage? MAX` and friends), because the instrument is the authority
on its own limits and it reports them 2% above nameplate - 81.6 V and 25.5 A.

ONE MESSAGE PER FRAME, NO CACHED TIER. Every channel below is re-read on every
frame, in a single compound SCPI message. There is no settings cache and
therefore no assumption that nothing else touches the instrument: a front-panel
knob, the web interface or a second socket client can change any setting
mid-run and the next frame reports it truthfully. This is affordable only
because semicolons batch the whole frame into one round trip - 60 queries, one
exchange. What the frame actually costs is the measurement inside it: a
MEASure is an acquisition over SENSe:SWEep:NPLCycles power line cycles, ~21 ms
at the default 1 PLC, against ~1.4 ms for every other query in the message
combined.

MEASURE ONCE, FETCH THE REST. `voltage`, `current` and `power` come from one
acquisition: MEASure:VOLTage? performs it, and FETCh:CURRent?/FETCh:POWer? read
the same buffer rather than acquiring again. That halves the frame cost and,
more importantly, makes the three values simultaneous - V, I and P in one row
describe one instant, so power computed from them is consistent with the power
reported.

PRIORITY MODE DECIDES WHICH SETTINGS BIND. In voltage priority (`FUNCtion
VOLTage`) `setpoint_voltage` is the regulated value and `current_limit` /
`current_limit_negative` bound it. In current priority `setpoint_current` is
regulated and `voltage_limit` bounds it. Both sets are readable and published in
either mode, so `priority_mode` is what tells a reader which pair was in
control. Switching the mode turns the output off and reverts every output
setting to its reset value, which is why the backend refuses the switch while
the output is on.

THREE STATUS REGISTERS, CONDITION AND EVENT. Operation, Questionable 1 and
Questionable 2 are each read twice per frame: the condition register for what is
true now, and the event register for what became true since the previous frame.
The event registers are why this driver keeps no software latch of its own -
the instrument's positive-transition filter is set to pass every bit
(`STATus:QUEStionable:PTRansition?` reads 16383), so a protection that trips and
clears between two frames still appears in that frame's `*_event` channel. A
condition merely standing does not re-latch, so an event channel means "this
began during this frame", not "this is true".

Two event bits are set by this driver's own polling and carry no information
about the device under test: `measure_active_event` and
`waiting_for_measure_trigger_event` fire on every frame because the frame's own
MEASure initiates the measurement system.

PROTECTION. OV, OV-, CP+, CP-, OT, PF and EDP are always enabled and not
programmable; OC, the watchdog, the inhibit pin and user-defined protection are
programmable. Every one of them latches and disables the output until
`clear_protection` is called, and clearing only takes effect once the cause is
gone. Note what `protection_mode` means for a device that stores energy: LOWZ,
the reset default, actively sinks current for 2 ms while shutting down, at up to
120% of the current rating; HIGHZ disconnects without sinking and lets the
output decay through the instrument's passive network instead. A battery or a
large capacitor on the output is the case where that distinction matters.

HIGHZ is not absolute, and this model is the reason: because its output exceeds
60 V, the guide states the down-programmer remains enabled for a POWER-FAIL
fault whatever the mode is set to, for safety reasons. So a DUT that must never
be actively discharged can still be discharged by an AC-line fault. The
instrument also reverts the mode to LOWZ by itself whenever the priority mode
changes.

SENSE LEADS, AND WHY `sense_fault` IS NOT A TRIP. The three sense-lead faults
have three different consequences, and conflating them misreads a run:

  - OPEN (`sense_fault`, questionable bit 13): raised within ~50 us. The
    instrument reverts to LOCAL sensing and keeps regulating, but the voltage at
    the output terminals sits approximately 1% ABOVE the programmed value. So
    this is an accuracy fault, not a shutdown - and 1% of the programmed value is
    0.8 V at 80 V, larger than most tolerances a test would assert on. Clears by
    itself once the leads are reconnected.

    Whether the `voltage` channel shows that 1% is NOT established. The guide
    says only that the instrument measures "the actual voltage and current being
    supplied to the load", and does not say where the voltmeter taps relative to
    the sense terminals; confirming it needs an external meter across an
    energized output, which has not been done here. So do not assume this bit's
    error is visible in the telemetry - treat `sense_fault` itself as the
    evidence.
  - SHORTED: detected by over-voltage protection, which disables the output
    (`tripped_ov`). Not programmable.
  - REVERSED: detected by negative over-voltage protection, which disables the
    output (`tripped_ov_negative`). Not programmable.

Neither of the latter two can be detected without enabling the output, so
mis-wiring is only ever discovered by briefly energizing the load.

An open sense lead can be promoted to a shutdown, which is what a test that
cannot tolerate the 1% error should do: `OpenSense` is available as a signal
expression input, so defining an expression as `"OpenSense"` and routing it to
the user protection (`set_signal_expression`, `set_user_protection_source`,
`set_user_protection_state`) turns the fault into a latching output protection.
Detection can also be turned off entirely with `set_sense_fault_detection`,
which the guide suggests where lead configuration or load dynamics cause false
trips - that silences the report and leaves the 1% error in place.

CURRENT SINKING AND THE N7909A. Standing alone this unit sinks 10% of its rated
current indefinitely. Each N7909A power dissipator adds 1 kW of dissipation; a
2 kW model needs two to sink 100%, and with one it sinks 50%. The instrument
offers no direct query for how many are attached - per the guide, the only way
to know is `CURRent:LIMit:NEGative? MIN`, whose magnitude is 10%, 50% or 100% of
the maximum programmable current accordingly. That is what the backend checks
the declared `dissipators` count against, and a dissipator that was cabled after
the supply was powered on reads as absent because it is only recognised at
power-on.

ONE ALIAS, NOT TWO PARAMETERS. `OUTPut:PROTection:DELay` and
`CURRent:PROTection:DELay` are the same value on this firmware - writing either
moves both, measured - so only `ocp_delay` is declared. The OUTPut form appears
in the instrument's own `*LRN?` dump but in no command list in the guide; the
CURRent form is the documented one. Declaring both would put two columns in
every recorded row for one setting, and give two actions that silently
overwrite each other.

WHAT IS DELIBERATELY ABSENT, and how each absence announces itself. This unit
reports `*OPT?` as `0` - no options installed. An unavailable command is
answered with silence and discards the whole message it was part of, so
declaring any of these would cost a read timeout and a link resynchronisation on
every frame. Which error each names itself with was measured, and the two
classes are not the same thing:

  - `+302,"Option not installed"` - in the firmware, gated on hardware this unit
    does not have: output lists (`LIST`), the digitizer's programmable sample
    rate and point count (`SENSe:SWEep:TINTerval`/`POINts`), external data
    logging (`SENSe:ELOG`, `TRIGger:ELOG`), array readback (`FETCh:ARRay`), the
    black box recorder (`SYSTem:BBR`, `SENSe:BBR`), the disconnect and
    polarity-reverse relays (`OUTPut:RELay:POLarity`), measurement windowing
    (`SENSe:WINDow`), and `MEASure:POWer:MAXimum?`/`MINimum?`.
  - `+310,"The command is not supported by this model"` - not in this model's
    command set at all, whatever options are fitted: `VOLTage:BWIDth` (which the
    guide itself marks as present only for backward compatibility) and the
    arbitrary waveform subsystem (`ARB`).

The voltage and current `MAXimum`/`MINimum`/`HIGH`/`LOW` measurements DO work
here; only the power ones are gated. `TRIGger:ARB:SOURce` also answers normally
even though `ARB` itself does not, which is why it is declared.

Four commands the instrument does support are deliberately not exposed: the
CALibrate subsystem (which can degrade the instrument's accuracy, and which the
instrument itself guards with a password), `SYSTem:SECurity:IMMediate` (which
erases all user memory and reboots), `SYSTem:PASSword:FPANel:RESet` (which
clears the front-panel lockout password), and `HCOPy:SDUMp:DATA?` (which answers
with a binary image block that a line-oriented transport cannot carry).

RELAY CHANNELS ON A UNIT WITH NO RELAYS. This unit has neither disconnect nor
polarity-reverse relays - `OUTPut:RELay:POLarity?` answers `+302` - yet
`relay_lock` reads 1 and `OUTPut:RELay:LOCK` is accepted. The channel is
declared because it answers; on this unit it describes nothing physical.
"""
from __future__ import annotations

from typing import Dict, List

RATED_VOLTAGE_V = 80.0
RATED_CURRENT_A = 25.0
RATED_POWER_W = 2000.0
"""Nameplate ratings, used only to sanity-check the model at connect and to
report against. The binding numbers are read from the instrument, which allows
2% above these."""

MAX_DISSIPATORS = 2
"""N7909A units a 2 kW model can use. Each dissipates 1 kW."""

SINK_FRACTION_BY_DISSIPATORS: Dict[int, float] = {0: 0.10, 1: 0.50, 2: 1.00}
"""Fraction of the maximum programmable current this unit can sink, by how many
N7909A dissipators are connected and recognised. The 2 kW mapping: 1 kW models
reach 100% with a single dissipator, so this table is model-specific and the
backend confirms the model before trusting it."""


# --- Status register bit tables -------------------------------------------
# bit -> channel name. Each of these produces two boolean channels: `<name>`
# from the condition register and `<name>_event` from the event register.

OPERATION_BITS: Dict[int, str] = {
    0: "in_cv",  # output is regulating in constant voltage
    1: "in_cc",  # output is regulating in constant current
    2: "output_off",  # output is programmed off
    3: "waiting_for_measure_trigger",  # set by this driver's own MEASure - see the module docstring
    4: "waiting_for_transient_trigger",
    5: "measure_active",  # likewise set by this driver's own MEASure
    6: "transient_active",
    7: "user1_signal",  # the User1-defined expression is true
    8: "user2_signal",
}

QUESTIONABLE_BITS: Dict[int, str] = {
    0: "tripped_ov",  # over-voltage protection; hardware, always enabled, level is programmable
    1: "tripped_oc",  # over-current protection; programmable, off at reset
    2: "tripped_power_fail",  # AC mains low-line or brownout
    3: "tripped_over_power_positive",  # CP+, against a built-in threshold
    4: "tripped_over_temperature",  # OT; see over_temperature_margin for how close it is
    5: "tripped_over_power_negative",  # CP-, internally dissipated power; raised by an absent N7909A
    6: "tripped_ov_negative",  # OV-, reversed sense leads or a negative voltage at the terminals
    7: "in_positive_limit",  # LIM+, output is in positive voltage or current limit
    8: "in_negative_limit",  # LIM-, output is in negative current limit (sinking at the limit)
    9: "inhibited",  # the external INHibit digital pin disabled the output
    10: "unregulated",  # output is not regulating
    11: "tripped_watchdog",  # the I/O watchdog timer expired
    12: "tripped_excessive_dynamic",  # EDP, repetitive large voltage swings
    13: "sense_fault",  # an OPEN remote sense lead: the output still regulates, from local sense, ~1% high (see the sense-lead note below)
}

QUESTIONABLE2_BITS: Dict[int, str] = {
    0: "tripped_user_protection",  # a user-defined expression disabled the output
    1: "in_positive_peak_limit",  # IPK+
    2: "in_negative_peak_limit",  # IPK-
    3: "current_sharing_fault",  # CSF, on paralleled units
}

STATUS_REGISTERS: Dict[str, Dict[int, str]] = {
    "operation": OPERATION_BITS,
    "questionable": QUESTIONABLE_BITS,
    "questionable2": QUESTIONABLE2_BITS,
}


def _register_channels() -> List[str]:
    """Every channel the three status registers produce: the two raw integers
    per register, plus one boolean per documented bit for each."""
    names: List[str] = []
    for register, bits in STATUS_REGISTERS.items():
        names.append(f"{register}_status")  # raw condition register
        names.append(f"{register}_events")  # raw event register
        names.extend(bits.values())
        names.extend(f"{stem}_event" for stem in bits.values())
    return names


STATUS_CHANNELS: List[str] = _register_channels()

# --- Settings, re-read every frame like everything else -------------------
# The trailing comment on each line is the SCPI query it is read from.
SETTING_CHANNELS: List[str] = [
    "output_enabled",  # bool - OUTPut?
    "priority_mode",  # str VOLT|CURR - FUNCtion? - which of the settings below is regulating
    # Voltage
    "setpoint_voltage",  # V - VOLTage? - regulated value in voltage priority
    "voltage_limit",  # V - VOLTage:LIMit? - ceiling in current priority
    "voltage_mode",  # str FIX|STEP - VOLTage:MODE? - transient mode
    "triggered_voltage",  # V - VOLTage:TRIGgered? - value applied on a transient trigger
    "voltage_slew",  # V/s - VOLTage:SLEW? - 9.9E37 means unlimited
    "voltage_slew_max",  # bool - VOLTage:SLEW:MAXimum? - override to the fastest the hardware allows
    "ovp_level",  # V - VOLTage:PROTection? - over-voltage trip level
    "voltage_priority_resistance",  # ohm - VOLTage:RESistance? - emulated source resistance
    "voltage_priority_resistance_enabled",  # bool - VOLTage:RESistance:STATe?
    # Current
    "setpoint_current",  # A - CURRent? - regulated value in current priority; may be negative
    "current_limit",  # A - CURRent:LIMit? - positive ceiling in voltage priority
    "current_limit_negative",  # A - CURRent:LIMit:NEGative? - sinking floor; always negative
    "current_mode",  # str FIX|STEP - CURRent:MODE?
    "triggered_current",  # A - CURRent:TRIGgered?
    "current_slew",  # A/s - CURRent:SLEW?
    "current_slew_max",  # bool - CURRent:SLEW:MAXimum?
    "ocp_enabled",  # bool - CURRent:PROTection:STATe? - off at reset
    "ocp_delay",  # s - CURRent:PROTection:DELay? - 0 to 0.255 s. OUTPut:PROTection:DELay is the same parameter on this firmware, so it is not declared separately (see the alias note above)
    "ocp_delay_start",  # str SCH|CCTR - CURRent:PROTection:DELay:STARt? - settings change, or any entry into current limit
    "current_sharing",  # bool - CURRent:SHARing? - for paralleled units
    "resistance",  # ohm - RESistance?
    "resistance_enabled",  # bool - RESistance:STATe?
    # Output and protection
    "protection_mode",  # str LOWZ|HIGHZ - OUTPut:PROTection:MODE? - whether a shutdown sinks the DUT's energy
    "protection_coupling",  # bool - OUTPut:PROTection:COUPle?
    "watchdog_enabled",  # bool - OUTPut:PROTection:WDOG? - shuts the output down when SCPI traffic stops
    "watchdog_delay",  # s - OUTPut:PROTection:WDOG:DELay? - 1 to 3600
    "user_protection_enabled",  # bool - OUTPut:PROTection:USER?
    "user_protection_source",  # str - OUTPut:PROTection:USER:SOURce? - EXPR1-8 or NONE
    "inhibit_mode",  # str LATC|LIVE|OFF - OUTPut:INHibit:MODE?
    "output_delay_rise",  # s - OUTPut:DELay:RISE? - turn-on sequencing delay
    "output_delay_fall",  # s - OUTPut:DELay:FALL?
    "output_coupling",  # bool - OUTPut:COUPle?
    "output_coupling_delay_offset",  # s - OUTPut:COUPle:DOFFset?
    "relay_lock",  # bool - OUTPut:RELay:LOCK? - relays held closed rather than switching with the output
    "power_on_state",  # str RST|RCL0 - OUTPut:PON:STATe?
    # Measurement configuration
    "sense_fault_detection",  # bool - SENSe:FAULt:STATe? - whether an unstrapped sense lead is reported
    "sense_function_voltage",  # bool - SENSe:FUNCtion:VOLTage? - whether voltage is digitized
    "sense_function_current",  # bool - SENSe:FUNCtion:CURRent?
    "nplc",  # power line cycles - SENSe:SWEep:NPLCycles? - sets how long each MEASure takes
    "voltage_measurement_range",  # V - SENSe:VOLTage:RANGe?
    "current_measurement_range",  # A - SENSe:CURRent:RANGe? - one range only without Option 301
    "current_measurement_autorange",  # bool - SENSe:CURRent:RANGe:AUTO?
    # Interface and display
    "display_enabled",  # bool - DISPlay?
    "display_view",  # str - DISPlay:VIEW?
    "digital_input",  # bitmask int - DIGital:INPut:DATA? - state of the 7-pin rear connector
]

# --- Measurements, from one acquisition per frame -------------------------
METER_CHANNELS: List[str] = [
    "voltage",  # V - MEASure:VOLTage? - performs the acquisition the next two read from
    "current",  # A - FETCh:CURRent? - same acquisition, so simultaneous with voltage
    "power",  # W - FETCh:POWer? - likewise
    "amp_hours",  # Ah - FETCh:AHOur? - accumulated since the last reset_amp_hours; survives runs
    "watt_hours",  # Wh - FETCh:WHOur? - likewise
]

TEMPERATURE_CHANNELS: List[str] = [
    "ambient_temperature",  # degC - SYSTem:TEMPerature:AMBient? - at the air inlet
    "over_temperature_margin",  # degC - OUTPut:PROTection:TEMPerature:MARGin? - headroom before OT trips
]

# --- Driver-side channels, not read from the instrument -------------------
CLAMPED_QUANTITIES = ("voltage", "current", "sink_current")
"""The three directions a commanded value can be clamped in. Voltage and
current ceilings come from the instrument's own maximums; the sink ceiling comes
from the declared N7909A count."""

DRIVER_CHANNELS: List[str] = [
    *(f"clamped_{quantity}" for quantity in CLAMPED_QUANTITIES),
    # bool - sticky: a commanded value in this direction has been clamped at
    # some point. Sticky because a clamp is momentary while its consequence -
    # the test asked for something it did not get - lasts for the whole run.
    # Cleared with clear_clamped_latch.
    *(f"clamped_{quantity}_request" for quantity in CLAMPED_QUANTITIES),
    # float - the most recent value that was clamped in this direction, so the
    # record shows what was asked for and not only what was applied.
    "link_resynchronisations",
    # int - how many times the socket has been reopened to recover from a
    # desynchronised link. Zero on a healthy run; a climbing value is a real
    # fault even though each recovery succeeded.
]

TELEMETRY_CHANNELS: List[str] = [
    *STATUS_CHANNELS,
    *SETTING_CHANNELS,
    *METER_CHANNELS,
    *TEMPERATURE_CHANNELS,
    *DRIVER_CHANNELS,
]

# --- Commands --------------------------------------------------------------
# Complete coverage of what this unit implements, less the four exclusions
# named in the module docstring.
#
# A documented query becomes a `read_*` action only where its value is not
# already a telemetry channel above; where it is, the channel is the exposure and
# a duplicate action would be a second way to say the same thing. Three
# deliberate exceptions: the read-and-clear registers (`SYSTem:ERRor?`, `*ESR?`,
# the three `STATus:...:EVENt?`), which a caller may want to consume on purpose;
# and `read_ambient_temperature`, so a one-off temperature can be had without
# waiting for a frame.

DIGITAL_PINS = (1, 2, 3, 4, 5, 6, 7)
"""Pins on the rear digital port.

Not interchangeable, though the guide gives one flat list of functions for all
of them. Which functions each pin accepts was measured by offering every one to
every pin:

    pin 1     DIO, DINPut, TOUTput, TINPut, EXPRession<n>, FAULt
    pin 2     DIO, DINPut, TOUTput, TINPut, EXPRession<n>
    pin 3     DIO, DINPut, TOUTput, TINPut, EXPRession<n>, INHibit
    pins 4-7  DIO, DINPut, TOUTput, TINPut, EXPRession<n>, ONCouple, OFFCouple

So FAULt is pin 1 only, INHibit is pin 3 only (which agrees with the guide's
note that the Inhibit input is pin 3), and the output-coupling functions are
pins 4-7 only. Anything else answers `-224,"Illegal parameter value"`.

Setting a pin to ONCouple or OFFCouple also moves that pin's POLarity, so
`set_digital_pin_function` reads both back."""

THRESHOLD_COMPARATORS = (1, 2, 3, 4)
"""The four signal comparators, each of which can watch voltage, current,
power, amp-hours or watt-hours and drive a status bit or a protection."""

SIGNAL_EXPRESSIONS = (1, 2, 3, 4, 5, 6, 7, 8)
"""User-defined signal expressions, referenced by protection, status and
trigger sources as EXPR<n>."""

COMMAND_CHANNELS: List[str] = [
    # --- Output regulation ------------------------------------------------
    "set_priority_mode",  # FUNCtion VOLTage|CURRent - REFUSED while the output is on: it drops the output and resets every setting
    "set_voltage",  # VOLTage <v> - clamped to the instrument maximum
    "set_voltage_limit",  # VOLTage:LIMit <v> - clamped
    "set_triggered_voltage",  # VOLTage:TRIGgered <v> - clamped
    "set_voltage_mode",  # VOLTage:MODE FIXed|STEP
    "set_voltage_slew",  # VOLTage:SLEW <v/s>|MAXimum|INFinity
    "set_voltage_slew_max",  # VOLTage:SLEW:MAXimum <bool>
    "set_ovp",  # VOLTage:PROTection <v> - clamped to the instrument's 96 V maximum
    "set_voltage_priority_resistance",  # VOLTage:RESistance <ohm>
    "set_voltage_priority_resistance_state",  # VOLTage:RESistance:STATe <bool>
    "set_current",  # CURRent <a> - clamped both directions; negative sinks
    "set_current_limit",  # CURRent:LIMit <a> - clamped
    "set_current_limit_negative",  # CURRent:LIMit:NEGative <a> - clamped to the sink ceiling
    "set_triggered_current",  # CURRent:TRIGgered <a> - clamped
    "set_current_mode",  # CURRent:MODE FIXed|STEP
    "set_current_slew",  # CURRent:SLEW <a/s>|MAXimum|INFinity
    "set_current_slew_max",  # CURRent:SLEW:MAXimum <bool>
    "set_current_sharing",  # CURRent:SHARing <bool>
    "set_resistance",  # RESistance <ohm>
    "set_resistance_state",  # RESistance:STATe <bool>
    # --- Output state and sequencing --------------------------------------
    "enable_output",  # OUTPut <bool> - takes tens of milliseconds to complete
    "set_output_delay_rise",  # OUTPut:DELay:RISE <s>
    "set_output_delay_fall",  # OUTPut:DELay:FALL <s>
    "set_output_coupling",  # OUTPut:COUPle <bool>
    "set_output_coupling_delay_offset",  # OUTPut:COUPle:DOFFset <s>
    "set_output_coupling_on_source",  # OUTPut:COUPle:ON:SOURce EXPRession<1-8>|NONE
    "set_output_coupling_off_source",  # OUTPut:COUPle:OFF:SOURce EXPRession<1-8>|NONE
    "read_max_coupling_delay_offset",  # OUTPut:COUPle:MAX:DOFFset?
    "set_power_on_state",  # OUTPut:PON:STATe RST|RCL0 - non-volatile
    "set_relay_lock",  # OUTPut:RELay:LOCK <bool>
    # --- Protection --------------------------------------------------------
    "clear_protection",  # OUTPut:PROTection:CLEar - only takes effect once the cause is gone
    "set_protection_mode",  # OUTPut:PROTection:MODE LOWZ|HIGHZ - HIGHZ will not sink the DUT's energy while shutting down
    "set_protection_coupling",  # OUTPut:PROTection:COUPle <bool>
    "set_ocp_state",  # CURRent:PROTection:STATe <bool>
    "set_ocp_delay",  # CURRent:PROTection:DELay <s> - 0 to 0.255
    "set_ocp_delay_start",  # CURRent:PROTection:DELay:STARt SCHange|CCTRans
    "set_watchdog_state",  # OUTPut:PROTection:WDOG <bool> - shuts the output down when SCPI traffic stops
    "set_watchdog_delay",  # OUTPut:PROTection:WDOG:DELay <s> - 1 to 3600
    "set_user_protection_state",  # OUTPut:PROTection:USER <bool>
    "set_user_protection_source",  # OUTPut:PROTection:USER:SOURce EXPRession<1-8>|NONE
    "set_inhibit_mode",  # OUTPut:INHibit:MODE LATChing|LIVE|OFF
    # --- Measurement configuration and readback ---------------------------
    "set_nplc",  # SENSe:SWEep:NPLCycles <cycles> - the dominant term in this driver's frame period
    "set_voltage_measurement_range",  # SENSe:VOLTage:RANGe <v>
    "set_current_measurement_range",  # SENSe:CURRent:RANGe <a>
    "set_current_measurement_autorange",  # SENSe:CURRent:RANGe:AUTO <bool>
    "set_sense_function_voltage",  # SENSe:FUNCtion:VOLTage <bool>
    "set_sense_function_current",  # SENSe:FUNCtion:CURRent <bool>
    "set_sense_fault_detection",  # SENSe:FAULt:STATe <bool> - turns off reporting of an unstrapped sense lead
    "reset_amp_hours",  # SENSe:AHOur:RESet
    "reset_watt_hours",  # SENSe:WHOur:RESet
    "read_voltage_rms",  # MEASure:VOLTage:ACDC? - total RMS, AC+DC
    "read_voltage_max",  # MEASure:VOLTage:MAXimum?
    "read_voltage_min",  # MEASure:VOLTage:MINimum?
    "read_voltage_high",  # MEASure:VOLTage:HIGH? - the high level of a pulse waveform
    "read_voltage_low",  # MEASure:VOLTage:LOW?
    "read_current_rms",  # MEASure:CURRent:ACDC?
    "read_current_max",  # MEASure:CURRent:MAXimum?
    "read_current_min",  # MEASure:CURRent:MINimum?
    "read_current_high",  # MEASure:CURRent:HIGH?
    "read_current_low",  # MEASure:CURRent:LOW?
    # --- Signal comparators and expressions -------------------------------
    *(f"set_threshold_function_{n}" for n in THRESHOLD_COMPARATORS),  # SENSe:THReshold<n>:FUNCtion VOLT|CURR|POW|AHO|WHO
    *(f"set_threshold_operation_{n}" for n in THRESHOLD_COMPARATORS),  # SENSe:THReshold<n>:OPERation GT|LT
    *(f"set_threshold_voltage_{n}" for n in THRESHOLD_COMPARATORS),  # SENSe:THReshold<n>:VOLTage <v>
    *(f"set_threshold_current_{n}" for n in THRESHOLD_COMPARATORS),  # SENSe:THReshold<n>:CURRent <a>
    *(f"set_threshold_power_{n}" for n in THRESHOLD_COMPARATORS),  # SENSe:THReshold<n>:POWer <w>
    *(f"set_threshold_amp_hour_{n}" for n in THRESHOLD_COMPARATORS),  # SENSe:THReshold<n>:AHOur <ah>
    *(f"set_threshold_watt_hour_{n}" for n in THRESHOLD_COMPARATORS),  # SENSe:THReshold<n>:WHOur <wh>
    *(f"read_threshold_function_{n}" for n in THRESHOLD_COMPARATORS),  # SENSe:THReshold<n>:FUNCtion?
    *(f"read_threshold_operation_{n}" for n in THRESHOLD_COMPARATORS),  # SENSe:THReshold<n>:OPERation?
    *(f"read_threshold_level_{n}" for n in THRESHOLD_COMPARATORS),  # reads whichever level matches this comparator's function
    *(f"set_signal_expression_{n}" for n in SIGNAL_EXPRESSIONS),  # SYSTem:SIGNal:DEFine EXPRession<n>,"<expression>"
    *(f"read_signal_expression_{n}" for n in SIGNAL_EXPRESSIONS),  # SYSTem:SIGNal:DEFine? EXPRession<n>
    # --- Digital port ------------------------------------------------------
    "set_digital_output_data",  # DIGital:OUTPut:DATA <mask>
    "read_digital_output_data",  # DIGital:OUTPut:DATA?
    "set_digital_trigger_out_bus",  # DIGital:TOUTput:BUS <bool>
    *(f"set_digital_pin_function_{n}" for n in DIGITAL_PINS),  # DIGital:PIN<n>:FUNCtion DIO|DINPut|EXPRession<1-8>|FAULt|INHibit|ONCouple|OFFCouple|TOUTput|TINPut
    *(f"set_digital_pin_polarity_{n}" for n in DIGITAL_PINS),  # DIGital:PIN<n>:POLarity POSitive|NEGative
    *(f"read_digital_pin_function_{n}" for n in DIGITAL_PINS),  # DIGital:PIN<n>:FUNCtion?
    *(f"read_digital_pin_polarity_{n}" for n in DIGITAL_PINS),  # DIGital:PIN<n>:POLarity?
    # --- Transient and acquisition trigger systems ------------------------
    # STEP transients are standard; LIST and ARB need Option 303 and are absent.
    "initiate_transient",  # INITiate:TRANsient
    "initiate_transient_continuous",  # INITiate:CONTinuous:TRANsient <bool>
    "abort_transient",  # ABORt:TRANsient
    "trigger_transient",  # TRIGger:TRANsient:IMMediate
    "set_transient_trigger_source",  # TRIGger:TRANsient:SOURce BUS|EXTernal|IMMediate|EXPRession<1-8>|PIN<1-7>
    "read_transient_trigger_source",  # TRIGger:TRANsient:SOURce?
    "set_step_trigger_out",  # STEP:TOUTput <bool>
    "initiate_acquire",  # INITiate:ACQuire
    "abort_acquire",  # ABORt:ACQuire
    "trigger_acquire",  # TRIGger:ACQuire:IMMediate
    "set_acquire_trigger_source",  # TRIGger:ACQuire:SOURce BUS|CURRent1|EXTernal|EXPRession<1-8>|PIN<1-7>|TRANsient1|VOLTage1
    "read_acquire_trigger_source",  # TRIGger:ACQuire:SOURce?
    "set_acquire_trigger_voltage",  # TRIGger:ACQuire:VOLTage <v>
    "set_acquire_trigger_voltage_slope",  # TRIGger:ACQuire:VOLTage:SLOPe POSitive|NEGative
    "set_acquire_trigger_current",  # TRIGger:ACQuire:CURRent <a>
    "set_acquire_trigger_current_slope",  # TRIGger:ACQuire:CURRent:SLOPe POSitive|NEGative
    "set_acquire_trigger_out",  # TRIGger:ACQuire:TOUTput <bool>
    "read_acquire_trigger_count",  # TRIGger:ACQuire:INDices:COUNt?
    "read_acquire_trigger_indices",  # TRIGger:ACQuire:INDices:DATA?
    "set_arb_trigger_source",  # TRIGger:ARB:SOURce - accepted although Arb itself is absent
    "read_arb_trigger_source",  # TRIGger:ARB:SOURce?
    # --- Status registers --------------------------------------------------
    "read_operation_events",  # STATus:OPERation:EVENt? - read and CLEAR; the frame consumes this already
    "read_questionable_events",  # STATus:QUEStionable:EVENt? - likewise
    "read_questionable2_events",  # STATus:QUEStionable2:EVENt? - likewise
    "set_operation_enable",  # STATus:OPERation:ENABle <mask>
    "set_operation_ptr",  # STATus:OPERation:PTRansition <mask>
    "set_operation_ntr",  # STATus:OPERation:NTRansition <mask>
    "set_questionable_enable",  # STATus:QUEStionable:ENABle <mask>
    "set_questionable_ptr",  # STATus:QUEStionable:PTRansition <mask>
    "set_questionable_ntr",  # STATus:QUEStionable:NTRansition <mask>
    "set_questionable2_enable",  # STATus:QUEStionable2:ENABle <mask>
    "set_questionable2_ptr",  # STATus:QUEStionable2:PTRansition <mask>
    "set_questionable2_ntr",  # STATus:QUEStionable2:NTRansition <mask>
    "read_operation_enable",  # STATus:OPERation:ENABle?
    "read_questionable_enable",  # STATus:QUEStionable:ENABle?
    "read_questionable2_enable",  # STATus:QUEStionable2:ENABle?
    "read_operation_ptr",  # STATus:OPERation:PTRansition? - 511 at preset, which is why no software latch is needed
    "read_questionable_ptr",  # STATus:QUEStionable:PTRansition? - 16383 at preset
    "read_questionable2_ptr",  # STATus:QUEStionable2:PTRansition? - 127 at preset
    "read_operation_ntr",  # STATus:OPERation:NTRansition? - 0 at preset; nothing here latches falling edges
    "read_questionable_ntr",  # STATus:QUEStionable:NTRansition?
    "read_questionable2_ntr",  # STATus:QUEStionable2:NTRansition?
    "preset_status",  # STATus:PRESet - resets every Enable, PTR and NTR register
    *(f"set_operation_user_source_{n}" for n in (1, 2)),  # STATus:OPERation:USER<n>:SOURce EXPRession<1-8>|NONE
    *(f"read_operation_user_source_{n}" for n in (1, 2)),  # STATus:OPERation:USER<n>:SOURce?
    # --- Errors and identity ----------------------------------------------
    "read_error",  # SYSTem:ERRor? - read and CLEAR one entry; the write check consumes these first
    "drain_errors",  # driver-side: read SYSTem:ERRor? until the queue is empty, returning every entry
    "read_identity",  # *IDN? - manufacturer, model, serial number, firmware revision
    "read_options",  # *OPT? - installed options; 0 on this unit
    "read_learn_string",  # *LRN? - every settable value as a SCPI command string
    "read_scpi_version",  # SYSTem:VERSion?
    "read_line_frequency",  # SYSTem:LFRequency?
    "set_line_frequency_mode",  # SYSTem:LFRequency:MODE AUTO|MAN50|MAN60
    "read_calibration_date",  # CALibrate:DATE? - read only; the rest of CALibrate is deliberately not exposed
    "read_calibration_count",  # CALibrate:COUNt?
    "read_power_limit",  # [SOURce:]POWer:LIMit? - the instrument's output power limit, 2000 W here
    "read_data_format",  # FORMat? - must be ASCii for this transport; the setter is deliberately not exposed
    "read_byte_order",  # FORMat:BORDer?
    # --- Interface, display, system ---------------------------------------
    "set_remote_state",  # SYSTem:COMMunicate:RLSTate LOCal|REMote|RWLock - RWLock locks out the front panel
    "read_remote_state",  # SYSTem:COMMunicate:RLSTate?
    "read_control_socket_port",  # SYSTem:COMMunicate:TCPip:CONTrol?
    "set_display_state",  # DISPlay <bool>
    "set_display_view",  # DISPlay:VIEW METER_VI|METER_VP|METER_VIP
    "set_display_saver",  # DISPlay:SAVer <bool>
    "read_display_saver",  # DISPlay:SAVer?
    "set_lxi_identify",  # LXI:IDENtify:STATe <bool> - blinks the front panel identify indicator
    "read_lxi_identify",  # LXI:IDENtify:STATe?
    "set_date",  # SYSTem:DATE <yyyy>,<mm>,<dd>
    "set_time",  # SYSTem:TIME <hh>,<mm>,<ss>
    "read_date",  # SYSTem:DATE?
    "read_time",  # SYSTem:TIME?
    "read_ambient_temperature",  # SYSTem:TEMPerature:AMBient? - also a telemetry channel
    "reboot",  # SYSTem:REBoot - drops the link; the driver must reconnect afterwards
    # --- IEEE-488 common commands -----------------------------------------
    "clear_status",  # *CLS - clears the status structure and this session's error queue
    "reset",  # *RST - output off, and every setting back to its reset value
    "save_state",  # *SAV <0-9> - to non-volatile memory
    "recall_state",  # *RCL <0-9> - can apply any stored setpoint, so the ceiling is checked afterwards
    "trigger",  # *TRG - a BUS trigger for whichever system is armed
    "wait_operation_complete",  # *WAI
    "operation_complete",  # *OPC
    "read_operation_complete",  # *OPC?
    "self_test",  # *TST? - 0 means passed; an absent N7909A shows up here
    "read_event_status",  # *ESR? - read and CLEAR
    "set_event_status_enable",  # *ESE <mask>
    "read_event_status_enable",  # *ESE?
    "set_service_request_enable",  # *SRE <mask>
    "read_service_request_enable",  # *SRE?
    "read_status_byte",  # *STB?
    # --- Driver-side, not instrument commands -----------------------------
    "clear_clamped_latch",  # resets the sticky clamped_* channels and returns what they held
    "read_ratings",  # the instrument limits read at connect, and the sink ceiling derived from the dissipator count
]
