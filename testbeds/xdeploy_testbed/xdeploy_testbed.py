"""Physical testbed for xdeploy: the ODrive driver process and the thermocouple
DAQ, with connected clients for both. There is no separate DUT layer - the
ODrive is the entire actuator and sensor interface.

    with XdeployTestbed() as testbed:
        testbed.command.set_axis_state("CLOSED_LOOP_CONTROL")
        ...

NOTHING ON THIS STAND CONTROLS POWER. The ODrive's DC bus comes from a bench
supply outside this framework, set and switched by hand, and the DAQ takes no
commands at all - so unlike ydrive and zdrive there is no rail to configure, no
output to switch, and no check_rails() to confirm a setpoint. What the bus is
doing is knowable only where it is consumed, at the drive's own
`board_vbus_voltage`; get_bus_voltage() reads that, and xdeploy_rulebook's
undervoltage_bound is what watches it.

THE AXIS IS GRAVITY-LOADED AND THERE IS NO BRAKE. Gravity pulls the load in the
POSITIVE (retract) direction, so a disarmed axis runs positive until the load
reaches the ground and is held there. The fall is bounded, but it is still an
uncontrolled drop of the full load from wherever the axis was, and nothing here
can catch it - which is why stop() disarms rather than pretending to safe
anything. See xdeploy_rulebook for what a run does and does not notice.

WHAT THIS CONFIGURES, AND WHAT IT LEAVES ALONE. start() writes the motor's
current ceilings and the regen threshold - stand ceilings, taken from the motor's
nameplate and this stand's brake resistor. It writes nothing else: the controller
tuning is whatever was last loaded and dialled in by hand, and a run uses it as
found.

THE BOARD HAS NO DC BUS OVERVOLTAGE TRIP. Lowering a gravity load onto a bench
supply that cannot sink is caught by the brake resistor and by
xdeploy_rulebook's overvoltage_bound, and by nothing else.

use_mock_odrive substitutes the ODrive only. The DAQ's driver has no mock
backend, so a reachable thermocouple DAQ is always needed.

Test steps use testbed.command/testbed.telemetry directly, and the named
per-channel methods (get_pos_estimate(), ...) for a synchronous point-read -
those use separate sync clients so they do not contend with whatever else is
consuming .telemetry, such as LiveRulebookRunner. Every client accessor raises
RuntimeError before start().
"""
from __future__ import annotations

import logging
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from hardware.clients.telemetry_client import TelemetryClient
from hardware.driver_process import start_driver
from hardware.odrive import odrive_errors
from hardware.odrive.odrive_channels import (
    COMMAND_CHANNELS as ODRIVE_COMMAND_CHANNELS,
    TELEMETRY_CHANNELS as ODRIVE_TELEMETRY_CHANNELS,
)
from hardware.odrive.odrive_command_client import OdriveCommandClient
from hardware.tc_daq.tc_daq_channels import TELEMETRY_CHANNELS as TC_DAQ_TELEMETRY_CHANNELS
from hardware.tc_daq.transport import SILENCE_TIMEOUT_S as TC_DAQ_SILENCE_TIMEOUT_S
from protocol.paths import driver_console_path, driver_log_path
from protocol.wire import (
    DEFAULT_ODRIVE_COMMAND_ENDPOINT,
    DEFAULT_ODRIVE_TELEMETRY_ENDPOINT,
    DEFAULT_TC_DAQ_TELEMETRY_ENDPOINT,
    DEVICE_ODRIVE,
    DEVICE_TC_DAQ,
)

logger = logging.getLogger(__name__)

ODRIVE_MOTOR_SOFT_MAX_A = 28.5
"""Phase current the stand drives to, under the motor's rated ceiling."""

ODRIVE_MOTOR_HARD_MAX_A = 36.0
"""Measured phase current that trips CURRENT_LIMIT_VIOLATION - the motor's rating."""

ODRIVE_MAX_REGEN_CURRENT_A = 10.0
"""Regen current above which the brake resistor shunts, so a lowered load has
somewhere to go: the 1000 W bench supply cannot sink."""


class Motion(NamedTuple):
    """Where the axis is, how fast it is going, and whether it is still driving -
    from one telemetry frame.

    One read rather than three because every question worth asking about a moving
    axis is about all of them at once, and separate reads would answer from
    different frames a sample period apart.

    `armed` is in here because a loop watching a move has to notice the axis
    stopping driving. The ODrive disarms itself on a fault, so a move can end with
    the load coasting and no exception anywhere - which on this axis means it
    running to the ground under gravity while the loop waits for a position."""

    position: float
    velocity: float
    armed: bool


STARTUP_DELAY_S = 1.0
"""Seconds allowed for both drivers to bind their sockets before this testbed
connects their backends. Only socket binding happens here - the ODrive backend's
own connect() runs when connect_backend() is called below and carries its own
timeout, and the DAQ has no connect to call."""

TC_DAQ_STALENESS_S = TC_DAQ_SILENCE_TIMEOUT_S + 2.0
"""How long a TC DAQ frame may be old before its client calls the stream dead.

Above the transport's own silence timeout, so the driver gets to report a dead
serial link itself rather than this client timing out first on the same
condition."""


class XdeployTestbed:
    """Starts/stops the ODrive and thermocouple DAQ driver processes for xdeploy, and owns connected clients for both."""

    DEVICES: Tuple[str, ...] = (DEVICE_ODRIVE, DEVICE_TC_DAQ)
    """The devices whose driver processes this testbed owns. Declared here
    because this is what starts them; the test case unions this with its DUT
    façade's declaration (xdeploy has none, as with ydrive and zdrive) and
    publishes the result, so the telemetry engine records both devices into the
    run's directory. See testcases/base.py's DEVICES.

    THE BENCH SUPPLY IS NOT IN HERE, and cannot be: a device name is a driver
    this framework starts, and nothing drives that supply. Its state reaches the
    record only as the bus voltage the ODrive measures."""

    def __init__(
        self,
        use_mock_odrive: bool = False,
        odrive_serial_number: Optional[str] = None,
        tc_daq_port: Optional[str] = None,
        output_dir: Optional[Path] = None,
        test_id: Optional[str] = None,
    ) -> None:
        """
        use_mock_odrive: run MockOdriveBackend instead of real USB hardware.
            There is no equivalent for the DAQ - its driver has no mock backend -
            so this testbed always needs a reachable thermocouple DAQ.

        output_dir/test_id: where each driver writes its detailed log. Given
            both, every driver gets `--log-file <output_dir>/runs/<test_id>/
            <device>/logs.txt`, so a decoded ODrive fault lands beside the
            telemetry it happened during and the stored run explains itself.
            Omit them and the drivers log to their consoles only, which for a
            subprocess means nowhere anybody reads.

            A test case passes `self._output_dir` and `self.test_id` from
            PreTestSetup; both are resolved by then, since run() resolves the
            engine's real output dir from the heartbeat before that phase. The
            testbed is what starts the drivers, so it is the only participant
            that can hand them the path - the drivers themselves know nothing
            about runs.
        """
        self._use_mock_odrive = use_mock_odrive
        self._odrive_serial_number = odrive_serial_number
        self._tc_daq_port = tc_daq_port
        self._output_dir = output_dir
        self._test_id = test_id
        self._processes: List[subprocess.Popen] = []
        self._device_for_process: List[str] = []
        self._command: Optional[OdriveCommandClient] = None
        self._telemetry: Optional[TelemetryClient] = None
        self._sync_telemetry: Optional[TelemetryClient] = None
        self._tc_daq_telemetry: Optional[TelemetryClient] = None
        self._sync_tc_daq_telemetry: Optional[TelemetryClient] = None

    # --- lifecycle ---------------------------------------------------------

    def _log_args(self, device: str) -> List[str]:
        """`--log-file` for this device's driver, or nothing if this testbed
        wasn't told which run it belongs to."""
        if self._output_dir is None or self._test_id is None:
            return []
        return ["--log-file", str(driver_log_path(self._output_dir, self._test_id, device))]

    def _console_path(self, device: str):
        """Where to capture this device's raw stdout/stderr, or None if this testbed
        wasn't told which run it belongs to - see start_driver()."""
        if self._output_dir is None or self._test_id is None:
            return None
        return driver_console_path(self._output_dir, self._test_id, device)

    def start(self) -> None:
        """Bring both drivers up, verify their channel surfaces, and write the
        ODrive's current and regen ceilings. Nothing else is configured."""
        odrive_args = [sys.executable, "-m", "hardware.odrive.main", *self._log_args(DEVICE_ODRIVE)]
        if self._use_mock_odrive:
            odrive_args.append("--mock")
        elif self._odrive_serial_number is not None:
            odrive_args += ["--serial-number", self._odrive_serial_number]

        # The thermocouple DAQ takes no commands at all, so it gets no command
        # client below - nothing would be sendable through one. Its driver is
        # started, its stream is verified, and that is the whole interface. With
        # no --port it finds itself by its USB bridge's vendor id.
        tc_daq_args = [
            sys.executable, "-m", "hardware.tc_daq.main",
            *(["--port", self._tc_daq_port] if self._tc_daq_port else []),
            *self._log_args(DEVICE_TC_DAQ),
        ]

        self._processes = [
            start_driver(odrive_args, self._console_path(DEVICE_ODRIVE)),
            start_driver(tc_daq_args, self._console_path(DEVICE_TC_DAQ)),
        ]
        self._device_for_process = [DEVICE_ODRIVE, DEVICE_TC_DAQ]
        time.sleep(STARTUP_DELAY_S)

        self._command = OdriveCommandClient(endpoint=DEFAULT_ODRIVE_COMMAND_ENDPOINT)
        self._telemetry = TelemetryClient(endpoint=DEFAULT_ODRIVE_TELEMETRY_ENDPOINT)
        self._sync_telemetry = TelemetryClient(endpoint=DEFAULT_ODRIVE_TELEMETRY_ENDPOINT)
        self._tc_daq_telemetry = TelemetryClient(
            endpoint=DEFAULT_TC_DAQ_TELEMETRY_ENDPOINT, timeout_s=TC_DAQ_STALENESS_S
        )
        self._sync_tc_daq_telemetry = TelemetryClient(
            endpoint=DEFAULT_TC_DAQ_TELEMETRY_ENDPOINT, timeout_s=TC_DAQ_STALENESS_S
        )
        """A second subscription on each endpoint, for this process's own reads.

        The first of each pair goes to LiveRulebookRunner, which reads it on a
        thread of its own for the whole run. A SUB socket is not thread-safe and a
        subscription delivers each frame once, so a testbed reading the runner's
        client would both tear messages and take frames the bounds were meant to
        see. One client per consumer is what keeps them apart."""

        # Before waiting on a command server or a telemetry deadline: a driver
        # that has already exited will never answer, and its own log says why.
        self._require_drivers_alive()
        self._command.connect_backend()

        self._command.verify_actions(ODRIVE_COMMAND_CHANNELS)
        self._telemetry.verify_channels(ODRIVE_TELEMETRY_CHANNELS)
        # No verify_actions for the DAQ: it declares no commands, so there is
        # nothing to confirm. Its stream is the only thing to check, and a
        # faulted thermocouple still publishes its channel (as None), so this
        # passes with sensors unplugged - what it catches is a driver that
        # started against the wrong port and is streaming something else.
        self._tc_daq_telemetry.verify_channels(TC_DAQ_TELEMETRY_CHANNELS)

        # After the command surface is verified, not before: a write to an action
        # the board does not have should fail as a missing action rather than as
        # a command timeout. Written every run so the ceilings cannot be
        # inherited from whoever last touched the board; the controller tuning is
        # deliberately not written - see this module's docstring.
        self._command.set_motor_config_current_soft_max(ODRIVE_MOTOR_SOFT_MAX_A)
        self._command.set_motor_config_current_hard_max(ODRIVE_MOTOR_HARD_MAX_A)
        self._command.set_board_config_max_regen_current(ODRIVE_MAX_REGEN_CURRENT_A)

        # Said once at the top of every run, because it is what a recorded log
        # cannot otherwise establish: which numbers this run drove under, and
        # that the tuning behind them was not this stand's choice.
        logger.info(
            "xdeploy testbed up: %s and %s streaming, motor current limited to %.1f/%.1f A soft/hard "
            "with regen above %.1f A shunted. The controller tuning is the board's own, and this "
            "stand holds no supply - the DC bus is whatever the bench supply is set to.",
            DEVICE_ODRIVE, DEVICE_TC_DAQ, ODRIVE_MOTOR_SOFT_MAX_A, ODRIVE_MOTOR_HARD_MAX_A,
            ODRIVE_MAX_REGEN_CURRENT_A,
        )

    def _require_drivers_alive(self) -> None:
        """Raise if either driver process has already exited.

        Without this, a driver that died during startup - an ODrive that is not
        attached, a thermocouple DAQ that is unplugged - surfaces as a timeout
        naming neither the device nor the reason, and the DAQ's is the worse of
        the two: it has no command client, so it surfaces as a telemetry
        staleness deadline rather than a refused connect. The exit code and the
        log path are what a person actually needs, and both are known here."""
        dead = [
            (device, process)
            for device, process in zip(self._device_for_process, self._processes)
            if process.poll() is not None
        ]
        if not dead:
            return
        detail = "; ".join(
            f"{device} driver exited with code {process.returncode}"
            + (f" - see {driver_log_path(self._output_dir, self._test_id, device)}"
               if self._output_dir is not None and self._test_id is not None else "")
            for device, process in dead
        )
        raise RuntimeError(f"a hardware driver did not stay up: {detail}")

    def stop(self) -> None:
        """Disarm the axis, then close everything down, finishing even if a step
        fails.

        THIS DOES NOT SAFE THE STAND, AND NOTHING HERE COULD. The bus belongs to
        a bench supply this framework does not hold, and the axis has no brake -
        so disarming is the most a teardown can do, and on a gravity-loaded axis
        it is also the moment the load is left to itself. A run that ends with the
        load still lifted ends with it dropping to the ground; getting it down
        first is that test's business, taken before teardown, not something this
        can retrofit.

        Each step runs independently, logging a failure rather than raising, so
        one wedged client cannot leave the rest of the stand up."""
        self._safe("disarm the ODrive axis", lambda: self.command.set_axis_state("IDLE"))
        self._safe("disconnect the ODrive backend", lambda: self.command.disconnect_backend())

        for client in (self._command, self._telemetry, self._sync_telemetry,
                       self._tc_daq_telemetry, self._sync_tc_daq_telemetry):
            if client is not None:
                self._safe(f"close {type(client).__name__}", client.close)
        self._command = self._telemetry = self._sync_telemetry = None
        self._tc_daq_telemetry = self._sync_tc_daq_telemetry = None

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

    # --- reads ------------------------------------------------------------

    def get_channels(self) -> Dict[str, object]:
        """Block for the next ODrive telemetry frame and return its channels.
        Uses a separate sync client so it doesn't contend with whatever else is
        consuming .telemetry (e.g. LiveRulebookRunner)."""
        return self.sync_telemetry.latest_frame().channels

    def get_bus_voltage(self) -> float:
        """The DC bus voltage, measured at the drive.

        THE ONLY MEASUREMENT OF THE BUS THIS STAND HAS. The supply is a bench
        instrument nothing here talks to, so there is no setpoint to compare
        against and no second opinion - what the drive reports is the whole
        answer, and a person at the supply is the only thing that changes it."""
        return float(self.get_channels()["board_vbus_voltage"])

    def get_fet_temperature_c(self) -> float:
        """The inverter FET temperature, in Celsius, off the ODrive's own thermistor.

        The drive's own thermal state, which no thermocouple on this stand
        measures. Unbounded on xdeploy - see xdeploy_rulebook - so this is a read
        for a step that wants it, not a limit anything enforces."""
        return float(self.get_channels()["motor_fet_thermistor_temperature"])

    def get_tc_temperatures_c(self) -> Dict[int, float]:
        """Every wired thermocouple, by channel number, in Celsius.

        Only the channels carrying a number: this DAQ streams eight and reports
        FAULT for one it cannot read, which the driver publishes as None. A
        caller comparing against a limit wants the readings that exist rather
        than a None to guard against - and a bounded channel going open is
        already fatal through the rulebook, which is a better place to notice it
        than a flow-control check."""
        channels = self.sync_tc_daq_telemetry.latest_frame().channels
        readings = {}
        for name, value in channels.items():
            if name.startswith("temperature_") and name.endswith("_c"):
                if isinstance(value, (int, float)):
                    readings[int(name.split("_")[1])] = float(value)
        return readings

    def get_motion(self) -> Motion:
        """Position, velocity and whether the axis is driving, from one frame.

        Raises if the position is not a usable number - see
        _require_finite_position()."""
        channels = self.get_channels()
        return Motion(
            position=self._require_finite_position(channels),
            velocity=channels["vel_estimate"],
            armed=bool(channels["axis_is_armed"]),
        )

    def get_pos_estimate(self) -> float:
        """Where the axis is, in turns.

        Raises if the reading is not a usable number - see
        _require_finite_position()."""
        return self._require_finite_position(self.get_channels())

    def _require_finite_position(self, channels: Dict[str, object]) -> float:
        """This frame's `pos_estimate`, or raise if it is not a finite number.

        pos_estimate READS NaN WHILE EVERY OTHER CHANNEL LOOKS HEALTHY, from an
        uncalibrated board or a dead encoder, and nothing downstream survives it
        quietly: every comparison against a NaN is False, so a move never judges
        itself arrived, and a NaN taken as a run's origin propagates into every
        target derived from it. The two causes and how the mapper statuses tell
        them apart are written out in full in
        testbeds/zdrive_testbed/zdrive_testbed.py's copy of this guard; it is
        repeated here rather than shared because it is a fact about the ODrive
        and belongs in hardware/odrive/ once a third stand needs it.

        Rejected here, at the one place both position accessors pass through,
        rather than left for each caller to test. Raising is safe wherever a
        position is read: nothing in stop() reads one, so the axis is still
        disarmed."""
        raw = channels["pos_estimate"]
        try:
            position = float(raw)
        except (TypeError, ValueError):
            position = float("nan")
        if math.isfinite(position):
            return position

        posvel = odrive_errors.describe("posvelmapper_status", channels.get("posvelmapper_status"))
        commut = odrive_errors.describe("commutmapper_status", channels.get("commutmapper_status"))
        raise RuntimeError(
            f"the ODrive published pos_estimate={raw!r}, which is not a position anything can be "
            f"commanded relative to. posvelmapper_status={posvel}, commutmapper_status={commut}. "
            "MISSING_INPUT on either means the encoder is not delivering a usable signal - check "
            "the sensor and its magnet, since a dead encoder streams a random angle that both "
            "mappers reject and that calibration can still appear to succeed against. "
            "RELATIVE_MODE on posvelmapper with the encoder otherwise healthy means the opposite: "
            "axis0.pos_vel_mapper.config.offset_valid is False and the board needs its encoder "
            "offset calibration run and saved"
        )

    def get_vel_estimate(self) -> float:
        return self.get_channels()["vel_estimate"]

    def get_axis_armed_status(self) -> bool:
        """Whether the axis is actively controlling the motor (`axis_is_armed`).

        Requesting an axis state only writes `requested_state`; the ODrive acts on
        it asynchronously and can decline. This is the reading that says whether
        it took - and on a stand with no brake it is also the reading that says
        whether anything is holding the load."""
        return bool(self.get_channels()["axis_is_armed"])

    def get_faults(self) -> Dict[str, str]:
        """Every watched ODrive channel currently reading as a fault, decoded -
        empty when the board is clean. One frame, so it describes one instant."""
        return odrive_errors.faults_in_frame(self.get_channels())

    def describe_errors(self) -> Dict[str, str]:
        """Every watched channel decoded, faulted or not - the diagnostic for
        "why did the axis refuse", where a channel reading NOMINAL is as much of
        the answer as one reading a fault. One frame."""
        channels = self.get_channels()
        return {
            name: odrive_errors.describe(name, channels[name])
            for name in odrive_errors.WATCHED_CHANNELS
            if name in channels
        }

    # --- clients ----------------------------------------------------------

    @property
    def command(self) -> OdriveCommandClient:
        if self._command is None:
            raise RuntimeError("XdeployTestbed.command accessed before start()")
        return self._command

    @property
    def telemetry(self) -> TelemetryClient:
        if self._telemetry is None:
            raise RuntimeError("XdeployTestbed.telemetry accessed before start()")
        return self._telemetry

    @property
    def sync_telemetry(self) -> TelemetryClient:
        """This process's own ODrive subscription - see start()."""
        if self._sync_telemetry is None:
            raise RuntimeError("XdeployTestbed.sync_telemetry accessed before start()")
        return self._sync_telemetry

    @property
    def tc_daq_telemetry(self) -> TelemetryClient:
        """The thermocouple DAQ's stream.

        The only interface this device has - it accepts no commands, so there is
        no command client to pair with it."""
        if self._tc_daq_telemetry is None:
            raise RuntimeError("XdeployTestbed.tc_daq_telemetry accessed before start()")
        return self._tc_daq_telemetry

    @property
    def sync_tc_daq_telemetry(self) -> TelemetryClient:
        """This process's own thermocouple subscription - see start()."""
        if self._sync_tc_daq_telemetry is None:
            raise RuntimeError("XdeployTestbed.sync_tc_daq_telemetry accessed before start()")
        return self._sync_tc_daq_telemetry

    def __enter__(self) -> "XdeployTestbed":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
