"""Physical testbed for zdrive: the ODrive driver process and a CPX400DP bench
supply, with connected command/telemetry clients for both.

The supply feeds two rails: a 48 V motor bus on output 2 and a 24 V brake on
output 1 (see MOTOR_BUS/BRAKE_BUS below).

    with ZdriveTestbed() as testbed:
        testbed.power_motor_bus(True)
        testbed.command.set_axis_state("CLOSED_LOOP_CONTROL")
        testbed.power_brake_bus(True)   # only after the controller is holding

THE BRAKE IS MAGNET-APPLIED, AND SO FAIL-SAFE. A permanent magnet supplies the
holding force, so the brake is engaged whenever its rail is unpowered; powering
output 1 cancels that field and releases it. Losing the rail therefore holds the
load rather than dropping it. power_brake_bus() is the only place that polarity
is
asserted, and it moves the rail alone - pairing it with the axis state belongs in
a test step.

What this does NOT do: energize anything in start(). Rail setpoints are
configured with both outputs off, and powering the stand is a test's decision,
taken in PreTestSetup. It also does not configure the ODrive, including
`board_config_dc_bus_overvoltage_trip_level`, which is persistent device state
and matters on a 48 V bus.
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
from hardware.odrive.odrive_channels import (
    COMMAND_CHANNELS as ODRIVE_COMMAND_CHANNELS,
    TELEMETRY_CHANNELS as ODRIVE_TELEMETRY_CHANNELS,
)
from hardware.odrive.odrive_command_client import OdriveCommandClient
from protocol.paths import driver_log_path
from protocol.wire import (
    DEFAULT_CPX400DP_COMMAND_ENDPOINT,
    DEFAULT_CPX400DP_TELEMETRY_ENDPOINT,
    DEFAULT_ODRIVE_COMMAND_ENDPOINT,
    DEFAULT_ODRIVE_TELEMETRY_ENDPOINT,
    DEVICE_CPX400DP,
    DEVICE_ODRIVE,
)

from hardware.cpx400dp.rails import Rail, deliverable_current_a
from hardware.odrive.odrive_channels import (
    COMMAND_CHANNELS as ODRIVE_COMMAND_CHANNELS,
    TELEMETRY_CHANNELS as ODRIVE_TELEMETRY_CHANNELS,
)
from hardware.odrive.odrive_command_client import OdriveCommandClient
from protocol.paths import driver_log_path
from protocol.wire import (
    DEFAULT_CPX400DP_COMMAND_ENDPOINT,
    DEFAULT_CPX400DP_TELEMETRY_ENDPOINT,
    DEFAULT_ODRIVE_COMMAND_ENDPOINT,
    DEFAULT_ODRIVE_TELEMETRY_ENDPOINT,
    DEVICE_CPX400DP,
    DEVICE_ODRIVE,
)

logger = logging.getLogger(__name__)

CPX400DP_HOST = "169.254.229.133"
"""This stand's supply.

Not a stable address: the instrument reports DHCP, but its segment has no DHCP
server, so it self-assigns a link-local address that changes if a DHCP server
appears or on an address collision. `t599542.local` is the same instrument by
mDNS name and follows it when the address moves, at the cost of needing an mDNS
responder on the host. Pass either as ZdriveTestbed(cpx400dp_host=...)."""

CPX400DP_MDNS_HOST = "t599542.local"
"""The same supply by mDNS name: it advertises itself as `t<serial>.local`, which
follows it when its address changes. Needs an mDNS responder on the host - macOS
has one built in, a Windows or CentOS stand may not. Pass either this or
CPX400DP_HOST as ZdriveTestbed(cpx400dp_host=...)."""

STARTUP_DELAY_S = 1.0
"""Seconds allowed for both drivers to bind their sockets and connect. The
supply's connect() checks identity, clears its error registers, probes all 27
declared queries and reads its cached tier before it serves."""

MOTOR_BUS = Rail(name="zdrive motor bus", output=2, voltage_v=48.0, current_limit_a=16.0)
"""The ODrive's DC bus, on output 2.

The 16 A limit is above what the supply can deliver at 48 V - its 420 W envelope
caps this output at 8.75 A - so it does not act as a current limit. An overdraw
makes the output go unregulated and the bus voltage sag; `in_power_limit_2` is
the channel that reports it, not `current_2`. Lower the limit to 8.5 A or below
for real current limiting on this rail."""

BRAKE_BUS = Rail(name="zdrive brake", output=1, voltage_v=24.0, current_limit_a=5.0)
"""The zdrive brake, on output 1.

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
grabs, and build before it lets go."""


class ZdriveTestbed:
    """Starts/stops the ODrive and CPX400DP driver processes for zdrive, and owns connected clients for both."""

    DEVICES: Tuple[str, ...] = (DEVICE_ODRIVE, DEVICE_CPX400DP)
    """The devices whose driver processes this testbed owns. Declared here
    because this is what starts them; the test case unions this with its DUT
    façade's declaration (zdrive has none, as with ydrive) and publishes the
    result, so the telemetry engine records both devices into the run's
    directory. See testcases/base.py's DEVICES."""

    def __init__(
        self,
        use_mock_odrive: bool = False,
        odrive_serial_number: Optional[str] = None,
        cpx400dp_host: str = CPX400DP_HOST,
        output_dir: Optional[Path] = None,
        test_id: Optional[str] = None,
    ) -> None:
        """
        use_mock_odrive: run MockOdriveBackend instead of real USB hardware.
            There is no equivalent for the supply - that driver has no mock
            backend - so this testbed always needs a reachable CPX400DP.

        output_dir/test_id: where each driver writes its detailed log. Given
            both, every driver gets `--log-file <output_dir>/runs/<test_id>/
            <device>/logs.txt`, so a decoded ODrive fault or a refused setpoint
            lands beside the telemetry it happened during and the stored run
            explains itself. Omit them and the drivers log to their consoles
            only, which for a subprocess means nowhere anybody reads.

            A test case passes `self._output_dir` and `self.test_id` from
            PreTestSetup; both are resolved by then, since run() resolves the
            engine's real output dir from the heartbeat before that phase. The
            testbed is what starts the drivers, so it is the only participant
            that can hand them the path - the drivers themselves know nothing
            about runs.
        """
        self._use_mock_odrive = use_mock_odrive
        self._odrive_serial_number = odrive_serial_number
        self._cpx400dp_host = cpx400dp_host
        self._output_dir = output_dir
        self._test_id = test_id
        self._processes: List[subprocess.Popen] = []
        self._command: Optional[OdriveCommandClient] = None
        self._telemetry: Optional[TelemetryClient] = None
        self._sync_telemetry: Optional[TelemetryClient] = None
        self._supply: Optional[Cpx400dpCommandClient] = None
        self._supply_telemetry: Optional[TelemetryClient] = None

    # --- lifecycle ---------------------------------------------------------

    def _log_args(self, device: str) -> List[str]:
        """`--log-file` for this device's driver, or nothing if this testbed
        wasn't told which run it belongs to."""
        if self._output_dir is None or self._test_id is None:
            return []
        return ["--log-file", str(driver_log_path(self._output_dir, self._test_id, device))]

    def start(self) -> None:
        """Bring both drivers up, verify their channel surfaces, and configure
        both rails' setpoints - with the outputs left OFF."""
        odrive_args = [sys.executable, "-m", "hardware.odrive.main", *self._log_args(DEVICE_ODRIVE)]
        if self._use_mock_odrive:
            odrive_args.append("--mock")
        elif self._odrive_serial_number is not None:
            odrive_args += ["--serial-number", self._odrive_serial_number]

        # The driver-side ceiling is per-backend, not per-output, so the only
        # values it can carry are the maxima across both rails. That makes it a
        # coarse backstop against a gross typo (60 V, 20 A) rather than per-rail
        # protection: as far as the driver is concerned, 48 V on the 24 V brake
        # rail is a legal command. Nothing here can prevent that - a test holds
        # the same supply client - so check_rails() detects it instead, and is
        # the thing to call if a rail's integrity matters mid-run.
        supply_args = [
            sys.executable, "-m", "hardware.cpx400dp.main",
            "--host", self._cpx400dp_host,
            "--max-voltage", str(max(rail.voltage_v for rail in RAILS)),
            "--max-current", str(max(rail.current_limit_a for rail in RAILS)),
            *self._log_args(DEVICE_CPX400DP),
        ]

        self._processes = [subprocess.Popen(odrive_args), subprocess.Popen(supply_args)]
        time.sleep(STARTUP_DELAY_S)

        self._command = OdriveCommandClient(endpoint=DEFAULT_ODRIVE_COMMAND_ENDPOINT)
        self._telemetry = TelemetryClient(endpoint=DEFAULT_ODRIVE_TELEMETRY_ENDPOINT)
        self._sync_telemetry = TelemetryClient(endpoint=DEFAULT_ODRIVE_TELEMETRY_ENDPOINT)
        self._supply = Cpx400dpCommandClient(endpoint=DEFAULT_CPX400DP_COMMAND_ENDPOINT)
        self._supply_telemetry = TelemetryClient(endpoint=DEFAULT_CPX400DP_TELEMETRY_ENDPOINT)

        self._command.connect_backend()
        self._supply.connect_backend()

        self._command.verify_actions(ODRIVE_COMMAND_CHANNELS)
        self._telemetry.verify_channels(ODRIVE_TELEMETRY_CHANNELS)
        self._supply.verify_actions(CPX400DP_COMMAND_CHANNELS)
        self._supply_telemetry.verify_channels(CPX400DP_TELEMETRY_CHANNELS)

        self._configure_rails()

    def _configure_rails(self) -> None:
        """Set both rails' voltage and current setpoints, refusing to do it
        while an output is live.

        Writing a setpoint to an energized output would step the rail under
        whatever is connected. The supply's driver adopts, rather than resets,
        the output state it finds, so this is the layer that notices - and it
        raises rather than switching the output off, since something else
        energized it deliberately and this testbed does not know what."""
        live = [rail.name for rail in RAILS if self.rail_is_powered(rail)]
        if live:
            raise RuntimeError(
                f"refusing to configure setpoints while these rails are already energized: {live}. "
                "Something outside this testbed switched them on - the supply driver adopts, rather "
                "than resets, the output state it finds. Switch them off before starting a run."
            )

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

        self._safe("disconnect the ODrive backend", lambda: self.command.disconnect_backend())
        self._safe("disconnect the supply backend", lambda: self.supply.disconnect_backend())

        for client in (self._command, self._telemetry, self._sync_telemetry,
                       self._supply, self._supply_telemetry):
            if client is not None:
                self._safe(f"close {type(client).__name__}", client.close)
        self._command = self._telemetry = self._sync_telemetry = None
        self._supply = self._supply_telemetry = None

        for process in self._processes:
            self._safe(f"terminate pid {process.pid}", process.terminate)
        for process in self._processes:
            self._safe(f"reap pid {process.pid}", lambda p=process: p.wait(timeout=5))
        self._processes = []

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
        holding. Pairing the rail with the axis state, and waiting through
        TestCase.wait_for() so a fatal bound is still noticed, belongs in a test
        step; see testcases/ydrive/teststeps/teststeps.py's
        engage_brake()/release_brake() for the shape."""
        self.supply.enable_output(BRAKE_BUS.output, enabled)
        logger.info("%s %s", BRAKE_BUS.name, "released (rail energized)" if enabled else "engaged (rail de-energized)")

    def rail_is_powered(self, rail: Rail) -> bool:
        """Whether this rail's output is currently on, read from the supply."""
        return bool(self.get_supply_channels()[f"output_enabled_{rail.output}"])

    # --- reads ------------------------------------------------------------

    def get_channels(self) -> Dict[str, object]:
        """Block for the next ODrive telemetry frame and return its channels.
        Uses a separate sync client so it doesn't contend with whatever else is
        consuming .telemetry (e.g. LiveRulebookRunner)."""
        return self.sync_telemetry.latest_frame().channels

    def get_supply_channels(self) -> Dict[str, object]:
        """Block for the next supply telemetry frame and return its channels.

        The measured voltage and current are re-read at 5 Hz and held between
        reads, since the instrument's meters refresh at 4 Hz, so consecutive
        frames can carry the same reading."""
        return self.supply_telemetry.latest_frame().channels

    def get_pos_estimate(self) -> float:
        return self.get_channels()["pos_estimate"]

    def get_vel_estimate(self) -> float:
        return self.get_channels()["vel_estimate"]


    def get_brake_voltage(self) -> float:
        return self.get_supply_channels()[f"voltage_{BRAKE_BUS.output}"]

    def get_brake_current(self) -> float:
        return self.get_supply_channels()[f"current_{BRAKE_BUS.output}"]


    @property
    def command(self) -> OdriveCommandClient:
        if self._command is None:
            raise RuntimeError("ZdriveTestbed.command accessed before start()")
        return self._command

    @property
    def telemetry(self) -> TelemetryClient:
        if self._telemetry is None:
            raise RuntimeError("ZdriveTestbed.telemetry accessed before start()")
        return self._telemetry

    @property
    def sync_telemetry(self) -> TelemetryClient:
        if self._sync_telemetry is None:
            raise RuntimeError("ZdriveTestbed.sync_telemetry accessed before start()")
        return self._sync_telemetry

    @property
    def supply(self) -> Cpx400dpCommandClient:
        if self._supply is None:
            raise RuntimeError("ZdriveTestbed.supply accessed before start()")
        return self._supply

    @property
    def supply_telemetry(self) -> TelemetryClient:
        if self._supply_telemetry is None:
            raise RuntimeError("ZdriveTestbed.supply_telemetry accessed before start()")
        return self._supply_telemetry

    def __enter__(self) -> "ZdriveTestbed":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
