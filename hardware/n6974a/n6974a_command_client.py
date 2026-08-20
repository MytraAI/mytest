"""N6974A-specific convenience methods layered on the generic CommandClient -
one method per channel in n6974a_channels.py's COMMAND_CHANNELS. Each docstring
names the real SCPI command the call sends, so it can be looked up in the
instrument's own guide for more detail than the one-liner gives.

Setters that command energy return what the instrument actually holds
afterwards, rather than None. That is not decoration: a commanded value beyond
what the hardware allows is applied at the limit instead of refused, so the
return value is how a caller learns it did not get what it asked for. The
`clamped_*` telemetry channels record the same thing for the run.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..clients.command_client import CommandClient
from protocol.wire import DEFAULT_N6974A_COMMAND_ENDPOINT

DEFAULT_TIMEOUT_MS = 25_000
"""Must outlast the worst case the driver itself allows, or this client would
give up on a command the backend is still legitimately waiting on - and a
command timeout is fatal to a CommandClient by design.

That worst case is the backend's SLOW_COMMAND_TIMEOUT_S (20 s, sized for
`*TST?`, which takes 5.2 s measured, and for `*OPC?`/`*WAI`, which block until
pending operations finish) plus the telemetry frame that may hold the link ahead
of it. Everything else answers in about a millisecond; this ceiling exists for
the three slow commands, not for the ordinary ones."""


class N6974aCommandClient(CommandClient):
    """CommandClient with named sugar for every declared N6974A command channel."""

    def __init__(
        self,
        endpoint: str = DEFAULT_N6974A_COMMAND_ENDPOINT,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ):
        super().__init__(endpoint, timeout_ms)

    # --- output regulation -------------------------------------------------

    def set_priority_mode(self, mode: str) -> str:
        """FUNCtion - VOLTage or CURRent priority.

        REFUSED while the output is on. The instrument would switch the output
        off and revert every output setting to its reset value, so this is only
        allowed on a de-energized supply."""
        return self.execute("set_priority_mode", value=mode)

    def set_voltage(self, volts: float) -> float:
        """VOLTage - the regulated output voltage in voltage priority mode.
        Returns the value actually applied, which is clamped to what the
        instrument allows."""
        return self.execute("set_voltage", value=volts)

    def set_voltage_limit(self, volts: float) -> float:
        """VOLTage:LIMit - the voltage ceiling in current priority mode."""
        return self.execute("set_voltage_limit", value=volts)

    def set_triggered_voltage(self, volts: float) -> float:
        """VOLTage:TRIGgered - the voltage applied when the transient system is
        triggered, with voltage_mode set to STEP."""
        return self.execute("set_triggered_voltage", value=volts)

    def set_voltage_mode(self, mode: str) -> str:
        """VOLTage:MODE - FIXed, or STEP to apply triggered_voltage on a
        trigger. LIST and ARB need Option 303, which this unit does not have."""
        return self.execute("set_voltage_mode", value=mode)

    def set_voltage_slew(self, volts_per_second: Any) -> float:
        """VOLTage:SLEW - volts per second, or MAXimum/INFinity for as fast as
        the output circuit allows. Use it to stop a capacitive load crossing
        into current limit while up- or down-programming: the ceiling is
        (current limit - load current) / load capacitance."""
        return self.execute("set_voltage_slew", value=volts_per_second)

    def set_voltage_slew_max(self, enabled: bool) -> bool:
        """VOLTage:SLEW:MAXimum - override the slew setting with the fastest the
        hardware can do."""
        return self.execute("set_voltage_slew_max", value=enabled)

    def set_ovp(self, volts: float) -> float:
        """VOLTage:PROTection - the over-voltage trip level.

        Always enabled and not disableable; the reset value is 120% of the
        voltage rating, which protects nothing in practice. The circuit monitors
        the *sense* terminals rather than the output terminals, which is what
        lets it protect the load rather than the supply - and also why shorted
        sense leads trip it by themselves. An OPEN sense lead does not trip it;
        that is the sense fault, and it leaves the output regulating 1% high."""
        return self.execute("set_ovp", value=volts)

    def set_voltage_priority_resistance(self, ohms: float) -> float:
        """VOLTage:RESistance - emulated source resistance, for imitating a
        non-ideal source such as a battery. Voltage priority mode only."""
        return self.execute("set_voltage_priority_resistance", value=ohms)

    def set_voltage_priority_resistance_state(self, enabled: bool) -> bool:
        """VOLTage:RESistance:STATe - enable resistance programming in voltage
        priority mode."""
        return self.execute("set_voltage_priority_resistance_state", value=enabled)

    def set_current(self, amps: float) -> float:
        """CURRent - the regulated output current in current priority mode.

        May be negative, which holds a constant *sink* current - discharging a
        battery at a fixed rate, for instance. How negative it may go depends on
        how many N7909A dissipators the supply recognised."""
        return self.execute("set_current", value=amps)

    def set_current_limit(self, amps: float) -> float:
        """CURRent:LIMit - the positive current ceiling in voltage priority
        mode. The output crosses into current limit here."""
        return self.execute("set_current_limit", value=amps)

    def set_current_limit_negative(self, amps: float) -> float:
        """CURRent:LIMit:NEGative - the sinking floor in voltage priority mode.

        Always negative. Bounded by the dissipators: 10% of rating with none,
        50% with one, 100% with two."""
        return self.execute("set_current_limit_negative", value=amps)

    def set_triggered_current(self, amps: float) -> float:
        """CURRent:TRIGgered - the current applied on a transient trigger, with
        current_mode set to STEP."""
        return self.execute("set_triggered_current", value=amps)

    def set_current_mode(self, mode: str) -> str:
        """CURRent:MODE - FIXed or STEP."""
        return self.execute("set_current_mode", value=mode)

    def set_current_slew(self, amps_per_second: Any) -> float:
        """CURRent:SLEW - amps per second, or MAXimum/INFinity."""
        return self.execute("set_current_slew", value=amps_per_second)

    def set_current_slew_max(self, enabled: bool) -> bool:
        """CURRent:SLEW:MAXimum - override the slew setting with the fastest the
        hardware can do."""
        return self.execute("set_current_slew_max", value=enabled)

    def set_current_sharing(self, enabled: bool) -> bool:
        """CURRent:SHARing - current sharing for units paralleled together."""
        return self.execute("set_current_sharing", value=enabled)

    def set_resistance(self, ohms: float) -> float:
        """RESistance - output resistance level."""
        return self.execute("set_resistance", value=ohms)

    def set_resistance_state(self, enabled: bool) -> bool:
        """RESistance:STATe - enable output resistance programming."""
        return self.execute("set_resistance_state", value=enabled)

    # --- output state ------------------------------------------------------

    def enable_output(self, enabled: bool) -> bool:
        """OUTPut - switch the output on or off.

        Takes tens of milliseconds to complete, and any configured
        output_delay_rise/fall applies on top. Switching off does not mean zero
        volts instantly: what happens next depends on protection_mode and on
        what the load is holding."""
        return self.execute("enable_output", value=enabled)

    def set_output_delay_rise(self, seconds: float) -> float:
        """OUTPut:DELay:RISE - turn-on sequencing delay."""
        return self.execute("set_output_delay_rise", value=seconds)

    def set_output_delay_fall(self, seconds: float) -> float:
        """OUTPut:DELay:FALL - turn-off sequencing delay."""
        return self.execute("set_output_delay_fall", value=seconds)

    def set_output_coupling(self, enabled: bool) -> bool:
        """OUTPut:COUPle - couple this output's state to other units."""
        return self.execute("set_output_coupling", value=enabled)

    def set_output_coupling_delay_offset(self, seconds: float) -> float:
        """OUTPut:COUPle:DOFFset - delay offset that synchronises coupled state
        changes across units."""
        return self.execute("set_output_coupling_delay_offset", value=seconds)

    def set_output_coupling_on_source(self, source: str) -> None:
        """OUTPut:COUPle:ON:SOURce - EXPR1-8 or NONE."""
        return self.execute("set_output_coupling_on_source", value=source)

    def set_output_coupling_off_source(self, source: str) -> None:
        """OUTPut:COUPle:OFF:SOURce - EXPR1-8 or NONE."""
        return self.execute("set_output_coupling_off_source", value=source)

    def read_max_coupling_delay_offset(self) -> float:
        """OUTPut:COUPle:MAX:DOFFset? - the offset this unit requires."""
        return self.execute("read_max_coupling_delay_offset")

    def set_power_on_state(self, state: str) -> str:
        """OUTPut:PON:STATe - RST for reset values at power-on, or RCL0 to
        recall stored state 0. Non-volatile: it outlasts this run and every
        subsequent power cycle until changed."""
        return self.execute("set_power_on_state", value=state)

    def set_relay_lock(self, enabled: bool) -> bool:
        """OUTPut:RELay:LOCK - hold the output relays closed rather than letting
        them switch with the output."""
        return self.execute("set_relay_lock", value=enabled)

    # --- protection --------------------------------------------------------

    def clear_protection(self) -> Any:
        """OUTPut:PROTection:CLEar - clear a latched protection.

        Every protection on this instrument latches and holds the output
        disabled. This only takes effect once the *cause* is gone, so a step
        that clears and immediately re-enables without removing the cause will
        trip straight back. Returns the questionable status register and the
        output state after clearing, so the caller can see whether it worked."""
        return self.execute("clear_protection")

    def set_protection_mode(self, mode: str) -> str:
        """OUTPut:PROTection:MODE - LOWZ or HIGHZ shutdown behaviour.

        LOWZ, the reset default, programs the output to zero and actively sinks
        current for 2 ms while disconnecting, at up to 120% of the current
        rating. HIGHZ disconnects without sinking and lets the energy dissipate
        through the instrument's passive network instead. HIGHZ is the one to
        use when the device under test can source energy - a battery, another
        supply, a large capacitor - since LOWZ would discharge it hard at the
        moment of a fault.

        Two caveats. The instrument reverts to LOWZ by itself whenever the
        priority mode changes. And HIGHZ is not absolute on this model: because
        its output exceeds 60 V, the guide states the down-programmer stays
        enabled for a POWER-FAIL fault regardless of this setting, so an AC-line
        failure can still actively discharge the DUT."""
        return self.execute("set_protection_mode", value=mode)

    def set_protection_coupling(self, enabled: bool) -> bool:
        """OUTPut:PROTection:COUPle - propagate a protection shutdown to other
        coupled units."""
        return self.execute("set_protection_coupling", value=enabled)

    def set_ocp_state(self, enabled: bool) -> bool:
        """CURRent:PROTection:STATe - enable over-current protection, which
        disables the output when it reaches the current limit and crosses from
        constant voltage into current limit. Off at reset."""
        return self.execute("set_ocp_state", value=enabled)

    def set_ocp_delay(self, seconds: float) -> float:
        """CURRent:PROTection:DELay - 0 to 0.255 s of grace before an
        over-current shuts the output down, so a momentary inrush or a
        programmed step does not trip it.

        `OUTPut:PROTection:DELay` is the same parameter on this firmware - it is
        what the instrument's own `*LRN?` dump calls it - so there is no separate
        action for it."""
        return self.execute("set_ocp_delay", value=seconds)

    def set_ocp_delay_start(self, start: str) -> str:
        """CURRent:PROTection:DELay:STARt - SCHange starts the delay on a
        settings change, CCTRans on any transition into current limit."""
        return self.execute("set_ocp_delay_start", value=start)

    def set_watchdog_state(self, enabled: bool) -> bool:
        """OUTPut:PROTection:WDOG - shut the output down when no SCPI traffic
        reaches the instrument for watchdog_delay seconds.

        A dead-man's switch: this driver polls telemetry continuously, so the
        timer never expires while it is alive, and a driver killed with the
        output energized leaves the instrument to de-energize itself. Two
        caveats. Front-panel activity does not reset the timer, but traffic on
        *any* remote interface does, so a browser sitting on the instrument's
        web page will hold it off. And this driver never arms it - that is a
        test's decision, not the driver's."""
        return self.execute("set_watchdog_state", value=enabled)

    def set_watchdog_delay(self, seconds: int) -> int:
        """OUTPut:PROTection:WDOG:DELay - 1 to 3600 seconds."""
        return self.execute("set_watchdog_delay", value=seconds)

    def set_user_protection_state(self, enabled: bool) -> bool:
        """OUTPut:PROTection:USER - enable protection driven by a user-defined
        signal expression.

        This is the route for turning a condition the instrument treats as
        informational into a latching shutdown. `OpenSense` is the notable one:
        an open sense lead otherwise just leaves the output regulating about 1%
        high."""
        return self.execute("set_user_protection_state", value=enabled)

    def set_user_protection_source(self, source: str) -> str:
        """OUTPut:PROTection:USER:SOURce - EXPR1-8 or NONE."""
        return self.execute("set_user_protection_source", value=source)

    def set_inhibit_mode(self, mode: str) -> str:
        """OUTPut:INHibit:MODE - LATChing, LIVE or OFF, for the external
        shutdown signal on digital pin 3."""
        return self.execute("set_inhibit_mode", value=mode)

    # --- measurement -------------------------------------------------------

    def set_nplc(self, cycles: float) -> float:
        """SENSe:SWEep:NPLCycles - measurement time in power line cycles.

        The dominant term in this driver's frame period: each frame contains one
        acquisition, so 1 PLC (the default) gives ~21 ms of the ~32 ms frame,
        and 0.1 PLC gives a ~4 ms frame. Reducing it costs the line-frequency
        noise rejection an integral number of cycles buys."""
        return self.execute("set_nplc", value=cycles)

    def set_voltage_measurement_range(self, volts: float) -> float:
        """SENSe:VOLTage:RANGe - select a voltage measurement range."""
        return self.execute("set_voltage_measurement_range", value=volts)

    def set_current_measurement_range(self, amps: float) -> float:
        """SENSe:CURRent:RANGe - select a current measurement range. This unit
        has one range; the low range needs Option 301."""
        return self.execute("set_current_measurement_range", value=amps)

    def set_current_measurement_autorange(self, enabled: bool) -> bool:
        """SENSe:CURRent:RANGe:AUTO - seamless measurement autoranging, which
        needs Option 301 to do anything on this unit."""
        return self.execute("set_current_measurement_autorange", value=enabled)

    def set_sense_function_voltage(self, enabled: bool) -> bool:
        """SENSe:FUNCtion:VOLTage - whether voltage is digitized. Turning it off
        makes the voltage measurement channels meaningless."""
        return self.execute("set_sense_function_voltage", value=enabled)

    def set_sense_function_current(self, enabled: bool) -> bool:
        """SENSe:FUNCtion:CURRent - whether current is digitized."""
        return self.execute("set_sense_function_current", value=enabled)

    def set_sense_fault_detection(self, enabled: bool) -> bool:
        """SENSe:FAULt:STATe - whether an open remote sense lead is reported.

        An open sense lead is not a shutdown: the instrument reverts to local
        sensing and keeps regulating, with the output terminals about 1% above
        the programmed value. Whether the measured `voltage` channel reveals
        that 1% is not documented and has not been verified here, so treat the
        `sense_fault` bit as the evidence rather than the reading. Turning detection off silences the report and
        leaves that error in place, which the guide suggests only where the lead
        configuration or load dynamics cause false trips.

        To go the other way and make an open sense lead abort a run, route it
        into the user protection: set_signal_expression(n, "OpenSense"), then
        set_user_protection_source("EXPR<n>") and
        set_user_protection_state(True).

        Shorted and reversed sense leads are a different matter entirely - they
        are caught by over-voltage and negative over-voltage protection, which
        disable the output and are not programmable or affected by this
        setting."""
        return self.execute("set_sense_fault_detection", value=enabled)

    def reset_amp_hours(self) -> float:
        """SENSe:AHOur:RESet - zero the amp-hour accumulator. It accumulates
        continuously and survives runs, so a test measuring charge should zero
        it at the start rather than subtracting."""
        return self.execute("reset_amp_hours")

    def reset_watt_hours(self) -> float:
        """SENSe:WHOur:RESet - zero the watt-hour accumulator."""
        return self.execute("reset_watt_hours")

    def read_voltage_rms(self) -> float:
        """MEASure:VOLTage:ACDC? - total RMS voltage, AC plus DC."""
        return self.execute("read_voltage_rms")

    def read_voltage_max(self) -> float:
        """MEASure:VOLTage:MAXimum? - the highest sample in a fresh
        acquisition."""
        return self.execute("read_voltage_max")

    def read_voltage_min(self) -> float:
        """MEASure:VOLTage:MINimum?"""
        return self.execute("read_voltage_min")

    def read_voltage_high(self) -> float:
        """MEASure:VOLTage:HIGH? - the High level of a pulse waveform, from a
        16-bin histogram of the acquisition rather than the raw maximum."""
        return self.execute("read_voltage_high")

    def read_voltage_low(self) -> float:
        """MEASure:VOLTage:LOW? - the Low level of a pulse waveform."""
        return self.execute("read_voltage_low")

    def read_current_rms(self) -> float:
        """MEASure:CURRent:ACDC? - total RMS current, AC plus DC."""
        return self.execute("read_current_rms")

    def read_current_max(self) -> float:
        """MEASure:CURRent:MAXimum?"""
        return self.execute("read_current_max")

    def read_current_min(self) -> float:
        """MEASure:CURRent:MINimum?"""
        return self.execute("read_current_min")

    def read_current_high(self) -> float:
        """MEASure:CURRent:HIGH?"""
        return self.execute("read_current_high")

    def read_current_low(self) -> float:
        """MEASure:CURRent:LOW?"""
        return self.execute("read_current_low")

    # --- comparators and expressions ---------------------------------------

    def set_threshold_function(self, comparator: int, function: str) -> str:
        """SENSe:THReshold<n>:FUNCtion - which quantity comparator 1-4 watches:
        VOLT, CURR, POW, AHO or WHO."""
        return self.execute(f"set_threshold_function_{comparator}", value=function)

    def set_threshold_operation(self, comparator: int, operation: str) -> str:
        """SENSe:THReshold<n>:OPERation - GT or LT."""
        return self.execute(f"set_threshold_operation_{comparator}", value=operation)

    def set_threshold_level(self, comparator: int, function: str, level: float) -> float:
        """SENSe:THReshold<n>:<function> - the level for the given quantity.

        The level registers are per quantity, so setting the current level does
        not change the voltage level. `function` picks which one, and only the
        one matching the comparator's own FUNCtion has any effect."""
        action = {
            "VOLT": "set_threshold_voltage", "CURR": "set_threshold_current",
            "POW": "set_threshold_power", "AHO": "set_threshold_amp_hour",
            "WHO": "set_threshold_watt_hour",
        }
        key = str(function).strip().upper()[:4]
        if key not in action:
            raise ValueError(f"function must be one of {', '.join(sorted(action))}, got {function!r}")
        return self.execute(f"{action[key]}_{comparator}", value=level)

    def read_threshold_function(self, comparator: int) -> str:
        """SENSe:THReshold<n>:FUNCtion?"""
        return self.execute(f"read_threshold_function_{comparator}")

    def read_threshold_operation(self, comparator: int) -> str:
        """SENSe:THReshold<n>:OPERation?"""
        return self.execute(f"read_threshold_operation_{comparator}")

    def read_threshold_level(self, comparator: int) -> Dict[str, Any]:
        """The level actually in use by comparator 1-4: reads its FUNCtion and
        then the matching level register, returning both."""
        return self.execute(f"read_threshold_level_{comparator}")

    def set_signal_expression(self, number: int, expression: str) -> str:
        """SYSTem:SIGNal:DEFine EXPRession<n> - define one of eight signal
        expressions, referenced elsewhere as EXPR<n> by protection sources,
        status bits, output coupling and trigger sources.

        Inputs are named states and events - OutpOn, OutpOff, OutpSettled, CV,
        CC, CL+, CL-, VL+, Prot, OpenSense, OnC, OffC - combined with And, Or,
        Not, parentheses and Delay. `"OpenSense"` routed to the user protection
        is how an open sense lead becomes a shutdown rather than a 1% voltage
        error."""
        return self.execute(f"set_signal_expression_{number}", value=expression)

    def read_signal_expression(self, number: int) -> str:
        """SYSTem:SIGNal:DEFine? EXPRession<n>"""
        return self.execute(f"read_signal_expression_{number}")

    # --- digital port ------------------------------------------------------

    def set_digital_output_data(self, mask: int) -> int:
        """DIGital:OUTPut:DATA - drive the pins configured as digital outputs."""
        return self.execute("set_digital_output_data", value=mask)

    def read_digital_output_data(self) -> int:
        """DIGital:OUTPut:DATA?"""
        return self.execute("read_digital_output_data")

    def set_digital_trigger_out_bus(self, enabled: bool) -> bool:
        """DIGital:TOUTput:BUS - allow BUS triggers on digital port pins."""
        return self.execute("set_digital_trigger_out_bus", value=enabled)

    def set_digital_pin_function(self, pin: int, function: str) -> str:
        """DIGital:PIN<n>:FUNCtion - DIO, DINPut, TOUTput, TINPut or EXPR1-8 on
        any pin, plus FAULt on pin 1, INHibit on pin 3, and ONCouple/OFFCouple
        on pins 4-7. Those restrictions were measured, not documented; see
        n6974a_channels.DIGITAL_PINS for the table.

        Returns the pin's function and polarity together, because setting
        ONCouple or OFFCouple also moves the polarity."""
        return self.execute(f"set_digital_pin_function_{pin}", value=function)

    def set_digital_pin_polarity(self, pin: int, polarity: str) -> str:
        """DIGital:PIN<n>:POLarity - POSitive or NEGative."""
        return self.execute(f"set_digital_pin_polarity_{pin}", value=polarity)

    def read_digital_pin_function(self, pin: int) -> str:
        """DIGital:PIN<n>:FUNCtion?"""
        return self.execute(f"read_digital_pin_function_{pin}")

    def read_digital_pin_polarity(self, pin: int) -> str:
        """DIGital:PIN<n>:POLarity?"""
        return self.execute(f"read_digital_pin_polarity_{pin}")

    # --- transients and acquisition ----------------------------------------

    def initiate_transient(self) -> None:
        """INITiate:TRANsient - arm the transient system, so a trigger applies
        triggered_voltage/triggered_current."""
        return self.execute("initiate_transient")

    def initiate_transient_continuous(self, enabled: bool) -> bool:
        """INITiate:CONTinuous:TRANsient - re-arm automatically after each
        trigger."""
        return self.execute("initiate_transient_continuous", value=enabled)

    def abort_transient(self) -> None:
        """ABORt:TRANsient - cancel an armed or running transient. Does not stop
        continuous re-arming; turn that off first."""
        return self.execute("abort_transient")

    def trigger_transient(self) -> None:
        """TRIGger:TRANsient:IMMediate - trigger the armed transient now."""
        return self.execute("trigger_transient")

    def set_transient_trigger_source(self, source: str) -> str:
        """TRIGger:TRANsient:SOURce - BUS, EXTernal, IMMediate, EXPR1-8 or
        PIN1-7. Unlike the acquisition system this one takes IMMediate, and does
        not take a measured quantity as a source."""
        return self.execute("set_transient_trigger_source", value=source)

    def read_transient_trigger_source(self) -> str:
        """TRIGger:TRANsient:SOURce?"""
        return self.execute("read_transient_trigger_source")

    def set_step_trigger_out(self, enabled: bool) -> bool:
        """STEP:TOUTput - emit a trigger out when a transient step occurs."""
        return self.execute("set_step_trigger_out", value=enabled)

    def initiate_acquire(self) -> None:
        """INITiate:ACQuire - arm the measurement trigger system."""
        return self.execute("initiate_acquire")

    def abort_acquire(self) -> None:
        """ABORt:ACQuire - cancel a triggered measurement."""
        return self.execute("abort_acquire")

    def trigger_acquire(self) -> None:
        """TRIGger:ACQuire:IMMediate - trigger the armed acquisition now."""
        return self.execute("trigger_acquire")

    def set_acquire_trigger_source(self, source: str) -> str:
        """TRIGger:ACQuire:SOURce - BUS, CURRent1, VOLTage1, TRANsient1,
        EXTernal, EXPR1-8 or PIN1-7.

        CURRent1 and VOLTage1 trigger a measurement on the output crossing a
        level (see set_acquire_trigger_voltage/current and their slopes). Note
        IMMediate is NOT among them - use trigger_acquire() to fire one now."""
        return self.execute("set_acquire_trigger_source", value=source)

    def read_acquire_trigger_source(self) -> str:
        """TRIGger:ACQuire:SOURce?"""
        return self.execute("read_acquire_trigger_source")

    def set_acquire_trigger_voltage(self, volts: float) -> float:
        """TRIGger:ACQuire:VOLTage - the level that triggers an acquisition when
        the source is VOLTage1."""
        return self.execute("set_acquire_trigger_voltage", value=volts)

    def set_acquire_trigger_voltage_slope(self, slope: str) -> str:
        """TRIGger:ACQuire:VOLTage:SLOPe - POSitive or NEGative."""
        return self.execute("set_acquire_trigger_voltage_slope", value=slope)

    def set_acquire_trigger_current(self, amps: float) -> float:
        """TRIGger:ACQuire:CURRent - the level that triggers an acquisition when
        the source is CURRent1."""
        return self.execute("set_acquire_trigger_current", value=amps)

    def set_acquire_trigger_current_slope(self, slope: str) -> str:
        """TRIGger:ACQuire:CURRent:SLOPe - POSitive or NEGative."""
        return self.execute("set_acquire_trigger_current_slope", value=slope)

    def set_acquire_trigger_out(self, enabled: bool) -> bool:
        """TRIGger:ACQuire:TOUTput - send measurement triggers to a digital
        port pin."""
        return self.execute("set_acquire_trigger_out", value=enabled)

    def read_acquire_trigger_count(self) -> int:
        """TRIGger:ACQuire:INDices:COUNt? - how many triggers were captured
        during the acquisition."""
        return self.execute("read_acquire_trigger_count")

    def read_acquire_trigger_indices(self) -> str:
        """TRIGger:ACQuire:INDices:DATA? - where in the acquisition the triggers
        landed."""
        return self.execute("read_acquire_trigger_indices")

    def set_arb_trigger_source(self, source: str) -> str:
        """TRIGger:ARB:SOURce - accepted although Arb itself needs Option 303,
        which this unit does not have."""
        return self.execute("set_arb_trigger_source", value=source)

    def read_arb_trigger_source(self) -> str:
        """TRIGger:ARB:SOURce?"""
        return self.execute("read_arb_trigger_source")

    # --- status registers --------------------------------------------------

    def read_operation_events(self) -> int:
        """STATus:OPERation:EVENt? - read and CLEAR.

        Rarely what you want: the telemetry stream reads this every frame and
        publishes it decoded as `in_cv_event`, `output_off_event` and so on.
        Calling this consumes edges the next frame would otherwise report."""
        return self.execute("read_operation_events")

    def read_questionable_events(self) -> int:
        """STATus:QUEStionable:EVENt? - read and CLEAR; see
        read_operation_events for why you probably do not want it."""
        return self.execute("read_questionable_events")

    def read_questionable2_events(self) -> int:
        """STATus:QUEStionable2:EVENt? - read and CLEAR."""
        return self.execute("read_questionable2_events")

    def set_operation_enable(self, mask: int) -> int:
        """STATus:OPERation:ENABle - which operation bits reach the status
        byte."""
        return self.execute("set_operation_enable", value=mask)

    def set_operation_ptr(self, mask: int) -> int:
        """STATus:OPERation:PTRansition - which positive edges reach the event
        register. Preset to all bits, which is what lets the event channels
        catch a transition shorter than one frame."""
        return self.execute("set_operation_ptr", value=mask)

    def set_operation_ntr(self, mask: int) -> int:
        """STATus:OPERation:NTRansition - which negative edges reach the event
        register. Preset to none."""
        return self.execute("set_operation_ntr", value=mask)

    def set_questionable_enable(self, mask: int) -> int:
        """STATus:QUEStionable:ENABle"""
        return self.execute("set_questionable_enable", value=mask)

    def set_questionable_ptr(self, mask: int) -> int:
        """STATus:QUEStionable:PTRansition"""
        return self.execute("set_questionable_ptr", value=mask)

    def set_questionable_ntr(self, mask: int) -> int:
        """STATus:QUEStionable:NTRansition"""
        return self.execute("set_questionable_ntr", value=mask)

    def set_questionable2_enable(self, mask: int) -> int:
        """STATus:QUEStionable2:ENABle"""
        return self.execute("set_questionable2_enable", value=mask)

    def set_questionable2_ptr(self, mask: int) -> int:
        """STATus:QUEStionable2:PTRansition"""
        return self.execute("set_questionable2_ptr", value=mask)

    def set_questionable2_ntr(self, mask: int) -> int:
        """STATus:QUEStionable2:NTRansition"""
        return self.execute("set_questionable2_ntr", value=mask)

    def read_operation_enable(self) -> int:
        """STATus:OPERation:ENABle?"""
        return self.execute("read_operation_enable")

    def read_questionable_enable(self) -> int:
        """STATus:QUEStionable:ENABle?"""
        return self.execute("read_questionable_enable")

    def read_operation_ptr(self) -> int:
        """STATus:OPERation:PTRansition?"""
        return self.execute("read_operation_ptr")

    def read_questionable_ptr(self) -> int:
        """STATus:QUEStionable:PTRansition?"""
        return self.execute("read_questionable_ptr")

    def preset_status(self) -> Any:
        """STATus:PRESet - reset every Enable, PTR and NTR register.

        Safe for the event telemetry channels, and verified: the preset state
        leaves every PTR bit set (511, 16383 and 127 across the three registers)
        with NTR and Enable at zero. So it restores the edge-passing the
        `*_event` channels rely on rather than clearing it, and undoes a narrowed
        PTR mask rather than status itself."""
        return self.execute("preset_status")

    def set_operation_user_source(self, number: int, source: str) -> str:
        """STATus:OPERation:USER<n>:SOURce - drive the User1/User2 operation
        status bits from a signal expression."""
        return self.execute(f"set_operation_user_source_{number}", value=source)

    def read_operation_user_source(self, number: int) -> str:
        """STATus:OPERation:USER<n>:SOURce?"""
        return self.execute(f"read_operation_user_source_{number}")

    # --- errors, identity, system ------------------------------------------

    def read_error(self) -> str:
        """SYSTem:ERRor? - read and CLEAR one entry from this session's error
        queue.

        Usually reads `+0,"No error"`, because every write this driver sends
        carries its own error check and consumes the entry first. The queue
        belongs to this connection: measured, a second simultaneous socket client
        sees none of these entries, and neither does the front panel."""
        return self.execute("read_error")

    def drain_errors(self) -> List[str]:
        """Read this session's error queue until it is empty, returning every
        entry. Driver-side, not a single instrument command."""
        return self.execute("drain_errors")

    def read_identity(self) -> str:
        """*IDN? - manufacturer, model, serial number, firmware revision."""
        return self.execute("read_identity")

    def read_options(self) -> str:
        """*OPT? - installed options, or 0 for none."""
        return self.execute("read_options")

    def read_learn_string(self) -> str:
        """*LRN? - every settable value as a SCPI command string. A complete
        snapshot of the instrument's configuration in one query, useful for
        recording what a run started from."""
        return self.execute("read_learn_string")

    def read_scpi_version(self) -> str:
        """SYSTem:VERSion?"""
        return self.execute("read_scpi_version")

    def read_line_frequency(self) -> float:
        """SYSTem:LFRequency? - the detected AC line frequency, which sets how
        long an NPLC-based measurement takes."""
        return self.execute("read_line_frequency")

    def set_line_frequency_mode(self, mode: str) -> None:
        """SYSTem:LFRequency:MODE - AUTO, MAN50 or MAN60."""
        return self.execute("set_line_frequency_mode", value=mode)

    def read_calibration_date(self) -> str:
        """CALibrate:DATE? - when the instrument was last calibrated. The rest
        of the CALibrate subsystem is deliberately not exposed: it can degrade
        the instrument's accuracy, which is why the instrument itself guards it
        with a password."""
        return self.execute("read_calibration_date")

    def read_calibration_count(self) -> int:
        """CALibrate:COUNt? - how many times calibration has been saved."""
        return self.execute("read_calibration_count")

    def read_power_limit(self) -> float:
        """[SOURce:]POWer:LIMit? - the instrument's output power limit in watts,
        2000 W on this model. Voltage and current are each reachable to their
        full rating but not simultaneously; this is the product that bounds
        them."""
        return self.execute("read_power_limit")

    def read_data_format(self) -> str:
        """FORMat? - ASCii or REAL. This driver requires ASCii and checks it at
        connect; the setter is deliberately not exposed, because REAL would make
        every reply a binary block the line-oriented transport cannot read."""
        return self.execute("read_data_format")

    def read_byte_order(self) -> str:
        """FORMat:BORDer? - only meaningful for REAL format data."""
        return self.execute("read_byte_order")

    def read_ambient_temperature(self) -> float:
        """SYSTem:TEMPerature:AMBient? - degrees C at the air inlet. Also a
        telemetry channel."""
        return self.execute("read_ambient_temperature")

    def read_ratings(self) -> Dict[str, Any]:
        """The instrument's own limits, read at connect, plus the declared
        dissipator count and the sink ceiling it permits. Driver-side."""
        return self.execute("read_ratings")

    def read_control_socket_port(self) -> int:
        """SYSTem:COMMunicate:TCPip:CONTrol? - the port for a control socket,
        which is how device clear and service requests are sent. This driver
        uses only the data socket and needs neither."""
        return self.execute("read_control_socket_port")

    def set_remote_state(self, state: str) -> str:
        """SYSTem:COMMunicate:RLSTate - LOCal, REMote or RWLock. RWLock locks
        the front panel out, which stops an operator changing a setpoint
        mid-run; nothing in this driver sets it."""
        return self.execute("set_remote_state", value=state)

    def read_remote_state(self) -> str:
        """SYSTem:COMMunicate:RLSTate?"""
        return self.execute("read_remote_state")

    def set_display_state(self, enabled: bool) -> bool:
        """DISPlay - turn the front panel display on or off."""
        return self.execute("set_display_state", value=enabled)

    def set_display_view(self, view: str) -> str:
        """DISPlay:VIEW - METER_VI, METER_VP or METER_VIP."""
        return self.execute("set_display_view", value=view)

    def set_display_saver(self, enabled: bool) -> bool:
        """DISPlay:SAVer - the screen saver."""
        return self.execute("set_display_saver", value=enabled)

    def read_display_saver(self) -> bool:
        """DISPlay:SAVer?"""
        return self.execute("read_display_saver")

    def set_lxi_identify(self, enabled: bool) -> bool:
        """LXI:IDENtify:STATe - blink the front panel identify indicator, for
        finding which instrument in a rack this driver is talking to."""
        return self.execute("set_lxi_identify", value=enabled)

    def read_lxi_identify(self) -> bool:
        """LXI:IDENtify:STATe?"""
        return self.execute("read_lxi_identify")

    def set_date(self, date: str) -> None:
        """SYSTem:DATE - `<yyyy>,<mm>,<dd>`. Only used to timestamp the black
        box recorder, which this unit does not have."""
        return self.execute("set_date", value=date)

    def set_time(self, time: str) -> None:
        """SYSTem:TIME - `<hh>,<mm>,<ss>`."""
        return self.execute("set_time", value=time)

    def read_date(self) -> str:
        """SYSTem:DATE?"""
        return self.execute("read_date")

    def read_time(self) -> str:
        """SYSTem:TIME?"""
        return self.execute("read_time")

    def reboot(self) -> None:
        """SYSTem:REBoot - restart the instrument.

        Drops this link: the driver must reconnect afterwards, and the
        instrument takes about 30 seconds to become usable again."""
        return self.execute("reboot")

    # --- IEEE-488 common commands ------------------------------------------

    def clear_status(self) -> None:
        """*CLS - clear the status structure and this session's error queue."""
        return self.execute("clear_status")

    def reset(self) -> Any:
        """*RST - output off, and every setting back to its reset value:
        0.08 V, 0.255 A limit, -2.55 A negative limit, OVP 96 V, OCP off, slew
        at maximum, voltage priority, FIXed transient mode.

        Does not reset the error queue, the LAN configuration, the stored states,
        the accumulated amp-hours and watt-hours, or the power-on state
        selection. It DOES set FORMat back to ASCII."""
        return self.execute("reset")

    def save_state(self, slot: int) -> None:
        """*SAV - save the instrument state to non-volatile slot 0-9."""
        return self.execute("save_state", value=slot)

    def recall_state(self, slot: int) -> Any:
        """*RCL - recall the instrument state from slot 0-9.

        A store can hold any setpoint, and there is no way to know what it holds
        before recalling it, so the limits are checked *after* the fact: the
        values are already applied by the time an error is raised."""
        return self.execute("recall_state", value=slot)

    def trigger(self) -> None:
        """*TRG - a BUS trigger, for whichever system is armed with BUS as its
        source."""
        return self.execute("trigger")

    def wait_operation_complete(self) -> None:
        """*WAI - hold further command processing until pending operations
        finish."""
        return self.execute("wait_operation_complete")

    def operation_complete(self) -> None:
        """*OPC - set the operation-complete bit when pending operations
        finish."""
        return self.execute("operation_complete")

    def read_operation_complete(self) -> int:
        """*OPC? - returns 1 once all pending operations are complete."""
        return self.execute("read_operation_complete")

    def self_test(self) -> int:
        """*TST? - 0 means passed.

        Worth calling when a dissipator is expected: an N7909A that is cabled
        but not working, or that was disconnected while running, shows up as a
        self-test error."""
        return self.execute("self_test")

    def read_event_status(self) -> int:
        """*ESR? - read and CLEAR the standard event status register. Bits:
        7 power-on, 5 command error, 4 execution error, 3 device-specific error,
        2 query error, 0 operation complete."""
        return self.execute("read_event_status")

    def set_event_status_enable(self, mask: int) -> None:
        """*ESE"""
        return self.execute("set_event_status_enable", value=mask)

    def read_event_status_enable(self) -> int:
        """*ESE?"""
        return self.execute("read_event_status_enable")

    def set_service_request_enable(self, mask: int) -> None:
        """*SRE - which status byte bits request service. Only reaches anyone
        over a control socket, which this driver does not open."""
        return self.execute("set_service_request_enable", value=mask)

    def read_service_request_enable(self) -> int:
        """*SRE?"""
        return self.execute("read_service_request_enable")

    def read_status_byte(self) -> int:
        """*STB? - bit 2 is a non-empty error queue, bit 3 questionable, bit 4
        message available, bit 7 operation."""
        return self.execute("read_status_byte")

    # --- driver-side -------------------------------------------------------

    def clear_clamped_latch(self) -> Dict[str, Any]:
        """Reset the sticky `clamped_*` channels and return what they held.

        The latch records that a commanded voltage or current was applied at the
        hardware limit instead of the value asked for. Clear it between phases
        of a test to make "did anything get clamped *since here*" answerable."""
        return self.execute("clear_clamped_latch")

    # --- escape hatch ------------------------------------------------------

    def execute_raw(self, action: str, **params: Any) -> Any:
        """Call an action by its declared name, for anything the named methods
        above do not cover. Present so a caller is never forced back to the
        generic client to reach a declared channel."""
        return self.execute(action, **params)
