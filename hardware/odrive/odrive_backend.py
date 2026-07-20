"""Real ODrive backend - talks to a physical ODrive over USB via the
official `odrive` Python package (firmware 0.6.x, Pro/S1). See
mock_backend.py for the no-hardware-needed version.

Requires the optional `odrive` extra (`uv sync --extra odrive` / `pip
install odrive`) for actual USB communication. That import is deferred
into connect() (not module load time), so importing this module - or
running main.py with --mock - never needs the package installed; only
connecting to real hardware does.

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
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Tuple

from ..backend import HardwareBackend, HardwareError
from .odrive_channels import COMMAND_CHANNELS, TELEMETRY_CHANNELS

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_S = 0.05  # 20 Hz - each read is a real USB round-trip per channel, unlike the mocks' in-memory state; tune against real latency once hardware is in hand
DEFAULT_DISCOVERY_TIMEOUT_S = 10.0  # how long connect() waits for odrive.find_any() to see a matching device

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
    "board_brake_resistor0_chopper_temp": ("odrv", "brake_resistor0.chopper_temp"),
    "board_brake_resistor0_current": ("odrv", "brake_resistor0.current"),
    "board_brake_resistor0_duty": ("odrv", "brake_resistor0.duty"),
    "board_brake_resistor0_is_armed": ("odrv", "brake_resistor0.is_armed"),
    "board_brake_resistor0_was_saturated": ("odrv", "brake_resistor0.was_saturated"),
    "board_config_dc_bus_overvoltage_trip_level": ("odrv", "config.dc_bus_overvoltage_trip_level"),
    "board_config_dc_bus_undervoltage_trip_level": ("odrv", "config.dc_bus_undervoltage_trip_level"),
    "board_config_dc_max_negative_current": ("odrv", "config.dc_max_negative_current"),
    "board_config_dc_max_positive_current": ("odrv", "config.dc_max_positive_current"),
    "board_config_inverter0_current_hard_max": ("odrv", "config.inverter0.current_hard_max"),
    "board_config_inverter0_current_soft_max": ("odrv", "config.inverter0.current_soft_max"),
    "board_config_inverter0_current_soft_max_derated": ("odrv", "config.inverter0.current_soft_max_derated"),
    "board_config_inverter0_derating_start": ("odrv", "config.inverter0.derating_start"),
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
    "encoder_onboard0_config_field_check_mode": ("odrv", "onboard_encoder0.config.field_check_mode"),
    "encoder_onboard0_field_status": ("odrv", "onboard_encoder0.field_status"),
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
    "total_charge_used": ("axis", "total_charge_used"),
    "total_power_used": ("axis", "total_power_used"),
    "traptraj_config_accel_limit": ("axis", "trap_traj.config.accel_limit"),
    "traptraj_config_decel_limit": ("axis", "trap_traj.config.decel_limit"),
    "traptraj_config_vel_limit": ("axis", "trap_traj.config.vel_limit"),
    "vel_estimate": ("axis", "vel_estimate"),
}

# command name -> (root, dotted_path) for plain setattr(value) commands. Excludes set_axis_state/
# set_control_mode (handled specially in execute() - see module docstring) and anything in _METHODS.
_SETTERS: Dict[str, Tuple[str, str]] = {
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
    "set_board_config_inverter0_current_soft_max_derated": ("odrv", "config.inverter0.current_soft_max_derated"),
    "set_board_config_inverter0_derating_start": ("odrv", "config.inverter0.derating_start"),
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
    "set_controller_config_vel_ramp_rate": ("axis", "controller.config.vel_ramp_rate"),
    "set_encoder_onboard0_config_field_check_mode": ("odrv", "onboard_encoder0.config.field_check_mode"),
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


def _to_jsonable(value: Any) -> Any:
    """Coerce a raw fibre RPC value to something json.dumps can serialize.
    Real fibre properties are normally already plain float/int/bool/str,
    but enum-like values that slip through as non-primitive objects get
    cast to int (or str as a last resort) rather than crashing the
    telemetry server's JSON encode."""
    if value is None or isinstance(value, (int, float, bool, str)):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


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
        self._warned_missing_channels: Set[str] = set()

    async def connect(self) -> None:
        try:
            import odrive
            from odrive.enums import AxisState, ControlMode
        except ImportError as exc:
            raise HardwareError(
                "the 'odrive' package isn't installed - install the optional extra "
                "(uv sync --extra odrive / pip install odrive) to talk to real ODrive hardware"
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
                yield await asyncio.to_thread(self._read_all_channels)
            await asyncio.sleep(SAMPLE_INTERVAL_S)

    def _read_all_channels(self) -> dict:
        axis = self._odrv.axis0
        odrv = self._odrv
        result = {}
        for name, (root, path) in _TELEMETRY_PATHS.items():
            obj = axis if root == "axis" else odrv
            try:
                result[name] = _to_jsonable(_get_path(obj, path))
            except AttributeError as exc:
                # Only AttributeError is treated as benign - a channel this
                # particular hardware config doesn't have (e.g. a different
                # encoder type than onboard_encoder0). Anything else (a real
                # connection loss, etc.) propagates and is NOT caught here -
                # see runner.py, which treats stream_samples() raising as
                # fatal and shuts the process down loudly rather than letting
                # telemetry silently go quiet.
                result[name] = None
                if name not in self._warned_missing_channels:
                    self._warned_missing_channels.add(name)
                    logger.warning(
                        "channel %r (odrv0%s.%s) not present on this device - reporting None from "
                        "now on; this either means the hardware config genuinely doesn't have it, "
                        "or the attribute path is wrong (%s)",
                        name, ".axis0" if root == "axis" else "", path, exc,
                    )
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

    def _require_connected(self) -> None:
        if self._odrv is None:
            raise HardwareError("backend not connected")
