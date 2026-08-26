"""Real ODrive backend - talks to a physical ODrive over USB via the
official `odrive` Python package (firmware 0.6.x, Pro/S1). See
mock_backend.py for the no-hardware-needed version.

Depends on the `odrive` package (a hard dependency of this project - see
pyproject.toml) for actual USB communication. That import is still
deferred into connect() rather than module load time, so importing this
module - or running main.py with --mock - doesn't pay for it unless a
real connection is actually attempted.

connect() only opens the USB link and confirms the device answers -
it does NOT engage CLOSED_LOOP_CONTROL. Call
set_axis_state("CLOSED_LOOP_CONTROL") explicitly before commanding
position/velocity/torque, the same way this framework never assumes a
power supply's output is on just because it's connected. Pass
serial_number to OdriveBackend() to pick a specific board if more than
one ODrive is attached.

Every channel in odrive_channels.py is implemented here via one
attribute-path table (_TELEMETRY_PATHS/_SETTERS/_METHODS) rather than a
hand-written accessor per channel; _validate_channel_coverage() (run at
import time) fails loudly if that table and the declared channel lists
ever disagree. See AI/Mytest.md for the full design rationale and
known ODrive documentation caveats.

connect() passes a `timeout` kwarg to odrive.find_any() so a missing/
unpowered device fails after discovery_timeout_s instead of hanging
forever - unverified against a real device like everything else here,
confirm find_any() actually accepts `timeout` for your installed
`odrive` package version before relying on it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from ..backend import HardwareBackend, HardwareError, MissingChannelError, to_jsonable
from protocol.wire import DEVICE_ODRIVE

from . import odrive_errors
from .odrive_channels import COMMAND_CHANNELS, TELEMETRY_CHANNELS

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_S = 0.05
"""Sleep *between* frames, not a frame period - and against real hardware
the difference matters. Each frame reads its declared channels as that many
sequential USB round-trips, which measured ~29 ms on a real ODrive Pro
(fw 0.6.11), so the achieved rate is ~12.6 Hz, not the 20 Hz this interval
alone implies. Measured over a 60 s ManualTest run: 749 frames in 59.9 s.

Left at 0.05 deliberately rather than retuned: dropping it to ~0.021 would
get closer to a true 20 Hz, but the right value depends on what the tests
actually need from the sample rate, which is a test-engineering decision,
not a driver one. Whatever it's set to, the publisher's high-water mark is
sized from it (see protocol/wire.py's hwm_for_interval), so the buffer stays
proportional to the intended rate."""
DEFAULT_DISCOVERY_TIMEOUT_S = 10.0  # how long connect() waits for odrive.find_any() to see a matching device

_UNSET = object()
"""Distinguishes "this channel has not been seen yet" from any real value, so
the first frame can report a pre-existing fault without announcing every
channel that is simply reading zero."""

_AXIS_STATE_NAMES = ("IDLE", "CLOSED_LOOP_CONTROL")
_CONTROL_MODE_NAMES = ("POSITION_CONTROL", "VELOCITY_CONTROL", "TORQUE_CONTROL")

# channel name -> (root, dotted_path). root "axis" means odrv0.axis0.<path>, "odrv" means odrv0.<path>.
# See odrive_channels.py for what each channel means (unit/description) - this table only carries
# the real attribute path, generated from and validated against that module's declared channel lists.
_TELEMETRY_PATHS: Dict[str, Tuple[str, str]] = {
    "active_errors": ("axis", "active_errors"),
    "axis_config_commutation_encoder": ("axis", "config.commutation_encoder"),
    "axis_config_enable_watchdog": ("axis", "config.enable_watchdog"),
    "axis_config_i_bus_hard_max": ("axis", "config.I_bus_hard_max"),
    "axis_config_i_bus_hard_min": ("axis", "config.I_bus_hard_min"),
    "axis_config_i_bus_soft_max": ("axis", "config.I_bus_soft_max"),
    "axis_config_i_bus_soft_min": ("axis", "config.I_bus_soft_min"),
    "axis_config_load_encoder": ("axis", "config.load_encoder"),
    "axis_config_p_bus_soft_max": ("axis", "config.P_bus_soft_max"),
    "axis_config_p_bus_soft_min": ("axis", "config.P_bus_soft_min"),
    "axis_config_startup_closed_loop_control": ("axis", "config.startup_closed_loop_control"),
    "axis_config_startup_encoder_index_search": ("axis", "config.startup_encoder_index_search"),
    "axis_config_startup_encoder_offset_calibration": ("axis", "config.startup_encoder_offset_calibration"),
    "axis_config_startup_homing": ("axis", "config.startup_homing"),
    "axis_config_startup_max_wait_for_ready": ("axis", "config.startup_max_wait_for_ready"),
    "axis_config_startup_motor_calibration": ("axis", "config.startup_motor_calibration"),
    "axis_config_torque_soft_max": ("axis", "config.torque_soft_max"),
    "axis_config_torque_soft_min": ("axis", "config.torque_soft_min"),
    "axis_config_watchdog_timeout": ("axis", "config.watchdog_timeout"),
    "axis_current_state": ("axis", "current_state"),
    "axis_is_armed": ("axis", "is_armed"),
    "axis_is_homed": ("axis", "is_homed"),
    "axis_procedure_result": ("axis", "procedure_result"),
    "board_config_dc_bus_overvoltage_trip_level": ("odrv", "config.dc_bus_overvoltage_trip_level"),
    "board_config_dc_bus_undervoltage_trip_level": ("odrv", "config.dc_bus_undervoltage_trip_level"),
    "board_config_dc_max_negative_current": ("odrv", "config.dc_max_negative_current"),
    "board_config_dc_max_positive_current": ("odrv", "config.dc_max_positive_current"),
    "board_config_inverter0_current_hard_max": ("odrv", "config.inverter0.current_hard_max"),
    "board_config_inverter0_current_soft_max": ("odrv", "config.inverter0.current_soft_max"),
    "board_config_inverter0_temp_limit_lower": ("odrv", "config.inverter0.temp_limit_lower"),
    "board_config_inverter0_temp_limit_upper": ("odrv", "config.inverter0.temp_limit_upper"),
    "board_config_max_regen_current": ("odrv", "config.max_regen_current"),
    "board_ibus": ("odrv", "ibus"),
    "board_serial_number": ("odrv", "serial_number"),
    "board_vbus_voltage": ("odrv", "vbus_voltage"),
    "commutmapper_status": ("axis", "commutation_mapper.status"),
    "controller_config_absolute_setpoints": ("axis", "controller.config.absolute_setpoints"),
    "controller_config_circular_setpoint_range": ("axis", "controller.config.circular_setpoint_range"),
    "controller_config_circular_setpoints": ("axis", "controller.config.circular_setpoints"),
    "controller_config_control_mode": ("axis", "controller.config.control_mode"),
    "controller_config_enable_overspeed_error": ("axis", "controller.config.enable_overspeed_error"),
    "controller_config_enable_torque_mode_vel_limit": ("axis", "controller.config.enable_torque_mode_vel_limit"),
    "controller_config_enable_vel_limit": ("axis", "controller.config.enable_vel_limit"),
    "controller_config_homing_speed": ("axis", "controller.config.homing_speed"),
    "controller_config_input_filter_bandwidth": ("axis", "controller.config.input_filter_bandwidth"),
    "controller_config_input_mode": ("axis", "controller.config.input_mode"),
    "controller_config_pos_gain": ("axis", "controller.config.pos_gain"),
    "controller_config_spinout_electrical_power_bandwidth": ("axis", "controller.config.spinout_electrical_power_bandwidth"),
    "controller_config_spinout_electrical_power_threshold": ("axis", "controller.config.spinout_electrical_power_threshold"),
    "controller_config_spinout_mechanical_power_bandwidth": ("axis", "controller.config.spinout_mechanical_power_bandwidth"),
    "controller_config_spinout_mechanical_power_threshold": ("axis", "controller.config.spinout_mechanical_power_threshold"),
    "controller_config_torque_ramp_rate": ("axis", "controller.config.torque_ramp_rate"),
    "controller_config_vel_gain": ("axis", "controller.config.vel_gain"),
    "controller_config_vel_integrator_gain": ("axis", "controller.config.vel_integrator_gain"),
    "controller_config_vel_integrator_limit": ("axis", "controller.config.vel_integrator_limit"),
    "controller_config_vel_limit": ("axis", "controller.config.vel_limit"),
    "controller_config_vel_limit_tolerance": ("axis", "controller.config.vel_limit_tolerance"),
    "controller_config_vel_ramp_rate": ("axis", "controller.config.vel_ramp_rate"),
    "controller_effective_torque_setpoint": ("axis", "controller.effective_torque_setpoint"),
    "controller_input_pos": ("axis", "controller.input_pos"),
    "controller_input_torque": ("axis", "controller.input_torque"),
    "controller_input_vel": ("axis", "controller.input_vel"),
    "controller_pos_setpoint": ("axis", "controller.pos_setpoint"),
    "controller_spinout_electrical_power": ("axis", "controller.spinout_electrical_power"),
    "controller_spinout_mechanical_power": ("axis", "controller.spinout_mechanical_power"),
    "controller_torque_setpoint": ("axis", "controller.torque_setpoint"),
    "controller_trajectory_done": ("axis", "controller.trajectory_done"),
    "controller_vel_integrator_torque": ("axis", "controller.vel_integrator_torque"),
    "controller_vel_setpoint": ("axis", "controller.vel_setpoint"),
    "debug_mcu_temperature": ("odrv", "debug.mcu_temperature"),
    "detailed_disarm_reason": ("axis", "detailed_disarm_reason"),
    "disarm_reason": ("axis", "disarm_reason"),
    "encoder_onboard0_raw": ("odrv", "onboard_encoder0.raw"),
    "encoder_onboard0_status": ("odrv", "onboard_encoder0.status"),
    "last_drv_fault": ("axis", "last_drv_fault"),
    "maxendstop_state": ("axis", "max_endstop.state"),
    "minendstop_state": ("axis", "min_endstop.state"),
    "motor_config_current_hard_max": ("axis", "config.motor.current_hard_max"),
    "motor_config_current_soft_max": ("axis", "config.motor.current_soft_max"),
    "motor_config_direction": ("axis", "config.motor.direction"),
    "motor_config_motor_type": ("axis", "config.motor.motor_type"),
    "motor_config_phase_inductance": ("axis", "config.motor.phase_inductance"),
    "motor_config_phase_resistance": ("axis", "config.motor.phase_resistance"),
    "motor_config_pole_pairs": ("axis", "config.motor.pole_pairs"),
    "motor_config_torque_constant": ("axis", "config.motor.torque_constant"),
    "motor_effective_current_lim": ("axis", "motor.effective_current_lim"),
    "motor_electrical_power": ("axis", "motor.electrical_power"),
    "motor_fet_thermistor_temperature": ("axis", "motor.fet_thermistor.temperature"),
    "motor_foc_id_measured": ("axis", "motor.foc.Id_measured"),
    "motor_foc_iq_measured": ("axis", "motor.foc.Iq_measured"),
    "motor_loss_power": ("axis", "motor.loss_power"),
    "motor_mechanical_power": ("axis", "motor.mechanical_power"),
    "motor_motor_thermistor_temperature": ("axis", "motor.motor_thermistor.temperature"),
    "motor_torque_estimate": ("axis", "motor.torque_estimate"),
    "pos_estimate": ("axis", "pos_estimate"),
    "posvelmapper_status": ("axis", "pos_vel_mapper.status"),
    "traptraj_config_accel_limit": ("axis", "trap_traj.config.accel_limit"),
    "traptraj_config_decel_limit": ("axis", "trap_traj.config.decel_limit"),
    "traptraj_config_vel_limit": ("axis", "trap_traj.config.vel_limit"),
    "vel_estimate": ("axis", "vel_estimate"),
}

# command name -> (root, dotted_path) for plain setattr(value) commands. Excludes set_axis_state/
# set_control_mode (handled specially in execute() - see module docstring) and anything in _METHODS.
_SETTERS: Dict[str, Tuple[str, str]] = {
    "set_pos_estimate": ("axis", "pos_estimate"),
    "set_axis_config_commutation_encoder": ("axis", "config.commutation_encoder"),
    "set_axis_config_enable_watchdog": ("axis", "config.enable_watchdog"),
    "set_axis_config_i_bus_hard_max": ("axis", "config.I_bus_hard_max"),
    "set_axis_config_i_bus_hard_min": ("axis", "config.I_bus_hard_min"),
    "set_axis_config_i_bus_soft_max": ("axis", "config.I_bus_soft_max"),
    "set_axis_config_i_bus_soft_min": ("axis", "config.I_bus_soft_min"),
    "set_axis_config_load_encoder": ("axis", "config.load_encoder"),
    "set_axis_config_p_bus_soft_max": ("axis", "config.P_bus_soft_max"),
    "set_axis_config_p_bus_soft_min": ("axis", "config.P_bus_soft_min"),
    "set_axis_config_startup_closed_loop_control": ("axis", "config.startup_closed_loop_control"),
    "set_axis_config_startup_encoder_index_search": ("axis", "config.startup_encoder_index_search"),
    "set_axis_config_startup_encoder_offset_calibration": ("axis", "config.startup_encoder_offset_calibration"),
    "set_axis_config_startup_homing": ("axis", "config.startup_homing"),
    "set_axis_config_startup_max_wait_for_ready": ("axis", "config.startup_max_wait_for_ready"),
    "set_axis_config_startup_motor_calibration": ("axis", "config.startup_motor_calibration"),
    "set_axis_config_torque_soft_max": ("axis", "config.torque_soft_max"),
    "set_axis_config_torque_soft_min": ("axis", "config.torque_soft_min"),
    "set_axis_config_watchdog_timeout": ("axis", "config.watchdog_timeout"),
    "set_board_config_dc_bus_overvoltage_trip_level": ("odrv", "config.dc_bus_overvoltage_trip_level"),
    "set_board_config_dc_bus_undervoltage_trip_level": ("odrv", "config.dc_bus_undervoltage_trip_level"),
    "set_board_config_dc_max_negative_current": ("odrv", "config.dc_max_negative_current"),
    "set_board_config_dc_max_positive_current": ("odrv", "config.dc_max_positive_current"),
    "set_board_config_inverter0_current_hard_max": ("odrv", "config.inverter0.current_hard_max"),
    "set_board_config_inverter0_current_soft_max": ("odrv", "config.inverter0.current_soft_max"),
    "set_board_config_inverter0_temp_limit_lower": ("odrv", "config.inverter0.temp_limit_lower"),
    "set_board_config_inverter0_temp_limit_upper": ("odrv", "config.inverter0.temp_limit_upper"),
    "set_board_config_max_regen_current": ("odrv", "config.max_regen_current"),
    "set_controller_config_absolute_setpoints": ("axis", "controller.config.absolute_setpoints"),
    "set_controller_config_circular_setpoint_range": ("axis", "controller.config.circular_setpoint_range"),
    "set_controller_config_circular_setpoints": ("axis", "controller.config.circular_setpoints"),
    "set_controller_config_enable_overspeed_error": ("axis", "controller.config.enable_overspeed_error"),
    "set_controller_config_enable_torque_mode_vel_limit": ("axis", "controller.config.enable_torque_mode_vel_limit"),
    "set_controller_config_enable_vel_limit": ("axis", "controller.config.enable_vel_limit"),
    "set_controller_config_homing_speed": ("axis", "controller.config.homing_speed"),
    "set_controller_config_input_filter_bandwidth": ("axis", "controller.config.input_filter_bandwidth"),
    "set_controller_config_input_mode": ("axis", "controller.config.input_mode"),
    "set_controller_config_pos_gain": ("axis", "controller.config.pos_gain"),
    "set_controller_config_spinout_electrical_power_bandwidth": ("axis", "controller.config.spinout_electrical_power_bandwidth"),
    "set_controller_config_spinout_electrical_power_threshold": ("axis", "controller.config.spinout_electrical_power_threshold"),
    "set_controller_config_spinout_mechanical_power_bandwidth": ("axis", "controller.config.spinout_mechanical_power_bandwidth"),
    "set_controller_config_spinout_mechanical_power_threshold": ("axis", "controller.config.spinout_mechanical_power_threshold"),
    "set_controller_config_torque_ramp_rate": ("axis", "controller.config.torque_ramp_rate"),
    "set_controller_config_vel_gain": ("axis", "controller.config.vel_gain"),
    "set_controller_config_vel_integrator_gain": ("axis", "controller.config.vel_integrator_gain"),
    "set_controller_config_vel_integrator_limit": ("axis", "controller.config.vel_integrator_limit"),
    "set_controller_config_vel_limit": ("axis", "controller.config.vel_limit"),
    "set_controller_config_vel_limit_tolerance": ("axis", "controller.config.vel_limit_tolerance"),
    "set_controller_config_vel_ramp_rate": ("axis", "controller.config.vel_ramp_rate"),
    "set_motor_config_current_hard_max": ("axis", "config.motor.current_hard_max"),
    "set_motor_config_current_soft_max": ("axis", "config.motor.current_soft_max"),
    "set_motor_config_direction": ("axis", "config.motor.direction"),
    "set_motor_config_motor_type": ("axis", "config.motor.motor_type"),
    "set_motor_config_phase_inductance": ("axis", "config.motor.phase_inductance"),
    "set_motor_config_phase_resistance": ("axis", "config.motor.phase_resistance"),
    "set_motor_config_pole_pairs": ("axis", "config.motor.pole_pairs"),
    "set_motor_config_torque_constant": ("axis", "config.motor.torque_constant"),
    "set_position": ("axis", "controller.input_pos"),
    "set_torque": ("axis", "controller.input_torque"),
    "set_traptraj_config_accel_limit": ("axis", "trap_traj.config.accel_limit"),
    "set_traptraj_config_decel_limit": ("axis", "trap_traj.config.decel_limit"),
    "set_traptraj_config_vel_limit": ("axis", "trap_traj.config.vel_limit"),
    "set_velocity": ("axis", "controller.input_vel"),
}

# command name -> (root, dotted_path_to_callable, ordered_arg_names) for real method calls
# (zero-arg actions like clear_errors, and multi-arg ones like move_incremental).
_METHODS: Dict[str, Tuple[str, str, List[str]]] = {
    "clear_errors": ("odrv", "clear_errors", []),
    "encoder_onboard0_get_field_strength": ("odrv", "onboard_encoder0.get_field_strength", []),
    "erase_configuration": ("odrv", "erase_configuration", []),
    "move_incremental": ("axis", "controller.move_incremental", ['delta', 'from_goal_point']),
    "reboot": ("odrv", "reboot", []),
    "save_configuration": ("odrv", "save_configuration", []),
    "set_abs_pos": ("axis", "set_abs_pos", ['pos']),
    "watchdog_feed": ("axis", "watchdog_feed", []),
}

# set_axis_state/set_control_mode aren't in _SETTERS (they need enum-name
# resolution, not a plain setattr - see execute()) or _METHODS (they're not
# real ODrive methods) - carved out here purely so the coverage check below
# can still account for them.
_SPECIAL_COMMANDS = {"set_axis_state", "set_control_mode"}


def _validate_channel_coverage() -> None:
    """Runs once at import time. TELEMETRY_CHANNELS/COMMAND_CHANNELS (the
    declared contract in odrive_channels.py, shared with MockOdriveBackend)
    and the path tables above (this backend's own private implementation of
    that contract) are two separate structures that must agree exactly -
    every declared channel needs a matching table entry, and vice versa.
    Nothing enforced that automatically before; a channel added to one and
    forgotten in the other would silently produce an incomplete telemetry
    frame instead of failing. This is the static, always-on equivalent of
    what TelemetryClient.verify_channels()/CommandClient.verify_actions() do
    for a *live* backend - applied here to this module's own internal
    tables instead of a running process."""
    declared_telemetry = set(TELEMETRY_CHANNELS)
    implemented_telemetry = set(_TELEMETRY_PATHS)
    if declared_telemetry != implemented_telemetry:
        raise AssertionError(
            "_TELEMETRY_PATHS is out of sync with TELEMETRY_CHANNELS - "
            f"missing: {sorted(declared_telemetry - implemented_telemetry)}, "
            f"extra: {sorted(implemented_telemetry - declared_telemetry)}"
        )

    declared_commands = set(COMMAND_CHANNELS)
    implemented_commands = set(_SETTERS) | set(_METHODS) | _SPECIAL_COMMANDS
    if declared_commands != implemented_commands:
        raise AssertionError(
            "_SETTERS/_METHODS is out of sync with COMMAND_CHANNELS - "
            f"missing: {sorted(declared_commands - implemented_commands)}, "
            f"extra: {sorted(implemented_commands - declared_commands)}"
        )


_validate_channel_coverage()


def _get_path(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _set_path(obj: Any, path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    for part in parents:
        obj = getattr(obj, part)
    setattr(obj, leaf, value)


def _call_path(obj: Any, path: str, *args: Any) -> Any:
    *parents, leaf = path.split(".")
    for part in parents:
        obj = getattr(obj, part)
    return getattr(obj, leaf)(*args)


class OdriveBackend(HardwareBackend):
    """Real ODrive backend over USB, via the official `odrive` package. Firmware 0.6.x (Pro/S1)."""

    device = DEVICE_ODRIVE
    sample_interval_s = SAMPLE_INTERVAL_S

    def __init__(
        self,
        serial_number: Optional[str] = None,
        discovery_timeout_s: float = DEFAULT_DISCOVERY_TIMEOUT_S,
    ) -> None:
        self._serial_number = serial_number
        self._discovery_timeout_s = discovery_timeout_s
        self._odrv = None  # the odrive package's device handle, once connected
        self._AxisState = None
        self._ControlMode = None
        self._last_watched: Dict[str, Any] = {}
        """Last seen value of each channel in odrive_errors.WATCHED_CHANNELS, so
        error logging can fire on change rather than on every frame."""

    async def connect(self) -> None:
        # Connecting twice is the normal path, not a caller error: runner.run()
        # connects when the driver process starts, and a client then sends
        # `connect` over the wire, as every testbed here does. Re-running
        # discovery is not merely wasteful - it calls find_any() in a worker
        # thread while stream_samples() is still reading channels off the
        # existing handle, and two concurrent users of the same USB device wedge
        # it. Observed on a real ODrive Pro (fw 0.6.12): the second connect
        # completed, telemetry then stopped permanently, and the testbed blocked
        # forever waiting for a frame that never came.
        if self.is_connected:
            logger.debug(
                "already connected to ODrive serial_number=%s, ignoring redundant connect",
                getattr(self._odrv, "serial_number", None),
            )
            return

        try:
            import odrive
            from odrive.enums import AxisState, ControlMode
        except ImportError as exc:
            raise HardwareError(
                "the 'odrive' package isn't installed - run 'uv sync' "
                "(or 'pip install odrive') to talk to real ODrive hardware"
            ) from exc

        self._AxisState = AxisState
        self._ControlMode = ControlMode
        logger.info(
            "discovering ODrive over USB (serial_number=%s, timeout=%.1fs)",
            self._serial_number, self._discovery_timeout_s,
        )
        # find_any() blocks on USB enumeration - run it off the event loop so
        # it doesn't stall the command/telemetry servers sharing this process.
        # It also blocks *indefinitely* with no timeout arg, so a missing/
        # unpowered/wrong-serial-number device would otherwise hang connect()
        # (and whoever's awaiting the "connect" command) forever.
        try:
            self._odrv = await asyncio.to_thread(
                odrive.find_any, serial_number=self._serial_number, timeout=self._discovery_timeout_s
            )
        except Exception as exc:
            raise HardwareError(
                f"no ODrive found within {self._discovery_timeout_s:.1f}s "
                f"(serial_number={self._serial_number!r}) - check USB connection and power"
            ) from exc
        logger.info("connected to ODrive serial_number=%s", getattr(self._odrv, "serial_number", None))
        await asyncio.to_thread(self._verify_declared_channels_exist)

    def _verify_declared_channels_exist(self) -> None:
        """Confirm every declared channel resolves on this device, raising
        MissingChannelError naming the ones that don't.

        Probed once at connect, because "not present" is a structural fact only
        this process can tell apart from "no value at this instant" - emptiness
        is not a usable signal, since a test-published state channel is
        legitimately blank until something sets it.

        Setters and methods are probed by resolving their parent and checking
        the leaf with hasattr, never by writing or calling: a health check must
        not have side effects on real hardware.
        """
        missing = []
        for name, (root, path) in sorted(_TELEMETRY_PATHS.items()):
            if not self._path_exists(root, path):
                missing.append((name, root, path))
        for name, (root, path) in sorted(_SETTERS.items()):
            if not self._path_exists(root, path):
                missing.append((name, root, path))
        for name, entry in sorted(_METHODS.items()):
            if not self._path_exists(entry[0], entry[1]):
                missing.append((name, entry[0], entry[1]))

        if not missing:
            logger.info(
                "verified all %d declared channels exist on this device",
                len(_TELEMETRY_PATHS) + len(_SETTERS) + len(_METHODS),
            )
            return

        detail = "\n".join(
            f"  {name} -> odrv0{'.axis0' if root == 'axis' else ''}.{path}" for name, root, path in missing
        )
        raise MissingChannelError(
            f"{len(missing)} declared channel(s) do not exist on this ODrive "
            f"(serial_number={getattr(self._odrv, 'serial_number', None)}, fw {self._firmware_version()}):\n"
            f"{detail}\n"
            "Either this board genuinely lacks the hardware (e.g. no brake resistor fitted), or the "
            "attribute path is wrong for this firmware. Fix the path, or remove the channel from "
            "hardware/odrive/odrive_channels.py and its table entry here - but do not leave it declared, "
            "because a declared-but-absent channel records nothing while looking present."
        )

    def _path_exists(self, root: str, path: str) -> bool:
        """Whether a dotted attribute path resolves on this device, walking
        intermediates so a missing parent is caught as cleanly as a missing
        leaf. Read-only: never assigns, never calls."""
        obj = self._odrv.axis0 if root == "axis" else self._odrv
        parts = path.split(".")
        for part in parts[:-1]:
            if not hasattr(obj, part):
                return False
            obj = getattr(obj, part)
        return hasattr(obj, parts[-1])

    def _firmware_version(self) -> str:
        try:
            return (
                f"{self._odrv.fw_version_major}.{self._odrv.fw_version_minor}."
                f"{self._odrv.fw_version_revision}"
            )
        except AttributeError:
            return "unknown"

    async def disconnect(self) -> None:
        if self._odrv is not None:
            logger.info("disconnecting from ODrive serial_number=%s", getattr(self._odrv, "serial_number", None))
            await self._set_axis_state("IDLE")
            self._odrv = None

    async def get_status(self) -> dict:
        self._require_connected()
        axis = self._odrv.axis0
        return {
            "connected": True,
            "serial_number": getattr(self._odrv, "serial_number", None),
            "axis_state": int(axis.current_state),
            "control_mode": int(axis.controller.config.control_mode),
        }

    async def execute(self, action: str, **params: Any) -> Any:
        self._require_connected()
        if action == "set_axis_state":
            return await self._set_axis_state(params["state"])
        if action == "set_control_mode":
            return await self._set_control_mode(params["mode"])
        if action in _SETTERS:
            root, path = _SETTERS[action]
            obj = self._odrv.axis0 if root == "axis" else self._odrv
            return await asyncio.to_thread(_set_path, obj, path, params["value"])
        if action in _METHODS:
            root, path, arg_names = _METHODS[action]
            obj = self._odrv.axis0 if root == "axis" else self._odrv
            args = [params[name] for name in arg_names]
            return await asyncio.to_thread(_call_path, obj, path, *args)
        raise HardwareError(f"unknown action: {action}")

    async def list_actions(self) -> List[str]:
        return list(COMMAND_CHANNELS)

    async def stream_samples(self) -> AsyncIterator[dict]:
        while True:
            if self._odrv is not None:
                frame = await asyncio.to_thread(self._read_all_channels)
                self._log_error_transitions(frame)
                yield frame
            await asyncio.sleep(SAMPLE_INTERVAL_S)

    def _log_error_transitions(self, frame: Dict[str, Any]) -> None:
        """Log a decoded line whenever a watched error or state channel changes.

        Edge-triggered, not level-triggered. A standing fault would otherwise
        log at the frame rate and bury everything else, so this fires once when
        the value appears and once when it clears - which is also the shape a
        person reads a log in: what changed, and when.

        The value of doing this in the driver at all is that the raw number is
        the only thing the recorded telemetry can carry. `active_errors` = 1056
        in a CSV needs a lookup; `DRV_FAULT | MOTOR_FAILED` in the log beside it
        does not. See hardware/odrive/odrive_errors.py.

        Deliberately never raises: a decode problem must not take down the
        telemetry stream, which runner.py would rightly treat as a device
        failure."""
        try:
            for channel in odrive_errors.WATCHED_CHANNELS:
                if channel not in frame:
                    continue
                current = frame[channel]
                previous = self._last_watched.get(channel, _UNSET)
                if previous == current:
                    continue
                self._last_watched[channel] = current
                if previous is _UNSET:
                    # First frame: report only what is already wrong, so a clean
                    # start does not announce eight channels reading zero.
                    if odrive_errors.is_fault(channel, current):
                        logger.warning(
                            "%s is already set at startup: %s (%s)",
                            channel, current, odrive_errors.describe(channel, current),
                        )
                    continue
                line = odrive_errors.format_transition(channel, previous, current)
                if odrive_errors.is_fault(channel, current):
                    logger.warning("ODrive fault: %s", line)
                elif odrive_errors.is_fault(channel, previous):
                    logger.info("ODrive fault cleared: %s", line)
                else:
                    logger.info("ODrive %s", line)
        except Exception:
            logger.exception("failed to log an ODrive error transition, continuing to stream")

    def _read_all_channels(self) -> dict:
        axis = self._odrv.axis0
        odrv = self._odrv
        result = {}
        for name, (root, path) in _TELEMETRY_PATHS.items():
            obj = axis if root == "axis" else odrv
            # AttributeError is deliberately not caught: connect() has already
            # probed every declared path, so absence is a setup-time error. If
            # one appears here the device's attribute graph changed mid-run, and
            # runner.py treating a raising stream_samples() as fatal is correct.
            result[name] = to_jsonable(_get_path(obj, path))
        return result

    async def _set_axis_state(self, state: str) -> None:
        if state not in _AXIS_STATE_NAMES:
            raise HardwareError(f"unknown axis state: {state}")
        await asyncio.to_thread(setattr, self._odrv.axis0, "requested_state", getattr(self._AxisState, state))

    async def _set_control_mode(self, mode: str) -> None:
        if mode not in _CONTROL_MODE_NAMES:
            raise HardwareError(f"unknown control mode: {mode}")
        await asyncio.to_thread(
            setattr, self._odrv.axis0.controller.config, "control_mode", getattr(self._ControlMode, mode)
        )

    @property
    def is_connected(self) -> bool:
        """Connection state is the device handle itself, not a flag."""
        return self._odrv is not None
