"""ODrive-specific convenience methods layered on the generic
CommandClient - one method per channel in odrive_channels.py's
COMMAND_CHANNELS. Each docstring below is the real ODrive attribute/
method path the call reaches (`axis0...`/`odrv0...`), so you can look
it up in ODrive's own docs for more detail than the one-liner gives.
"""
from __future__ import annotations

from ..clients.command_client import CommandClient
from protocol.wire import DEFAULT_ODRIVE_COMMAND_ENDPOINT


class OdriveCommandClient(CommandClient):
    """CommandClient with named sugar for every declared ODrive command channel."""

    def __init__(self, endpoint: str = DEFAULT_ODRIVE_COMMAND_ENDPOINT, timeout_ms: int = 5000):
        super().__init__(endpoint, timeout_ms)

    def set_axis_state(self, state: str) -> None:
        """axis0.requested_state - requests a new AxisState (e.g. "IDLE", "CLOSED_LOOP_CONTROL")"""
        self.execute("set_axis_state", state=state)

    def watchdog_feed(self) -> None:
        """axis0.watchdog_feed() - feeds the watchdog to prevent a watchdog timeout"""
        self.execute("watchdog_feed")

    def set_abs_pos(self, pos: float) -> None:
        """axis0.set_abs_pos(pos) - override the axis's absolute position, turns"""
        self.execute("set_abs_pos", pos=pos)

    def clear_errors(self) -> None:
        """odrv0.clear_errors() - clears active_errors/disarm_reason so the axis can re-arm"""
        self.execute("clear_errors")

    def move_incremental(self, delta: float, from_goal_point: bool = False) -> None:
        """axis0.controller.move_incremental(delta, from_goal_point) - relative position move"""
        self.execute("move_incremental", delta=delta, from_goal_point=from_goal_point)

    def set_axis_config_startup_motor_calibration(self, value) -> None:
        """axis0.config.startup_motor_calibration - bool"""
        self.execute("set_axis_config_startup_motor_calibration", value=value)

    def set_axis_config_startup_encoder_index_search(self, value) -> None:
        """axis0.config.startup_encoder_index_search - bool"""
        self.execute("set_axis_config_startup_encoder_index_search", value=value)

    def set_axis_config_startup_encoder_offset_calibration(self, value) -> None:
        """axis0.config.startup_encoder_offset_calibration - bool"""
        self.execute("set_axis_config_startup_encoder_offset_calibration", value=value)

    def set_axis_config_startup_closed_loop_control(self, value) -> None:
        """axis0.config.startup_closed_loop_control - bool"""
        self.execute("set_axis_config_startup_closed_loop_control", value=value)

    def set_axis_config_startup_homing(self, value) -> None:
        """axis0.config.startup_homing - bool"""
        self.execute("set_axis_config_startup_homing", value=value)

    def set_axis_config_startup_max_wait_for_ready(self, value) -> None:
        """axis0.config.startup_max_wait_for_ready - s - max wait for active_errors to clear before starting the startup sequence"""
        self.execute("set_axis_config_startup_max_wait_for_ready", value=value)

    def set_axis_config_watchdog_timeout(self, value) -> None:
        """axis0.config.watchdog_timeout - s"""
        self.execute("set_axis_config_watchdog_timeout", value=value)

    def set_axis_config_enable_watchdog(self, value) -> None:
        """axis0.config.enable_watchdog - bool"""
        self.execute("set_axis_config_enable_watchdog", value=value)

    def set_axis_config_load_encoder(self, value) -> None:
        """axis0.config.load_encoder - EncoderId enum - which top-level encoder feeds pos_vel_mapper"""
        self.execute("set_axis_config_load_encoder", value=value)

    def set_axis_config_commutation_encoder(self, value) -> None:
        """axis0.config.commutation_encoder - EncoderId enum - which top-level encoder feeds commutation_mapper"""
        self.execute("set_axis_config_commutation_encoder", value=value)

    def set_axis_config_i_bus_hard_min(self, value) -> None:
        """axis0.config.I_bus_hard_min - A"""
        self.execute("set_axis_config_i_bus_hard_min", value=value)

    def set_axis_config_i_bus_hard_max(self, value) -> None:
        """axis0.config.I_bus_hard_max - A"""
        self.execute("set_axis_config_i_bus_hard_max", value=value)

    def set_axis_config_i_bus_soft_min(self, value) -> None:
        """axis0.config.I_bus_soft_min - A"""
        self.execute("set_axis_config_i_bus_soft_min", value=value)

    def set_axis_config_i_bus_soft_max(self, value) -> None:
        """axis0.config.I_bus_soft_max - A"""
        self.execute("set_axis_config_i_bus_soft_max", value=value)

    def set_axis_config_p_bus_soft_min(self, value) -> None:
        """axis0.config.P_bus_soft_min - W"""
        self.execute("set_axis_config_p_bus_soft_min", value=value)

    def set_axis_config_p_bus_soft_max(self, value) -> None:
        """axis0.config.P_bus_soft_max - W"""
        self.execute("set_axis_config_p_bus_soft_max", value=value)

    def set_axis_config_torque_soft_min(self, value) -> None:
        """axis0.config.torque_soft_min - Nm"""
        self.execute("set_axis_config_torque_soft_min", value=value)

    def set_axis_config_torque_soft_max(self, value) -> None:
        """axis0.config.torque_soft_max - Nm"""
        self.execute("set_axis_config_torque_soft_max", value=value)

    def set_motor_config_motor_type(self, value) -> None:
        """axis0.config.motor.motor_type - MotorType enum (HIGH_CURRENT / GIMBAL / ...)"""
        self.execute("set_motor_config_motor_type", value=value)

    def set_motor_config_pole_pairs(self, value) -> None:
        """axis0.config.motor.pole_pairs - int - magnet pole pairs"""
        self.execute("set_motor_config_pole_pairs", value=value)

    def set_motor_config_direction(self, value) -> None:
        """axis0.config.motor.direction - +-1 - motor spin direction vs. axis space"""
        self.execute("set_motor_config_direction", value=value)

    def set_motor_config_phase_resistance(self, value) -> None:
        """axis0.config.motor.phase_resistance - ohm"""
        self.execute("set_motor_config_phase_resistance", value=value)

    def set_motor_config_phase_inductance(self, value) -> None:
        """axis0.config.motor.phase_inductance - H"""
        self.execute("set_motor_config_phase_inductance", value=value)

    def set_motor_config_torque_constant(self, value) -> None:
        """axis0.config.motor.torque_constant - Nm/A - see module docstring for the 8.27-vs-8.23/Kv documentation discrepancy"""
        self.execute("set_motor_config_torque_constant", value=value)

    def set_motor_config_current_soft_max(self, value) -> None:
        """axis0.config.motor.current_soft_max - A - commanded current limit (motor-level; see board_config_inverter0_current_soft_max for the board-level one)"""
        self.execute("set_motor_config_current_soft_max", value=value)

    def set_motor_config_current_hard_max(self, value) -> None:
        """axis0.config.motor.current_hard_max - A - measured current ceiling -> CURRENT_LIMIT_VIOLATION if exceeded"""
        self.execute("set_motor_config_current_hard_max", value=value)

    def encoder_onboard0_get_field_strength(self) -> None:
        """odrv0.onboard_encoder0.get_field_strength() - reads back the onboard magnetic encoder's field strength magnitude, if the chip exposes it"""
        self.execute("encoder_onboard0_get_field_strength")

    def set_control_mode(self, mode: str) -> None:
        """axis0.controller.config.control_mode - requests a new control mode ("POSITION_CONTROL", "VELOCITY_CONTROL", "TORQUE_CONTROL")"""
        self.execute("set_control_mode", mode=mode)

    def set_position(self, value: float) -> None:
        """axis0.controller.input_pos - position setpoint, turns - only meaningful in POSITION_CONTROL"""
        self.execute("set_position", value=value)

    def set_pos_estimate(self, value: float) -> None:
        """axis0.pos_estimate - re-reference the axis to `value` turns, impulse-free.

        Firmware shifts input_pos and pos_setpoint by the same amount and sets
        absolute_setpoints, so the axis does not move; only what every later
        position MEANS changes. Writing this is how ODrive's docs say to do what
        the deprecated set_abs_pos() does."""
        self.execute("set_pos_estimate", value=value)

    def set_velocity(self, value: float) -> None:
        """axis0.controller.input_vel - velocity setpoint, turns/s - only meaningful in VELOCITY_CONTROL"""
        self.execute("set_velocity", value=value)

    def set_torque(self, value: float) -> None:
        """axis0.controller.input_torque - torque setpoint, Nm - only meaningful in TORQUE_CONTROL"""
        self.execute("set_torque", value=value)

    def set_controller_config_input_mode(self, value) -> None:
        """axis0.controller.config.input_mode - InputMode enum: INACTIVE / PASSTHROUGH / VEL_RAMP / POS_FILTER / TRAP_TRAJ / TORQUE_RAMP / MIRROR / TUNING"""
        self.execute("set_controller_config_input_mode", value=value)

    def set_controller_config_pos_gain(self, value) -> None:
        """axis0.controller.config.pos_gain - position-loop P gain (unit not documented by ODrive - see module docstring)"""
        self.execute("set_controller_config_pos_gain", value=value)

    def set_controller_config_vel_gain(self, value) -> None:
        """axis0.controller.config.vel_gain - velocity-loop P gain (unit not documented)"""
        self.execute("set_controller_config_vel_gain", value=value)

    def set_controller_config_vel_integrator_gain(self, value) -> None:
        """axis0.controller.config.vel_integrator_gain - velocity-loop I gain (unit not documented)"""
        self.execute("set_controller_config_vel_integrator_gain", value=value)

    def set_controller_config_vel_integrator_limit(self, value) -> None:
        """axis0.controller.config.vel_integrator_limit - Nm - integrator output clamp (inf to disable)"""
        self.execute("set_controller_config_vel_integrator_limit", value=value)

    def set_controller_config_vel_limit(self, value) -> None:
        """axis0.controller.config.vel_limit - turns/s - max velocity (inf to disable)"""
        self.execute("set_controller_config_vel_limit", value=value)

    def set_controller_config_vel_limit_tolerance(self, value) -> None:
        """axis0.controller.config.vel_limit_tolerance - multiple of vel_limit at
        which the axis raises an overspeed error"""
        self.execute("set_controller_config_vel_limit_tolerance", value=value)

    def set_controller_config_enable_vel_limit(self, value) -> None:
        """axis0.controller.config.enable_vel_limit - bool"""
        self.execute("set_controller_config_enable_vel_limit", value=value)

    def set_controller_config_enable_torque_mode_vel_limit(self, value) -> None:
        """axis0.controller.config.enable_torque_mode_vel_limit - bool"""
        self.execute("set_controller_config_enable_torque_mode_vel_limit", value=value)

    def set_controller_config_enable_overspeed_error(self, value) -> None:
        """axis0.controller.config.enable_overspeed_error - bool"""
        self.execute("set_controller_config_enable_overspeed_error", value=value)

    def set_controller_config_vel_ramp_rate(self, value) -> None:
        """axis0.controller.config.vel_ramp_rate - turns/s^2 - accel limit for VEL_RAMP input mode"""
        self.execute("set_controller_config_vel_ramp_rate", value=value)

    def set_controller_config_torque_ramp_rate(self, value) -> None:
        """axis0.controller.config.torque_ramp_rate - Nm/s - ramp rate for TORQUE_RAMP input mode"""
        self.execute("set_controller_config_torque_ramp_rate", value=value)

    def set_controller_config_circular_setpoints(self, value) -> None:
        """axis0.controller.config.circular_setpoints - bool"""
        self.execute("set_controller_config_circular_setpoints", value=value)

    def set_controller_config_circular_setpoint_range(self, value) -> None:
        """axis0.controller.config.circular_setpoint_range - turns"""
        self.execute("set_controller_config_circular_setpoint_range", value=value)

    def set_controller_config_absolute_setpoints(self, value) -> None:
        """axis0.controller.config.absolute_setpoints - bool - False = startup-relative frame, True = requires valid pos_estimate"""
        self.execute("set_controller_config_absolute_setpoints", value=value)

    def set_controller_config_homing_speed(self, value) -> None:
        """axis0.controller.config.homing_speed - turns/s - speed toward min_endstop during HOMING"""
        self.execute("set_controller_config_homing_speed", value=value)

    def set_controller_config_input_filter_bandwidth(self, value) -> None:
        """axis0.controller.config.input_filter_bandwidth - 1/s - POS_FILTER input filter bandwidth"""
        self.execute("set_controller_config_input_filter_bandwidth", value=value)

    def set_controller_config_spinout_mechanical_power_bandwidth(self, value) -> None:
        """axis0.controller.config.spinout_mechanical_power_bandwidth - Hz"""
        self.execute("set_controller_config_spinout_mechanical_power_bandwidth", value=value)

    def set_controller_config_spinout_electrical_power_bandwidth(self, value) -> None:
        """axis0.controller.config.spinout_electrical_power_bandwidth - Hz"""
        self.execute("set_controller_config_spinout_electrical_power_bandwidth", value=value)

    def set_controller_config_spinout_mechanical_power_threshold(self, value) -> None:
        """axis0.controller.config.spinout_mechanical_power_threshold - W"""
        self.execute("set_controller_config_spinout_mechanical_power_threshold", value=value)

    def set_controller_config_spinout_electrical_power_threshold(self, value) -> None:
        """axis0.controller.config.spinout_electrical_power_threshold - W"""
        self.execute("set_controller_config_spinout_electrical_power_threshold", value=value)

    def set_traptraj_config_vel_limit(self, value) -> None:
        """axis0.trap_traj.config.vel_limit - turns/s - max planned coast speed, strictly positive"""
        self.execute("set_traptraj_config_vel_limit", value=value)

    def set_traptraj_config_accel_limit(self, value) -> None:
        """axis0.trap_traj.config.accel_limit - turns/s^2 - strictly positive"""
        self.execute("set_traptraj_config_accel_limit", value=value)

    def set_traptraj_config_decel_limit(self, value) -> None:
        """axis0.trap_traj.config.decel_limit - turns/s^2 - strictly positive"""
        self.execute("set_traptraj_config_decel_limit", value=value)

    def set_board_config_dc_bus_undervoltage_trip_level(self, value) -> None:
        """odrv0.config.dc_bus_undervoltage_trip_level - V"""
        self.execute("set_board_config_dc_bus_undervoltage_trip_level", value=value)

    def set_board_config_dc_bus_overvoltage_trip_level(self, value) -> None:
        """odrv0.config.dc_bus_overvoltage_trip_level - V"""
        self.execute("set_board_config_dc_bus_overvoltage_trip_level", value=value)

    def set_board_config_dc_max_positive_current(self, value) -> None:
        """odrv0.config.dc_max_positive_current - A"""
        self.execute("set_board_config_dc_max_positive_current", value=value)

    def set_board_config_dc_max_negative_current(self, value) -> None:
        """odrv0.config.dc_max_negative_current - A"""
        self.execute("set_board_config_dc_max_negative_current", value=value)

    def set_board_config_max_regen_current(self, value) -> None:
        """odrv0.config.max_regen_current - A - regen threshold before the brake resistor shunts"""
        self.execute("set_board_config_max_regen_current", value=value)

    def set_board_config_inverter0_current_soft_max(self, value) -> None:
        """odrv0.config.inverter0.current_soft_max - A - board-level; separate from motor_config_current_soft_max"""
        self.execute("set_board_config_inverter0_current_soft_max", value=value)

    def set_board_config_inverter0_current_hard_max(self, value) -> None:
        """odrv0.config.inverter0.current_hard_max - A"""
        self.execute("set_board_config_inverter0_current_hard_max", value=value)

    def set_board_config_inverter0_temp_limit_lower(self, value) -> None:
        """odrv0.config.inverter0.temp_limit_lower - degC"""
        self.execute("set_board_config_inverter0_temp_limit_lower", value=value)

    def set_board_config_inverter0_temp_limit_upper(self, value) -> None:
        """odrv0.config.inverter0.temp_limit_upper - degC"""
        self.execute("set_board_config_inverter0_temp_limit_upper", value=value)

    def save_configuration(self) -> None:
        """odrv0.save_configuration()"""
        self.execute("save_configuration")

    def erase_configuration(self) -> None:
        """odrv0.erase_configuration()"""
        self.execute("erase_configuration")

    def reboot(self) -> None:
        """odrv0.reboot()"""
        self.execute("reboot")
