"""Physical testbed for ydrive: the ODrive driver process and a CPX400DP bench
supply, with connected command/telemetry clients for both. There is no separate
DUT layer - the ODrive is the entire actuator and sensor interface.

The supply feeds two rails: a 48 V motor bus on output 2 and a 24 V brake on
output 1 (see MOTOR_BUS/BRAKE_BUS below).

THE BRAKE IS MAGNET-APPLIED, AND SO FAIL-SAFE. A permanent magnet supplies the
holding force, so the brake is engaged whenever its rail is unpowered; powering
output 1 cancels that field and releases it. Losing the rail therefore holds the
load rather than dropping it. power_brake_bus() is the only place that polarity
is
asserted - and it moves the rail alone. Pairing the rail with the axis state, so
the motor never drives against an engaged brake and the brake never lets go of a
load the controller has not taken, is engage_brake()/release_brake() in
testcases/ydrive/teststeps/teststeps.py.

What this does NOT do: energize anything in start(). Rail setpoints are
configured with both outputs off, and powering the stand is a test's decision,
taken in PreTestSetup. It also does not configure the ODrive, including
`board_config_dc_bus_overvoltage_trip_level`, which is persistent device state.

use_mock substitutes the ODrive only. The supply's driver has no mock backend, so
a reachable CPX400DP is always needed.

Test steps use testbed.command/testbed.telemetry directly, and the named
per-channel methods (get_pos_estimate(), ...) for a synchronous point-read -
those use a separate sync_telemetry client so they do not contend with whatever
else is consuming .telemetry, such as LiveRulebookRunner. Every client accessor
raises RuntimeError before start().
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from hardware.clients.telemetry_client import TelemetryClient
from hardware.cpx400dp.cpx400dp_channels import (
    COMMAND_CHANNELS as CPX400DP_COMMAND_CHANNELS,
    TELEMETRY_CHANNELS as CPX400DP_TELEMETRY_CHANNELS,
)
from hardware.cpx400dp.cpx400dp_command_client import Cpx400dpCommandClient
from hardware.cpx400dp.rails import Rail, deliverable_current_a
from hardware.driver_process import start_driver
from hardware.odrive.odrive_channels import COMMAND_CHANNELS, TELEMETRY_CHANNELS
from hardware.odrive.odrive_command_client import OdriveCommandClient
from hardware.tc_daq.tc_daq_channels import TELEMETRY_CHANNELS as TC_DAQ_TELEMETRY_CHANNELS
from hardware.tc_daq.transport import (
    DEFAULT_PORT as DEFAULT_TC_DAQ_PORT,
    SILENCE_TIMEOUT_S as TC_DAQ_SILENCE_TIMEOUT_S,
)
from protocol.paths import driver_log_path
from protocol.wire import (
    DEFAULT_CPX400DP_COMMAND_ENDPOINT,
    DEFAULT_CPX400DP_TELEMETRY_ENDPOINT,
    DEFAULT_ODRIVE_COMMAND_ENDPOINT,
    DEFAULT_ODRIVE_TELEMETRY_ENDPOINT,
    DEFAULT_TC_DAQ_TELEMETRY_ENDPOINT,
    DEVICE_CPX400DP,
    DEVICE_ODRIVE,
    DEVICE_TC_DAQ,
)

logger = logging.getLogger(__name__)

CPX400DP_HOST = "169.254.166.16"
"""This stand's supply, serial 595016.

Not a stable address: the instrument reports DHCP, but its segment has no DHCP
server, so it self-assigns a link-local address that changes if a DHCP server
appears or on an address collision. Nothing announces the move, and `connect()`
checks the model rather than the serial, so a moved address is found with
`python -m tools.find_cpx400dp`, which reports the identity of whatever answers.
Pass a new one as YdriveTestbed(cpx400dp_host=...)."""

CPX400DP_MDNS_HOST = "t595016.local"
"""The same supply by mDNS name: it advertises itself as `t<serial>.local`, which
follows it when its address changes. Needs an mDNS responder on the host - macOS
has one built in, a Windows or CentOS stand may not. Pass either this or
CPX400DP_HOST as YdriveTestbed(cpx400dp_host=...)."""

TC_DAQ_STALENESS_S = TC_DAQ_SILENCE_TIMEOUT_S + 2.0
"""How long this stand's consumers wait for a thermocouple frame before treating
the stream as dead.

Derived from the driver's own tolerance rather than set alongside it, and
deliberately longer. Both ends are watching the same silence: at the same value
they race, and the client wins by reporting only that no frame arrived - while
the driver reports which port went quiet and for how long. Two seconds of margin
means the useful diagnosis is the one that lands. TelemetryClient's 5 s default
would fire first and make the driver's extra patience unreachable."""

STARTUP_DELAY_S = 1.0
"""Seconds allowed for both drivers to bind their sockets and connect. The
supply's connect() checks identity, clears its error registers, probes all 27
declared queries and reads its cached tier before it serves."""

MOTOR_BUS = Rail(name="ydrive motor bus", output=2, voltage_v=48.0, current_limit_a=16.0)
"""The ODrive's DC bus, on output 2.

The 16 A limit is above what the supply can deliver at 48 V - its 420 W envelope
caps this output at 8.75 A - so it does not act as a current limit. An overdraw
makes the output go unregulated and the bus voltage sag; `in_power_limit_2` is
the channel that reports it, not `current_2`. For the same reason
`ydrive_rulebook`'s fatal `board_ibus` > 30 A bound cannot fire on this rail."""

BRAKE_BUS = Rail(name="ydrive brake", output=1, voltage_v=24.0, current_limit_a=5.0)
"""The ydrive brake, on output 1.

Magnet-applied and fail-safe: the brake is engaged with this rail unpowered, and
powering it RELEASES the brake. 120 W is inside the envelope, so this rail does
get real current
limiting."""

RAILS = (BRAKE_BUS, MOTOR_BUS)
"""Both rails, ordered by output number. start() iterates this to configure
setpoints. It is not the teardown order - see stop()."""

BRAKE_SETTLE_S = 0.25
"""Seconds to wait after switching the brake rail, before moving or dwelling.

A placeholder to be replaced with the brake's datasheet figure. A brake is not
instantaneous: the coil field has to collapse before a magnet-applied brake
grabs, and build before it lets go, and an output's terminal voltage decays
through its capacitance rather than dropping.

The risk is asymmetric. Too short before a dwell means dwelling briefly
unbraked. Too short before a move means driving the axis into a brake that has
not let go."""


class YdriveTestbed:
    """Starts/stops the ODrive and CPX400DP driver processes for ydrive, and owns connected clients for both.

    Use as a context manager:

        with YdriveTestbed() as testbed:
            testbed.power_motor_bus(True)
            testbed.command.set_control_mode("POSITION_CONTROL")
            ...  # both drivers are up; arming and the brake are a test's to sequence
    """

    DEVICES: Tuple[str, ...] = (DEVICE_ODRIVE, DEVICE_CPX400DP, DEVICE_TC_DAQ)
    """The devices whose driver processes this testbed owns. Declared here
    because this is what starts them; the test case unions this with its DUT
    façade's declaration (there is none for ydrive) and publishes the result, so
    the telemetry engine knows whose frames belong to the run. See
    testcases/base.py's DEVICES."""

    def __init__(
        self,
        use_mock: bool = False,
        serial_number: Optional[str] = None,
        cpx400dp_host: str = CPX400DP_HOST,
        tc_daq_port: str = DEFAULT_TC_DAQ_PORT,
        output_dir: Optional[Path] = None,
        test_id: Optional[str] = None,
    ) -> None:
        """
        cpx400dp_host: this stand's supply. Defaults to CPX400DP_HOST, the
            address this stand's unit last self-assigned; pass a new address, or
            CPX400DP_MDNS_HOST, when it has moved.

        tc_daq_port: the serial port the thermocouple DAQ is on. Named by the
            OS rather than by the device - `COM<n>` on Windows,
            `/dev/cu.usbserial-<n>` on macOS - and it changes with enumeration
            order, so this stand's own value belongs here rather than in the
            driver. `python -m hardware.tc_daq.main --list-ports` prints the
            candidates.

        output_dir/test_id: given both, each driver writes its detailed log to
            `<output_dir>/runs/<test_id>/<device>/logs.txt`, beside the telemetry
            it produced - which is where a decoded ODrive fault goes (see
            hardware/odrive/odrive_errors.py). A test case passes
            `self._output_dir` and `self.test_id` from PreTestSetup. Omitted, the
            drivers log to their consoles, which for a subprocess is nowhere
            anybody reads.
        """
        self._use_mock = use_mock
        self._serial_number = serial_number
        self._cpx400dp_host = cpx400dp_host
        self._tc_daq_port = tc_daq_port
        self._output_dir = output_dir
        self._test_id = test_id
        self._processes: List[subprocess.Popen] = []
        self._command: Optional[OdriveCommandClient] = None
        self._telemetry: Optional[TelemetryClient] = None
        self._sync_telemetry: Optional[TelemetryClient] = None
        self._supply: Optional[Cpx400dpCommandClient] = None
        self._supply_telemetry: Optional[TelemetryClient] = None
        self._tc_daq_telemetry: Optional[TelemetryClient] = None

    def _log_args(self, device: str) -> List[str]:
        """`--log-file` for one device's driver, or nothing if this testbed
        wasn't told which run it belongs to."""
        if self._output_dir is None or self._test_id is None:
            return []
        return ["--log-file", str(driver_log_path(self._output_dir, self._test_id, device))]

    def start(self) -> None:
        """Bring all three drivers up, verify their channel surfaces, and
        configure both rails' setpoints - with the outputs left OFF.

        Energizing is the test's decision, taken in PreTestSetup, not something
        that happens because a testbed was constructed."""
        odrive_args = [sys.executable, "-m", "hardware.odrive.main", *self._log_args(DEVICE_ODRIVE)]
        if self._use_mock:
            odrive_args.append("--mock")
        elif self._serial_number is not None:
            odrive_args += ["--serial-number", self._serial_number]

        # The driver's ceiling is per-backend rather than per-output, so the only
        # values it can carry are the maxima across both rails. It is a coarse
        # backstop against a gross typo, not per-rail protection: to the driver,
        # 48 V on the 24 V brake rail is a legal command. check_rails() detects
        # that after the fact.
        supply_args = [
            sys.executable, "-m", "hardware.cpx400dp.main",
            "--host", self._cpx400dp_host,
            "--max-voltage", str(max(rail.voltage_v for rail in RAILS)),
            "--max-current", str(max(rail.current_limit_a for rail in RAILS)),
            *self._log_args(DEVICE_CPX400DP),
        ]

        # The thermocouple DAQ takes no commands at all, so it gets no command
        # client below - nothing would be sendable through one. Its driver is
        # started, its stream is verified, and that is the whole interface.
        tc_daq_args = [
            sys.executable, "-m", "hardware.tc_daq.main",
            "--port", self._tc_daq_port,
            *self._log_args(DEVICE_TC_DAQ),
        ]

        self._processes = [
            start_driver(odrive_args), start_driver(supply_args), start_driver(tc_daq_args)
        ]
        time.sleep(STARTUP_DELAY_S)  # let the drivers bind their sockets

        self._command = OdriveCommandClient(endpoint=DEFAULT_ODRIVE_COMMAND_ENDPOINT)
        self._telemetry = TelemetryClient(endpoint=DEFAULT_ODRIVE_TELEMETRY_ENDPOINT)
        self._sync_telemetry = TelemetryClient(endpoint=DEFAULT_ODRIVE_TELEMETRY_ENDPOINT)
        self._supply = Cpx400dpCommandClient(endpoint=DEFAULT_CPX400DP_COMMAND_ENDPOINT)
        self._supply_telemetry = TelemetryClient(endpoint=DEFAULT_CPX400DP_TELEMETRY_ENDPOINT)
        self._tc_daq_telemetry = TelemetryClient(
            endpoint=DEFAULT_TC_DAQ_TELEMETRY_ENDPOINT, timeout_s=TC_DAQ_STALENESS_S
        )

        self._command.connect_backend()
        self._supply.connect_backend()

        self._command.verify_actions(COMMAND_CHANNELS)
        self._telemetry.verify_channels(TELEMETRY_CHANNELS)
        self._supply.verify_actions(CPX400DP_COMMAND_CHANNELS)
        self._supply_telemetry.verify_channels(CPX400DP_TELEMETRY_CHANNELS)
        # No verify_actions for the DAQ: it declares no commands, so there is
        # nothing to confirm. Its stream is the only thing to check, and a
        # faulted thermocouple still publishes its channel (as None), so this
        # passes with sensors unplugged - what it catches is a driver that
        # started against the wrong port and is streaming something else.
        self._tc_daq_telemetry.verify_channels(TC_DAQ_TELEMETRY_CHANNELS)

        self._configure_rails()

    def _configure_rails(self) -> None:
        """Switch both outputs off, then set every rail's voltage and current
        setpoints.

        A run starts from a de-energized stand whatever it finds. The supply's
        driver adopts, rather than resets, the output state it is started into,
        so a rail can still be live from a previous run - and writing a setpoint
        to a live output would step that rail under whatever is connected to it.
        Switching off first makes every setpoint below land on a dead rail.

        Powering anything back up is then a test's own decision, taken in
        PreTestSetup or by prepare_for_operation()."""
        for rail in RAILS:
            self.supply.enable_output(rail.output, False)
        logger.info("both outputs off - configuring setpoints on de-energized rails")

        for rail in RAILS:
            if not rail.is_within_envelope:
                logger.warning(
                    "%s: the configured %.1f A limit is above what this supply can deliver at "
                    "%.1f V (%.2f A, from the 420 W envelope), so it will NOT act as a current "
                    "limit. If the load draws more, the output goes unregulated and the rail "
                    "voltage sags instead - watch in_power_limit_%d, not current_%d.",
                    rail.name, rail.current_limit_a, rail.voltage_v,
                    deliverable_current_a(rail.voltage_v), rail.output, rail.output,
                )
            self.supply.set_voltage(rail.output, rail.voltage_v)
            self.supply.set_current(rail.output, rail.current_limit_a)
            logger.info(
                "%s configured on output %d: %.1f V, %.1f A limit (%.0f W)",
                rail.name, rail.output, rail.voltage_v, rail.current_limit_a, rail.power_w,
            )
        self.check_rails()

    SETPOINT_TOLERANCE = 0.02
    """How far a read-back setpoint may sit from its configured value before
    check_rails() calls it wrong. The instrument reports voltage setpoints to
    10 mV and current to 1 mA, so this is a rounding allowance, not a band."""

    def check_rails(self) -> None:
        """Confirm both rails still hold their configured setpoints, raising if
        not.

        Called at the end of start(), because the supply accepts and then
        silently discards a value it dislikes - a write is not evidence of a
        setpoint. Also worth calling from a test wherever a rail's integrity
        matters: the driver's ceiling is per-backend, so it cannot stop 48 V
        being commanded onto the 24 V brake rail, and a test holds the same
        supply client this testbed does."""
        channels = self.get_supply_channels()
        wrong = []
        for rail in RAILS:
            for quantity, expected, channel in (
                ("voltage", rail.voltage_v, f"setpoint_voltage_{rail.output}"),
                ("current limit", rail.current_limit_a, f"setpoint_current_{rail.output}"),
            ):
                actual = channels[channel]
                if abs(float(actual) - expected) > self.SETPOINT_TOLERANCE:
                    wrong.append(f"{rail.name} {quantity}: expected {expected}, instrument holds {actual}")
        if wrong:
            raise RuntimeError(
                "the supply's rail setpoints do not match this stand's configuration:\n  "
                + "\n  ".join(wrong)
                + "\nSomething commanded a setpoint outside this testbed, or a write was refused. "
                "See MOTOR_BUS/BRAKE_BUS in this module for what the rails should be."
            )

    def stop(self) -> None:
        """Tear the stand down in a safe order, and finish even if a step fails.

        The order matters. The brake is magnet-applied, so dropping its rail
        first makes the brake grab and hold the load before anything else
        changes; only then is the axis disarmed and the motor bus removed. The
        reverse order would leave the load unheld while the drive shuts down, and
        a de-energized stand is one whose brake is holding.

        Each step runs independently, logging a failure rather than raising, so
        one wedged client cannot leave a 48 V bus energized."""
        # A plain sleep rather than TestCase.wait_for(): teardown has no test
        # case to poll, and nothing it could usefully abort for.
        self._safe("engage the brake (drop the 24 V rail)", self._engage_brake_for_teardown)
        self._safe("disarm the ODrive axis", lambda: self.command.set_axis_state("IDLE"))
        self._safe("drop the 48 V motor bus", lambda: self.power_motor_bus(False))
        self._safe("confirm both rails are off", self._confirm_rails_off)

        self._safe("disconnect the ODrive backend", lambda: self.command.disconnect_backend())
        self._safe("disconnect the supply backend", lambda: self.supply.disconnect_backend())

        for client in (self._command, self._telemetry, self._sync_telemetry,
                       self._supply, self._supply_telemetry, self._tc_daq_telemetry):
            if client is not None:
                self._safe(f"close {type(client).__name__}", client.close)
        self._command = self._telemetry = self._sync_telemetry = None
        self._supply = self._supply_telemetry = self._tc_daq_telemetry = None

        for process in self._processes:
            self._safe(f"terminate pid {process.pid}", process.terminate)
        for process in self._processes:
            self._safe(f"reap pid {process.pid}", lambda p=process: p.wait(timeout=5))
        self._processes = []

    def _confirm_rails_off(self) -> None:
        """Read both outputs back after switching them off, and say so at ERROR
        if either is still on.

        Every step of stop() logs its failure and continues, which is what keeps
        one wedged client from stranding the rest of the sequence - but it also
        means a rail that never actually switched off leaves the stand
        energized with nothing stating it plainly. This is the one place that
        reads the outcome rather than the command.

        The queued frames are dropped first: the newest one already published
        can still predate the switch-off by a frame, and reporting a stand as
        energized when it isn't is the one thing this must not do."""
        self.supply_telemetry.discard_backlog()
        channels = self.get_supply_channels()
        still_on = [rail.name for rail in RAILS if channels.get(f"output_enabled_{rail.output}")]
        if still_on:
            logger.error(
                "TEARDOWN LEFT THE STAND ENERGIZED: %s still on - switch the supply off by hand",
                ", ".join(still_on),
            )

    def _engage_brake_for_teardown(self) -> None:
        """Drop the brake rail and give the brake time to grab, before the axis is
        disarmed and the bus removed."""
        self.power_brake_bus(False)
        time.sleep(BRAKE_SETTLE_S)

    @staticmethod
    def _safe(what: str, action: Callable[[], object]) -> None:
        """Run a teardown step, logging rather than raising on failure."""
        try:
            action()
        except Exception as exc:  # teardown must continue regardless
            logger.error("teardown step failed, continuing: %s: %r", what, exc)

    # --- power ------------------------------------------------------------

    def power_motor_bus(self, enabled: bool) -> None:
        """Switch the 48 V motor bus (output 2) on or off.

        The output ramps rather than stepping, so a check immediately after
        enabling reads low; and switching off does not mean zero volts, since the
        output capacitance takes a moment to discharge."""
        self.supply.enable_output(MOTOR_BUS.output, enabled)
        logger.info("%s %s", MOTOR_BUS.name, "energized" if enabled else "de-energized")

    def power_brake_bus(self, enabled: bool) -> None:
        """Switch the 24 V brake rail (output 1) on or off.

        Powering RELEASES the brake; removing power lets it grab. This only moves
        the rail. It does NOT wait for the brake to act on it, and does NOT touch
        the axis state - so calling it directly can leave the controller driving
        against an engaged brake, or the brake letting go of a load nothing is
        holding. The sequenced versions that cannot do either are
        engage_brake()/release_brake() in
        testcases/ydrive/teststeps/teststeps.py, which is what a test should
        use."""
        self.supply.enable_output(BRAKE_BUS.output, enabled)
        logger.info("%s %s", BRAKE_BUS.name, "released (rail energized)" if enabled else "engaged (rail de-energized)")

    def get_supply_channels(self) -> Dict[str, object]:
        """Block for the next supply telemetry frame and return its channels.

        The measured voltage and current are re-read at 5 Hz and held between
        reads, since the instrument's meters refresh at 4 Hz, so consecutive
        frames can carry the same reading."""
        return self.supply_telemetry.latest_frame().channels


    @property
    def supply(self) -> Cpx400dpCommandClient:
        if self._supply is None:
            raise RuntimeError("YdriveTestbed.supply accessed before start()")
        return self._supply

    @property
    def supply_telemetry(self) -> TelemetryClient:
        if self._supply_telemetry is None:
            raise RuntimeError("YdriveTestbed.supply_telemetry accessed before start()")
        return self._supply_telemetry

    @property
    def tc_daq_telemetry(self) -> TelemetryClient:
        """The thermocouple DAQ's stream.

        The only interface this device has - it accepts no commands, so there is
        no command client to pair with it."""
        if self._tc_daq_telemetry is None:
            raise RuntimeError("YdriveTestbed.tc_daq_telemetry accessed before start()")
        return self._tc_daq_telemetry

    @property
    def command(self) -> OdriveCommandClient:
        if self._command is None:
            raise RuntimeError("YdriveTestbed.command accessed before start()")
        return self._command

    @property
    def telemetry(self) -> TelemetryClient:
        if self._telemetry is None:
            raise RuntimeError("YdriveTestbed.telemetry accessed before start()")
        return self._telemetry

    @property
    def sync_telemetry(self) -> TelemetryClient:
        if self._sync_telemetry is None:
            raise RuntimeError("YdriveTestbed.sync_telemetry accessed before start()")
        return self._sync_telemetry

    def get_channels(self) -> Dict[str, object]:
        """The newest ODrive frame's full channels dict, blocking if none has
        arrived yet.

        Use this whenever more than one channel is needed from the same instant -
        reading two named accessors gives two values from two different frames,
        and costs two frame periods."""
        return self.sync_telemetry.latest_frame().channels

    def get_pos_estimate(self) -> float:
        return self.get_channels()["pos_estimate"]

    def get_axis_armed_status(self) -> bool:
        """Whether the axis is actively controlling the motor (`axis_is_armed`).

        Requesting an axis state only writes `requested_state`; the ODrive acts on
        it asynchronously and can decline. This is the reading that says whether
        it took."""
        return bool(self.get_channels()["axis_is_armed"])

    def get_vel_estimate(self) -> float:
        return self.get_channels()["vel_estimate"]

    def __enter__(self) -> "YdriveTestbed":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
