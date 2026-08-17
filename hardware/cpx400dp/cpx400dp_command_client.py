"""CPX400DP-specific convenience methods layered on the generic CommandClient -
one method per channel in cpx400dp_channels.py's COMMAND_CHANNELS. Each
docstring names the real TTi mnemonic the call sends, so it can be looked up in
the instrument's own manual for more detail than the one-liner gives.

The default timeout is larger than CommandClient's, because the `with verify`
command family blocks the instrument's parser for up to 5 seconds - the generic
client's 5000 ms default would expire just before such a command answered,
marking the client permanently broken for a command about to succeed.
"""
from __future__ import annotations

from typing import Any

from ..clients.command_client import CommandClient
from protocol.wire import DEFAULT_CPX400DP_COMMAND_ENDPOINT

DEFAULT_TIMEOUT_MS = 10_000
"""Must exceed the 5 s `with verify` block plus the round-trips of the error
check that follows it - see the module docstring."""


class Cpx400dpCommandClient(CommandClient):
    """CommandClient with named sugar for every declared CPX400DP command channel."""

    def __init__(
        self,
        endpoint: str = DEFAULT_CPX400DP_COMMAND_ENDPOINT,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ):
        super().__init__(endpoint, timeout_ms)

    # --- setpoints ---------------------------------------------------------

    def set_voltage(self, output: int, volts: float) -> None:
        """V<n> - set output <n>'s voltage setpoint, in volts."""
        self.execute(f"set_voltage_{output}", value=volts)

    def set_voltage_verify(self, output: int, volts: float) -> None:
        """V<n>V - set output <n>'s voltage and wait for the output to reach it.

        BLOCKS THE INSTRUMENT LINK FOR UP TO 5 SECONDS, stalling telemetry for
        that whole time, and sets the verify-timeout bit if the output does not
        settle (a large output capacitor will do this). Prefer set_voltage()
        and a Bound on the measured `voltage_<n>` channel: a Rulebook checks
        the same thing continuously, records it, and blocks nothing."""
        self.execute(f"set_voltage_verify_{output}", value=volts)

    def set_current(self, output: int, amps: float) -> None:
        """I<n> - set output <n>'s current limit, in amps. The output switches
        to constant-current regulation at this value."""
        self.execute(f"set_current_{output}", value=amps)

    def set_ovp(self, output: int, volts: float) -> None:
        """OVP<n> - set output <n>'s over-voltage protection trip threshold."""
        self.execute(f"set_ovp_{output}", value=volts)

    def set_ocp(self, output: int, amps: float) -> None:
        """OCP<n> - set output <n>'s over-current protection trip threshold."""
        self.execute(f"set_ocp_{output}", value=amps)

    def set_delta_voltage(self, output: int, volts: float) -> None:
        """DELTAV<n> - step size used by increment_voltage/decrement_voltage."""
        self.execute(f"set_delta_voltage_{output}", value=volts)

    def set_delta_current(self, output: int, amps: float) -> None:
        """DELTAI<n> - step size used by increment_current/decrement_current."""
        self.execute(f"set_delta_current_{output}", value=amps)

    # --- stepping ----------------------------------------------------------

    def increment_voltage(self, output: int) -> None:
        """INCV<n> - raise output <n>'s voltage by its delta_voltage step."""
        self.execute(f"increment_voltage_{output}")

    def increment_voltage_verify(self, output: int) -> None:
        """INCV<n>V - as increment_voltage, waiting for the output to reach it.
        BLOCKS UP TO 5 SECONDS - see set_voltage_verify()."""
        self.execute(f"increment_voltage_verify_{output}")

    def decrement_voltage(self, output: int) -> None:
        """DECV<n> - lower output <n>'s voltage by its delta_voltage step."""
        self.execute(f"decrement_voltage_{output}")

    def decrement_voltage_verify(self, output: int) -> None:
        """DECV<n>V - as decrement_voltage, waiting for the output to reach it.
        BLOCKS UP TO 5 SECONDS - see set_voltage_verify()."""
        self.execute(f"decrement_voltage_verify_{output}")

    def increment_current(self, output: int) -> None:
        """INCI<n> - raise output <n>'s current limit by its delta_current step."""
        self.execute(f"increment_current_{output}")

    def decrement_current(self, output: int) -> None:
        """DECI<n> - lower output <n>'s current limit by its delta_current step."""
        self.execute(f"decrement_current_{output}")

    # --- output control ----------------------------------------------------

    def enable_output(self, output: int, enabled: bool) -> None:
        """OP<n> - switch output <n> on or off.

        Two caveats. The output ramps rather than stepping, so a readback taken
        immediately after enabling is short of the setpoint. And switching off
        does not mean zero volts: the terminals decay through the output
        capacitance."""
        self.execute(f"enable_output_{output}", value=1 if enabled else 0)

    def enable_all_outputs(self, enabled: bool) -> None:
        """OPALL - switch both outputs together. Outputs already in the
        requested state stay as they are."""
        self.execute("enable_all_outputs", value=1 if enabled else 0)

    def trip_reset(self) -> None:
        """TRIPRST - documented as "attempt to clear all trip conditions", and on
        this instrument it clears nothing.

        What does clear a trip differs by trip. An OVP trip clears when `set_ovp()`
        is raised back above the voltage setpoint, with no TRIPRST involved. An OCP
        trip clears only on an explicit `enable_output(n, False)` - even though the
        trip has already switched the output off - and ignores both a raised OCP
        level and TRIPRST.

        A recovery step should therefore remove the cause, explicitly command the
        output off, and re-enable. Do not write a step that calls this and waits
        for the trip to clear; it will wait forever."""
        self.execute("trip_reset")

    def set_config_mode(self, mode: int) -> None:
        """CONFIG - 2 = outputs independent, 0 = output 2 tracks output 1.
        Fails with EER 104 unless output 2 is off first."""
        self.execute("set_config_mode", value=mode)

    def set_tracking_ratio(self, percent: float) -> None:
        """RATIO - output 2 as a percentage (0-100) of output 1. Settable any
        time, but only takes effect in voltage-tracking mode (CONFIG 0)."""
        self.execute("set_tracking_ratio", value=percent)

    # --- setup stores ------------------------------------------------------

    def save_setup(self, output: int, store: int) -> None:
        """SAV<n> - save output <n>'s current setup to store 0-9."""
        self.execute(f"save_setup_{output}", value=store)

    def recall_setup(self, output: int, store: int) -> None:
        """RCL<n> - recall output <n>'s setup from store 0-9.

        A store can hold any setpoint, so if this backend was given a
        max_voltage/max_current ceiling the recalled values are checked against
        it *after* the fact - they are already applied by the time an error is
        raised."""
        self.execute(f"recall_setup_{output}", value=store)

    # --- limit status ------------------------------------------------------

    def set_limit_status_enable(self, output: int, mask: int) -> None:
        """LSE<n> - which limit bits raise LIM<n> in the Status Byte Register."""
        self.execute(f"set_limit_status_enable_{output}", value=mask)

    def read_limit_status(self, output: int) -> int:
        """LSR<n>? - read and CLEAR output <n>'s limit status register.

        Rarely what you want: the telemetry stream already polls this every
        frame and publishes it decoded as `in_cv_<n>`, `tripped_oc_<n>` and so
        on. Calling this consumes bits the next frame would otherwise have
        reported."""
        return self.execute(f"read_limit_status_{output}")

    def clear_limit_status_latch(self, output: int) -> int:
        """Driver-side, not an instrument command: reset the sticky
        `limit_status_latched_<n>` channel and return what it held.

        The latch accumulates every limit bit ever seen, so a trip shorter than
        one frame period still appears. Clear it between phases of a test to
        make "did anything trip *since here*" answerable."""
        return self.execute(f"clear_limit_status_latch_{output}")

    # --- interface control -------------------------------------------------

    def go_local(self) -> None:
        """LOCAL - return the instrument to front-panel control. Does NOT
        release an interface lock."""
        self.execute("go_local")

    def interface_lock(self) -> int:
        """IFLOCK - request exclusive control. Returns 1 if acquired, -1 if
        unavailable. The backend can also take this at connect - see its
        take_interface_lock argument."""
        return self.execute("interface_lock")

    def interface_unlock(self) -> int:
        """IFUNLOCK - release the lock. Returns 0 if released, -1 if this
        interface has no authority to release it (which also puts 200 in the
        execution error register)."""
        return self.execute("interface_unlock")

    def read_interface_lock(self) -> int:
        """IFLOCK? - 1 = held by this interface, 0 = no lock, -1 = held
        elsewhere or this interface is barred from taking control."""
        return self.execute("read_interface_lock")

    # --- status and error registers ----------------------------------------

    def clear_status(self) -> None:
        """*CLS - clear the status structure."""
        self.execute("clear_status")

    def read_execution_error(self) -> int:
        """EER? - read and CLEAR the execution error register.

        Usually reads 0, because the backend checks this itself after every
        command it sends and raises on a non-zero code, consuming it first."""
        return self.execute("read_execution_error")

    def read_query_error(self) -> int:
        """QER? - read and CLEAR the query error register."""
        return self.execute("read_query_error")

    def read_event_status(self) -> int:
        """*ESR? - read and CLEAR the Standard Event Status Register. Bits:
        7 power-on, 5 command error, 4 execution error, 3 verify timeout,
        2 query error, 0 operation complete. Also normally consumed by the
        backend's own post-write check."""
        return self.execute("read_event_status")

    def set_event_status_enable(self, mask: int) -> None:
        """*ESE - set the Standard Event Status Enable Register."""
        self.execute("set_event_status_enable", value=mask)

    def read_event_status_enable(self) -> int:
        """*ESE? - read the Standard Event Status Enable Register."""
        return self.execute("read_event_status_enable")

    def set_service_request_enable(self, mask: int) -> None:
        """*SRE - set the Service Request Enable Register. A GPIB artifact; of
        little use over ethernet."""
        self.execute("set_service_request_enable", value=mask)

    def read_service_request_enable(self) -> int:
        """*SRE? - read the Service Request Enable Register. GPIB artifact."""
        return self.execute("read_service_request_enable")

    def read_status_byte(self) -> int:
        """*STB? - read the Status Byte Register. GPIB artifact."""
        return self.execute("read_status_byte")

    def set_parallel_poll_enable(self, mask: int) -> None:
        """*PRE - set the Parallel Poll Enable Register. Meaningless over TCP."""
        self.execute("set_parallel_poll_enable", value=mask)

    def read_parallel_poll_enable(self) -> int:
        """*PRE? - read the Parallel Poll Enable Register. GPIB artifact."""
        return self.execute("read_parallel_poll_enable")

    def read_individual_status(self) -> int:
        """*IST? - the ist local message defined by IEEE 488.2. GPIB artifact."""
        return self.execute("read_individual_status")

    # --- synchronisation ---------------------------------------------------

    def operation_complete(self) -> None:
        """*OPC - set the operation-complete bit in the event status register."""
        self.execute("operation_complete")

    def read_operation_complete(self) -> int:
        """*OPC? - always answers 1: every command on this instrument is
        sequential, so nothing is ever still in flight."""
        return self.execute("read_operation_complete")

    def wait_operation_complete(self) -> None:
        """*WAI - documented as taking no additional action here, for the same
        reason."""
        self.execute("wait_operation_complete")

    # --- miscellaneous -----------------------------------------------------

    def reset(self) -> None:
        """*RST - back to remote defaults: 1 V, 1 A, 10 mV and 10 mA steps,
        OVP 66 V, OCP 22 A, interface lock and voltage tracking cancelled.
        Remote interface settings, stored setups and the power-on output state
        are left alone."""
        self.execute("reset")

    def trigger(self) -> None:
        """*TRG - accepted and does nothing; the supply has no trigger."""
        self.execute("trigger")

    def self_test(self) -> int:
        """*TST? - always answers 0; the supply has no self-test capability."""
        return self.execute("self_test")

    def read_identity(self) -> str:
        """*IDN? - manufacturer, model, serial number, firmware revision."""
        return self.execute("read_identity")

    def read_bus_address(self) -> int:
        """ADDRESS? - the GPIB bus address, usable as a general identifier."""
        return self.execute("read_bus_address")

    # --- escape hatch ------------------------------------------------------

    def execute_raw(self, action: str, **params: Any) -> Any:
        """Call an action by its declared name, for anything the named methods
        above do not cover. Present so a caller is never forced back to the
        generic client to reach a declared channel."""
        return self.execute(action, **params)
