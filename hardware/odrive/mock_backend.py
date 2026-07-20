"""Simulated ODrive backend for local development without real
hardware attached.

Every channel in odrive_channels.py is present in each telemetry
frame, but only a small "hero" set has real simulated dynamics:
position/velocity/torque, axis state, control mode, measured current,
torque estimate, DC bus voltage/current, FET temperature (see
_step_physics()) - same spirit as MockDutBackend. Everything else
(config setpoints, limits, startup-sequence flags, ...) is a static
placeholder that only changes if you explicitly set it via the
matching `set_<channel>` command - there's nothing physically
meaningful to simulate for most of these.

Not a physically rigorous model, same as every other mock here: it
proves the framework's plumbing works, not real motor behavior.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, AsyncIterator, Dict, List

from ..backend import HardwareBackend, HardwareError
from .odrive_channels import COMMAND_CHANNELS, TELEMETRY_CHANNELS

SAMPLE_INTERVAL_S = 0.02  # 50 Hz - matches the other mocks; no real USB latency to approximate here

_AXIS_STATE_IDLE = 1
_AXIS_STATE_CLOSED_LOOP_CONTROL = 8
_AXIS_STATES = {"IDLE": _AXIS_STATE_IDLE, "CLOSED_LOOP_CONTROL": _AXIS_STATE_CLOSED_LOOP_CONTROL}
_CONTROL_MODES = ("POSITION_CONTROL", "VELOCITY_CONTROL", "TORQUE_CONTROL")

# Static placeholder value for every declared telemetry channel, seeded once at
# construction into self._config. Derived heuristically from each channel's own
# declared type/description in odrive_channels.py (bool -> False, int-ish ->
# 0, enum/state/mode -> 0, everything else -> 0.0) - exact values don't matter
# for channels nothing in this mock ever computes; only their *presence* does.
DEFAULTS: Dict[str, Any] = {
    "axis_current_state": 0,  # overwritten by the physics tick each sample - see stream_samples()
    "axis_procedure_result": 0,
    "axis_is_homed": False,
    "axis_is_armed": False,
    "pos_estimate": 0.0,  # overwritten by the physics tick each sample - see stream_samples()
    "vel_estimate": 0.0,  # overwritten by the physics tick each sample - see stream_samples()
    "active_errors": 0,  # overwritten by the physics tick each sample - see stream_samples()
    "disarm_reason": 0,  # overwritten by the physics tick each sample - see stream_samples()
    "detailed_disarm_reason": 0,
    "last_drv_fault": 0,
    "total_charge_used": 0.0,
    "total_power_used": 0.0,
    "axis_config_startup_motor_calibration": False,
    "axis_config_startup_encoder_index_search": False,
    "axis_config_startup_encoder_offset_calibration": False,
    "axis_config_startup_closed_loop_control": False,
    "axis_config_startup_homing": False,
    "axis_config_startup_max_wait_for_ready": 0.0,
    "axis_config_watchdog_timeout": 0.0,
    "axis_config_enable_watchdog": False,
    "axis_config_load_encoder": 0,
    "axis_config_commutation_encoder": 0,
    "axis_config_i_bus_hard_min": 0.0,
    "axis_config_i_bus_hard_max": 0.0,
    "axis_config_i_bus_soft_min": 0.0,
    "axis_config_i_bus_soft_max": 0.0,
    "axis_config_p_bus_soft_min": 0.0,
    "axis_config_p_bus_soft_max": 0.0,
    "axis_config_torque_soft_min": 0.0,
    "axis_config_torque_soft_max": 0.0,
    "motor_foc_iq_measured": 0.0,  # overwritten by the physics tick each sample - see stream_samples()
    "motor_foc_id_measured": 0.0,
    "motor_torque_estimate": 0.0,  # overwritten by the physics tick each sample - see stream_samples()
    "motor_mechanical_power": 0.0,
    "motor_electrical_power": 0.0,
    "motor_loss_power": 0.0,
    "motor_effective_current_lim": 0,
    "motor_fet_thermistor_temperature": 0.0,  # overwritten by the physics tick each sample - see stream_samples()
    "motor_motor_thermistor_temperature": 0.0,
    "motor_config_motor_type": 0,
    "motor_config_pole_pairs": 0,
    "motor_config_direction": 0,
    "motor_config_phase_resistance": 0.0,
    "motor_config_phase_inductance": 0.0,
    "motor_config_torque_constant": 0.0,
    "motor_config_current_soft_max": 0.0,
    "motor_config_current_hard_max": 0.0,
    "posvelmapper_status": 0,
    "commutmapper_status": 0,
    "encoder_onboard0_status": 0,
    "encoder_onboard0_field_status": 0,
    "encoder_onboard0_raw": 0.0,
    "encoder_onboard0_config_field_check_mode": 0.0,
    "controller_pos_setpoint": 0.0,  # overwritten by the physics tick each sample - see stream_samples()
    "controller_vel_setpoint": 0.0,  # overwritten by the physics tick each sample - see stream_samples()
    "controller_torque_setpoint": 0.0,  # overwritten by the physics tick each sample - see stream_samples()
    "controller_effective_torque_setpoint": 0,
    "controller_trajectory_done": False,
    "controller_vel_integrator_torque": 0,
    "controller_spinout_mechanical_power": 0,
    "controller_spinout_electrical_power": 0,
    "controller_input_pos": 0,  # overwritten by the physics tick each sample - see stream_samples()
    "controller_input_vel": 0,  # overwritten by the physics tick each sample - see stream_samples()
    "controller_input_torque": 0,  # overwritten by the physics tick each sample - see stream_samples()
    "controller_config_control_mode": 0,  # overwritten by the physics tick each sample - see stream_samples()
    "controller_config_input_mode": 0,
    "controller_config_pos_gain": 0.0,
    "controller_config_vel_gain": 0.0,
    "controller_config_vel_integrator_gain": 0.0,
    "controller_config_vel_integrator_limit": 0,
    "controller_config_vel_limit": 0.0,
    "controller_config_enable_vel_limit": False,
    "controller_config_enable_torque_mode_vel_limit": False,
    "controller_config_enable_overspeed_error": False,
    "controller_config_vel_ramp_rate": 0,
    "controller_config_torque_ramp_rate": 0,
    "controller_config_circular_setpoints": False,
    "controller_config_circular_setpoint_range": 0.0,
    "controller_config_absolute_setpoints": False,
    "controller_config_homing_speed": 0.0,
    "controller_config_input_filter_bandwidth": 0.0,
    "controller_config_spinout_mechanical_power_bandwidth": 0.0,
    "controller_config_spinout_electrical_power_bandwidth": 0.0,
    "controller_config_spinout_mechanical_power_threshold": 0.0,
    "controller_config_spinout_electrical_power_threshold": 0.0,
    "traptraj_config_vel_limit": 0.0,
    "traptraj_config_accel_limit": 0.0,
    "traptraj_config_decel_limit": 0.0,
    "minendstop_state": False,
    "maxendstop_state": False,
    "board_vbus_voltage": 0.0,  # overwritten by the physics tick each sample - see stream_samples()
    "board_ibus": 0.0,  # overwritten by the physics tick each sample - see stream_samples()
    "board_serial_number": 0,
    "board_brake_resistor0_current": 0.0,
    "board_brake_resistor0_duty": 0.0,
    "board_brake_resistor0_chopper_temp": 0.0,
    "board_brake_resistor0_is_armed": False,
    "board_brake_resistor0_was_saturated": False,
    "board_config_dc_bus_undervoltage_trip_level": 0.0,
    "board_config_dc_bus_overvoltage_trip_level": 0.0,
    "board_config_dc_max_positive_current": 0.0,
    "board_config_dc_max_negative_current": 0.0,
    "board_config_max_regen_current": 0.0,
    "board_config_inverter0_current_soft_max": 0.0,
    "board_config_inverter0_current_hard_max": 0.0,
    "board_config_inverter0_temp_limit_lower": 0.0,
    "board_config_inverter0_temp_limit_upper": 0.0,
    "board_config_inverter0_derating_start": 0.0,
    "board_config_inverter0_current_soft_max_derated": 0.0,
    "debug_mcu_temperature": 0.0,
}


class MockOdriveBackend(HardwareBackend):
    """Simulated ODrive axis - position/velocity/torque control, no real hardware needed."""

    def __init__(self) -> None:
        self._connected = False
        self._config: Dict[str, Any] = dict(DEFAULTS)
        self._axis_state = _AXIS_STATE_IDLE
        self._control_mode = "POSITION_CONTROL"
        self._input_pos = 0.0
        self._input_vel = 0.0
        self._input_torque = 0.0
        self._pos_estimate = 0.0
        self._vel_estimate = 0.0
        self._active_errors = 0
        self._disarm_reason = 0

    async def connect(self) -> None:
        await asyncio.sleep(0.05)  # pretend there's a USB handshake
        self._connected = True

    async def disconnect(self) -> None:
        self._axis_state = _AXIS_STATE_IDLE
        self._connected = False

    async def get_status(self) -> dict:
        return {
            "connected": self._connected,
            "axis_state": self._axis_state,
            "control_mode": self._control_mode,
        }

    async def execute(self, action: str, **params: Any) -> Any:
        self._require_connected()

        if action == "set_axis_state":
            state = params["state"]
            if state not in _AXIS_STATES:
                raise HardwareError(f"unknown axis state: {state}")
            self._axis_state = _AXIS_STATES[state]
            return None
        if action == "set_control_mode":
            mode = params["mode"]
            if mode not in _CONTROL_MODES:
                raise HardwareError(f"unknown control mode: {mode}")
            self._control_mode = mode
            return None
        if action == "set_position":
            self._input_pos = params["value"]
            return None
        if action == "set_velocity":
            self._input_vel = params["value"]
            return None
        if action == "set_torque":
            self._input_torque = params["value"]
            return None
        if action == "clear_errors":
            self._active_errors = 0
            self._disarm_reason = 0
            return None

        # Real methods with no physically-meaningful mock behavior - accept
        # and no-op (or return a harmless placeholder), rather than raising,
        # so a test exercising the full command surface doesn't need to know
        # which channels are "real" physics vs. plumbing-only in the mock.
        if action == "set_abs_pos":
            self._pos_estimate = params["pos"]
            return None
        if action == "watchdog_feed":
            return None
        if action in ("save_configuration", "erase_configuration", "reboot"):
            return None
        if action == "encoder_onboard0_get_field_strength":
            return 0.5
        if action == "move_incremental":
            self._input_pos += params["delta"]
            return None

        if action.startswith("set_") and action[len("set_"):] in self._config:
            self._config[action[len("set_"):]] = params["value"]
            return None

        raise HardwareError(f"unknown action: {action}")

    async def list_actions(self) -> List[str]:
        return list(COMMAND_CHANNELS)

    def _require_connected(self) -> None:
        if not self._connected:
            raise HardwareError("backend not connected")

    async def stream_samples(self) -> AsyncIterator[dict]:
        while True:
            self._step_physics()
            yield {**self._config, **self._hero_channels()}
            await asyncio.sleep(SAMPLE_INTERVAL_S)

    def _step_physics(self) -> None:
        """First-order-ish position/velocity approximation, same spirit as
        MockDutBackend - only runs while CLOSED_LOOP_CONTROL is engaged."""
        if self._axis_state == _AXIS_STATE_CLOSED_LOOP_CONTROL:
            if self._control_mode == "POSITION_CONTROL":
                error = self._input_pos - self._pos_estimate
                self._vel_estimate += (error * 4.0 - self._vel_estimate) * 0.1
            elif self._control_mode == "VELOCITY_CONTROL":
                self._vel_estimate += (self._input_vel - self._vel_estimate) * 0.2
            else:  # TORQUE_CONTROL
                self._vel_estimate += self._input_torque * SAMPLE_INTERVAL_S * 2.0
            self._pos_estimate += self._vel_estimate * SAMPLE_INTERVAL_S

    def _hero_channels(self) -> Dict[str, Any]:
        iq_measured = abs(self._vel_estimate) * 0.3 + abs(self._input_torque) * 0.5
        iq_measured += abs(random.gauss(0, 0.02))
        torque_estimate = iq_measured * self._config.get("motor_config_torque_constant", 0.0) or iq_measured * 0.05
        vbus_voltage = 24.0 + random.gauss(0, 0.05)
        ibus = iq_measured * 0.7 if self._axis_state == _AXIS_STATE_CLOSED_LOOP_CONTROL else 0.0
        return {
            "axis_current_state": self._axis_state,
            "pos_estimate": self._pos_estimate,
            "vel_estimate": self._vel_estimate,
            "active_errors": self._active_errors,
            "disarm_reason": self._disarm_reason,
            "controller_input_pos": self._input_pos,
            "controller_input_vel": self._input_vel,
            "controller_input_torque": self._input_torque,
            "controller_config_control_mode": self._control_mode,
            "controller_pos_setpoint": self._input_pos,
            "controller_vel_setpoint": self._input_vel,
            "controller_torque_setpoint": self._input_torque,
            "motor_foc_iq_measured": iq_measured,
            "motor_torque_estimate": torque_estimate,
            "motor_fet_thermistor_temperature": 30.0 + iq_measured * 2.0,
            "board_vbus_voltage": vbus_voltage,
            "board_ibus": ibus,
        }
