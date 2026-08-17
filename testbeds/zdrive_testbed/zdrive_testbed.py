"""Physical testbed for zdrive: an ODrive motor controller and a CPX400DP bench
supply feeding two rails - a 48 V motor bus and a 24 V brake.

Starts and stops both hardware driver processes as a unit and owns connected
command/telemetry clients for each, so a test case instantiates one object to
get control of the whole stand. Unlike YdriveTestbed, which owns a single
device, this is the first testbed on real hardware where the instruments have
to be sequenced against each other.

    with ZdriveTestbed() as testbed:
        testbed.power_motor_bus(True)
        testbed.command.set_axis_state("CLOSED_LOOP_CONTROL")
        testbed.release_brake()
        ...

WHAT THIS DOES NOT DO: energize anything at start(). Rail setpoints are
configured with both outputs off, and powering the drive is the test's own
decision, taken in PreTestSetup - the same division ExampleTestbed keeps. A
testbed that energized a motor bus merely by being constructed would make
`with ZdriveTestbed()` a live-hardware action, which is not what a context
manager should imply.

It also does not configure the ODrive. In particular it does not touch
`board_config_dc_bus_overvoltage_trip_level`, which matters on a 48 V bus and
is worth checking once against this rail - but ODrive config is persistent
device state, so a testbed writing it silently on every run would be changing
the board behind whoever set it.

THE BRAKE IS SPRING-APPLIED. Powering output 1 releases it; removing power lets
it grab and hold the load. Every method here is named for the rail rather than
the brake action where the distinction could mislead, and `release_brake()` /
`engage_brake()` are provided as the readable aliases - if a future zdrive
revision uses a power-applied brake, those two aliases and stop()'s ordering
are the only places that assumption lives.
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

from .config.instruments import BRAKE_BUS, CPX400DP_HOST, MOTOR_BUS, RAILS, Rail, deliverable_current_a

logger = logging.getLogger(__name__)

STARTUP_DELAY_S = 1.0
"""Let both drivers bind their sockets and finish connecting. Longer than
YdriveTestbed's 0.5 s because the supply's connect() does real work before it
serves: identity check, `*CLS`, a probe of all 27 declared queries, and a read
of the cached tier - about 65 ms of round-trips on top of process startup."""


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
        whatever is connected. The supply's own driver is passive and will adopt
        an output it finds already on, so this is the layer that has to notice -
        and it raises rather than switching the output off, because something
        else deliberately energized it and this testbed does not know what."""
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

        Called at the end of start() to confirm the writes took - the supply
        accepts and then silently discards a value it dislikes, so a write is
        not evidence of a setpoint. Also worth calling from a test at any point
        a rail's integrity matters, because the driver's own ceiling is
        per-backend rather than per-output: it cannot stop 48 V being commanded
        onto the 24 V brake rail, and a test holds the same supply client this
        testbed does. Detection is the only guard available at this layer."""
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
                "See testbeds/zdrive_testbed/config/instruments.py for what the rails should be."
            )

    def stop(self) -> None:
        """Tear the stand down in a safe order, and finish even if a step fails.

        THE ORDER MATTERS AND IS NOT ARBITRARY. The brake is spring-applied, so
        dropping its rail first makes the brake grab and hold the load before
        anything else changes. Only then is the axis disarmed and the motor bus
        removed. Reversing this would leave the load unheld while the drive is
        being shut down.

        Each step is independent: a failure is logged and the rest still run.
        That matters more here than in a single-device testbed, because these
        steps are a power sequence - one client failing to answer must not leave
        a 48 V bus energized. It mirrors what TestCase.teardown_step() does for
        the phases above this."""
        self._safe("engage the brake (drop the 24 V rail)", lambda: self.engage_brake())
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

        Two measured caveats from the supply itself: the output ramps rather
        than stepping, so a check immediately after enabling reads low; and
        switching off does not mean zero volts, since the output capacitance
        takes a moment to discharge."""
        self.supply.enable_output(MOTOR_BUS.output, enabled)
        logger.info("%s %s", MOTOR_BUS.name, "energized" if enabled else "de-energized")

    def power_brake_bus(self, enabled: bool) -> None:
        """Switch the 24 V brake rail (output 1) on or off.

        Prefer release_brake()/engage_brake(), which say what the rail does."""
        self.supply.enable_output(BRAKE_BUS.output, enabled)
        logger.info("%s %s", BRAKE_BUS.name, "energized" if enabled else "de-energized")

    def release_brake(self) -> None:
        """Power the brake rail, releasing the brake. Spring-applied: powered is
        released."""
        self.power_brake_bus(True)

    def engage_brake(self) -> None:
        """Remove power from the brake rail, letting the brake grab."""
        self.power_brake_bus(False)

    def rail_is_powered(self, rail: Rail) -> bool:
        """Whether this rail's output is currently on, read from the supply."""
        return bool(self.get_supply_channels()[f"output_enabled_{rail.output}"])

    # --- reads ------------------------------------------------------------

    def get_channels(self) -> Dict[str, object]:
        """Block for the next ODrive telemetry frame and return its channels.
        Uses a separate sync client so it doesn't contend with whatever else is
        consuming .telemetry (e.g. LiveRulebookRunner)."""
        return next(self.sync_telemetry.frames()).channels

    def get_supply_channels(self) -> Dict[str, object]:
        """Block for the next supply telemetry frame and return its channels.

        Note the measured voltage and current in it are re-read from the
        instrument at 5 Hz and held between reads - its meters only update at
        4 Hz - so consecutive frames can carry the same reading."""
        return next(self.supply_telemetry.frames()).channels

    def get_pos_estimate(self) -> float:
        return self.get_channels()["pos_estimate"]

    def get_vel_estimate(self) -> float:
        return self.get_channels()["vel_estimate"]

    def get_motor_bus_voltage(self) -> float:
        """Measured volts on the 48 V rail, from the supply's own meter. This is
        the supply's view; the ODrive's own `board_vbus_voltage` is the drive's
        view of the same rail, and comparing them shows the cable drop."""
        return self.get_supply_channels()[f"voltage_{MOTOR_BUS.output}"]

    def get_motor_bus_current(self) -> float:
        """Measured amps into the drive. Not trustworthy below a few tens of
        milliamps - the supply's current readback is +-0.3% of reading +-2
        digits, and a digit is 10 mA."""
        return self.get_supply_channels()[f"current_{MOTOR_BUS.output}"]

    def get_brake_voltage(self) -> float:
        return self.get_supply_channels()[f"voltage_{BRAKE_BUS.output}"]

    def get_brake_current(self) -> float:
        return self.get_supply_channels()[f"current_{BRAKE_BUS.output}"]

    def motor_bus_is_unregulated(self) -> bool:
        """Whether the motor bus has hit the supply's power envelope.

        This is the channel that matters on this rail rather than current: the
        configured 16 A limit is above the 8.75 A the supply can source at 48 V,
        so an overdraw shows up as the output going unregulated and the voltage
        sagging, not as a current limit engaging."""
        return bool(self.get_supply_channels()[f"in_power_limit_{MOTOR_BUS.output}"])

    # --- accessors --------------------------------------------------------

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
