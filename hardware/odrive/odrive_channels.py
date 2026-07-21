"""Declared telemetry/command surface for the ODrive axis - the source
of truth both OdriveBackend (real) and MockOdriveBackend (simulated)
implement identically, so a test can't tell which one it's running
against from the channel list alone.

A curated subset of ODrive firmware 0.6.x's full API (not every
documented attribute) - just what a test author would realistically
set/reference, or that would help diagnose a test failure. See
AI/Mytest.md for the full rationale on what was cut and why.

Every writable attribute appears in both lists: its current value in
TELEMETRY_CHANNELS, and `set_<name>` in COMMAND_CHANNELS. Read-only
attributes and pure actions (`clear_errors`, `save_configuration`, ...)
appear in only one. Names are the real dotted attribute path with
`axis0.`/`odrv0.` stripped and `.` -> `_`, prefixed by subsystem
(`motor_`, `controller_`, `board_`, ...).

A few things worth knowing before referencing these in a test:
  - `motor_config_torque_constant`: ODrive's own docs disagree on
    whether to compute this as `8.27/Kv` or `8.23/Kv` - pick one
    deliberately; this file doesn't assume either.
  - `controller_config_pos_gain`/`vel_gain`/`vel_integrator_gain` have
    no documented units - treat as plain tuning values.
  - Only the onboard magnetic encoder's status is exposed
    (`encoder_onboard0_*`) - this backend assumes that built-in
    encoder, not an external incremental/hall/SPI/RS485 one.
"""
from __future__ import annotations

TELEMETRY_CHANNELS = [
    # --- Axis: state machine / error diagnostics (axis0.*, read-only) ---
    "axis_current_state",  # AxisState enum int - current state of the axis
    "axis_procedure_result",  # ProcedureResult enum int - result of the last calibration/procedure
    "axis_is_homed",  # bool - whether the axis has been successfully homed
    "axis_is_armed",  # bool - whether the axis is actively controlling the motor
    "pos_estimate",  # turns - axis position estimate (confirmed direct on axis0, not under .encoder)
    "vel_estimate",  # turns/s - axis velocity estimate
    "active_errors",  # bitmask (Error enum) - currently-active error flags, auto-clears when resolved
    "disarm_reason",  # Error enum int - reason the axis last disarmed, cleared via clear_errors
    "detailed_disarm_reason",  # int - extra detail on disarm_reason, not populated for every error type
    "last_drv_fault",  # int - last gate-driver (DRV chip) fault code
    "total_charge_used",  # Coulombs - total charge drawn from the bus since boot (can be negative, i.e. regen)
    "total_power_used",  # Joules - total energy drawn from the bus since boot (can be negative)

    # --- Axis config (axis0.config.*, settable but also readable - see COMMAND_CHANNELS) ---
    "axis_config_startup_motor_calibration",  # bool
    "axis_config_startup_encoder_index_search",  # bool
    "axis_config_startup_encoder_offset_calibration",  # bool
    "axis_config_startup_closed_loop_control",  # bool
    "axis_config_startup_homing",  # bool
    "axis_config_startup_max_wait_for_ready",  # s - max wait for active_errors to clear before starting the startup sequence
    "axis_config_watchdog_timeout",  # s
    "axis_config_enable_watchdog",  # bool
    "axis_config_load_encoder",  # EncoderId enum - which top-level encoder feeds pos_vel_mapper
    "axis_config_commutation_encoder",  # EncoderId enum - which top-level encoder feeds commutation_mapper
    "axis_config_i_bus_hard_min",  # A
    "axis_config_i_bus_hard_max",  # A
    "axis_config_i_bus_soft_min",  # A
    "axis_config_i_bus_soft_max",  # A
    "axis_config_p_bus_soft_min",  # W
    "axis_config_p_bus_soft_max",  # W
    "axis_config_torque_soft_min",  # Nm
    "axis_config_torque_soft_max",  # Nm

    # --- Motor (axis0.motor.*, read-only telemetry) ---
    "motor_foc_iq_measured",  # A - measured q-axis (torque-producing) current (axis0.motor.foc.Iq_measured)
    "motor_foc_id_measured",  # A - measured d-axis current; should sit near zero for a well-tuned FOC loop
    "motor_torque_estimate",  # Nm - measured current * torque_constant, filtered
    "motor_mechanical_power",  # W - torque * speed
    "motor_electrical_power",  # W - modulation * voltage * current
    "motor_loss_power",  # W - estimated inverter + motor power loss
    "motor_effective_current_lim",  # A - dynamic current limit from motor + inverter constraints (thermal derating, etc.)
    "motor_fet_thermistor_temperature",  # degC - inverter FET temperature
    "motor_motor_thermistor_temperature",  # degC - external motor winding thermistor (ExternalMotorAxis only)

    # --- Motor config (axis0.config.motor.*, RW) ---
    "motor_config_motor_type",  # MotorType enum (HIGH_CURRENT / GIMBAL / ...)
    "motor_config_pole_pairs",  # int - magnet pole pairs
    "motor_config_direction",  # +-1 - motor spin direction vs. axis space
    "motor_config_phase_resistance",  # ohm
    "motor_config_phase_inductance",  # H
    "motor_config_torque_constant",  # Nm/A - see module docstring for the 8.27-vs-8.23/Kv documentation discrepancy
    "motor_config_current_soft_max",  # A - commanded current limit (motor-level; see board_config_inverter0_current_soft_max for the board-level one)
    "motor_config_current_hard_max",  # A - measured current ceiling -> CURRENT_LIMIT_VIOLATION if exceeded

    # --- Feedback estimator health (axis0.pos_vel_mapper / axis0.commutation_mapper) ---
    "posvelmapper_status",  # ComponentStatus enum - is the control-feedback estimator giving valid readings
    "commutmapper_status",  # ComponentStatus enum - is the commutation-feedback estimator giving valid readings

    # --- Onboard encoder (odrv0.onboard_encoder0 - the built-in magnetic encoder; not under axis0, see module docstring) ---
    "encoder_onboard0_status",  # ComponentStatus enum
    "encoder_onboard0_field_status",  # FieldStrengthMonitoring enum - magnet field strength health (e.g. misalignment)
    "encoder_onboard0_raw",  # raw sensor reading, pre-scaling
    "encoder_onboard0_config_field_check_mode",

    # --- Controller (axis0.controller.*, read-only telemetry) ---
    "controller_pos_setpoint",  # turns - post-filter position reference actually used
    "controller_vel_setpoint",  # turns/s - post-filter velocity reference
    "controller_torque_setpoint",  # Nm - post-filter torque reference
    "controller_effective_torque_setpoint",  # Nm - torque actually fed to the motor model
    "controller_trajectory_done",  # bool - trap-traj move complete
    "controller_vel_integrator_torque",  # Nm - accumulated velocity-loop integrator value (windup diagnostic)
    "controller_spinout_mechanical_power",  # W - spinout/stall-detection diagnostic
    "controller_spinout_electrical_power",  # W - spinout/stall-detection diagnostic

    # --- Controller setpoints (RW, also readable) ---
    "controller_input_pos",  # turns - position setpoint (POSITION_CONTROL)
    "controller_input_vel",  # turns/s - velocity setpoint
    "controller_input_torque",  # Nm - torque setpoint

    # --- Controller config (axis0.controller.config.*, RW) ---
    "controller_config_control_mode",  # ControlMode enum: VOLTAGE_CONTROL / TORQUE_CONTROL / VELOCITY_CONTROL / POSITION_CONTROL
    "controller_config_input_mode",  # InputMode enum: INACTIVE / PASSTHROUGH / VEL_RAMP / POS_FILTER / TRAP_TRAJ / TORQUE_RAMP / MIRROR / TUNING
    "controller_config_pos_gain",  # position-loop P gain (unit not documented by ODrive - see module docstring)
    "controller_config_vel_gain",  # velocity-loop P gain (unit not documented)
    "controller_config_vel_integrator_gain",  # velocity-loop I gain (unit not documented)
    "controller_config_vel_integrator_limit",  # Nm - integrator output clamp (inf to disable)
    "controller_config_vel_limit",  # turns/s - max velocity (inf to disable)
    "controller_config_enable_vel_limit",  # bool
    "controller_config_enable_torque_mode_vel_limit",  # bool
    "controller_config_enable_overspeed_error",  # bool
    "controller_config_vel_ramp_rate",  # turns/s^2 - accel limit for VEL_RAMP input mode
    "controller_config_torque_ramp_rate",  # Nm/s - ramp rate for TORQUE_RAMP input mode
    "controller_config_circular_setpoints",  # bool
    "controller_config_circular_setpoint_range",  # turns
    "controller_config_absolute_setpoints",  # bool - False = startup-relative frame, True = requires valid pos_estimate
    "controller_config_homing_speed",  # turns/s - speed toward min_endstop during HOMING
    "controller_config_input_filter_bandwidth",  # 1/s - POS_FILTER input filter bandwidth
    "controller_config_spinout_mechanical_power_bandwidth",  # Hz
    "controller_config_spinout_electrical_power_bandwidth",  # Hz
    "controller_config_spinout_mechanical_power_threshold",  # W
    "controller_config_spinout_electrical_power_threshold",  # W

    # --- Trajectory planner (axis0.trap_traj.config.*, RW) ---
    "traptraj_config_vel_limit",  # turns/s - max planned coast speed, strictly positive
    "traptraj_config_accel_limit",  # turns/s^2 - strictly positive
    "traptraj_config_decel_limit",  # turns/s^2 - strictly positive

    # --- Endstops (axis0.min_endstop / .max_endstop, class SwitchInput) - state only, not wiring config ---
    "minendstop_state",  # bool - True = switch pressed
    "maxendstop_state",  # bool - True = switch pressed

    # --- Board / bus level (odrv0.*, not axis-specific) ---
    "board_vbus_voltage",  # V - DC bus voltage
    "board_ibus",  # A - DC bus current (calculated)
    "board_serial_number",  # int - use hex(...).upper() to match the USB descriptor string; identifies which physical unit produced this data
    "board_brake_resistor0_current",  # A
    "board_brake_resistor0_duty",  # ratio 0-1
    "board_brake_resistor0_chopper_temp",  # degC
    "board_brake_resistor0_is_armed",  # bool
    "board_brake_resistor0_was_saturated",  # bool - brake resistor couldn't dissipate all regen energy
    "board_config_dc_bus_undervoltage_trip_level",  # V
    "board_config_dc_bus_overvoltage_trip_level",  # V
    "board_config_dc_max_positive_current",  # A
    "board_config_dc_max_negative_current",  # A
    "board_config_max_regen_current",  # A - regen threshold before the brake resistor shunts
    "board_config_inverter0_current_soft_max",  # A - board-level; separate from motor_config_current_soft_max
    "board_config_inverter0_current_hard_max",  # A
    "board_config_inverter0_temp_limit_lower",  # degC
    "board_config_inverter0_temp_limit_upper",  # degC
    "board_config_inverter0_derating_start",  # V
    "board_config_inverter0_current_soft_max_derated",  # A
    "debug_mcu_temperature",  # degC - MCU die temperature; ODrive's own docs label this "diagnostics, not for end users", kept anyway since it's cheap and could explain thermal-stress-related odd behavior
]

COMMAND_CHANNELS = [
    # --- Axis-level actions ---
    "set_axis_state",  # requests a new AxisState (e.g. "IDLE", "CLOSED_LOOP_CONTROL")
    "watchdog_feed",  # feeds the watchdog to prevent a watchdog timeout
    "set_abs_pos",  # axis0.set_abs_pos(pos) - override the axis's absolute position, turns
    "clear_errors",  # odrv0.clear_errors() - clears active_errors/disarm_reason so the axis can re-arm
    "move_incremental",  # axis0.controller.move_incremental(delta, from_goal_point) - relative position move

    # --- Axis config setters (mirrors of the RW telemetry channels above, one set_<name> each) ---
    "set_axis_config_startup_motor_calibration",
    "set_axis_config_startup_encoder_index_search",
    "set_axis_config_startup_encoder_offset_calibration",
    "set_axis_config_startup_closed_loop_control",
    "set_axis_config_startup_homing",
    "set_axis_config_startup_max_wait_for_ready",
    "set_axis_config_watchdog_timeout",
    "set_axis_config_enable_watchdog",
    "set_axis_config_load_encoder",
    "set_axis_config_commutation_encoder",
    "set_axis_config_i_bus_hard_min",
    "set_axis_config_i_bus_hard_max",
    "set_axis_config_i_bus_soft_min",
    "set_axis_config_i_bus_soft_max",
    "set_axis_config_p_bus_soft_min",
    "set_axis_config_p_bus_soft_max",
    "set_axis_config_torque_soft_min",
    "set_axis_config_torque_soft_max",

    # --- Motor config setters ---
    "set_motor_config_motor_type",
    "set_motor_config_pole_pairs",
    "set_motor_config_direction",
    "set_motor_config_phase_resistance",
    "set_motor_config_phase_inductance",
    "set_motor_config_torque_constant",
    "set_motor_config_current_soft_max",
    "set_motor_config_current_hard_max",

    # --- Onboard encoder config setter + method ---
    "set_encoder_onboard0_config_field_check_mode",
    "encoder_onboard0_get_field_strength",  # odrv0.onboard_encoder0.get_field_strength() - reads back the onboard magnetic encoder's field strength magnitude, if the chip exposes it

    # --- Controller setpoints + config setters ---
    "set_control_mode",  # controller.config.control_mode
    "set_position",  # controller.input_pos
    "set_velocity",  # controller.input_vel
    "set_torque",  # controller.input_torque
    "set_controller_config_input_mode",
    "set_controller_config_pos_gain",
    "set_controller_config_vel_gain",
    "set_controller_config_vel_integrator_gain",
    "set_controller_config_vel_integrator_limit",
    "set_controller_config_vel_limit",
    "set_controller_config_enable_vel_limit",
    "set_controller_config_enable_torque_mode_vel_limit",
    "set_controller_config_enable_overspeed_error",
    "set_controller_config_vel_ramp_rate",
    "set_controller_config_torque_ramp_rate",
    "set_controller_config_circular_setpoints",
    "set_controller_config_circular_setpoint_range",
    "set_controller_config_absolute_setpoints",
    "set_controller_config_homing_speed",
    "set_controller_config_input_filter_bandwidth",
    "set_controller_config_spinout_mechanical_power_bandwidth",
    "set_controller_config_spinout_electrical_power_bandwidth",
    "set_controller_config_spinout_mechanical_power_threshold",
    "set_controller_config_spinout_electrical_power_threshold",

    # --- Trajectory planner setters ---
    "set_traptraj_config_vel_limit",
    "set_traptraj_config_accel_limit",
    "set_traptraj_config_decel_limit",

    # --- Board / bus level setters and actions ---
    "set_board_config_dc_bus_undervoltage_trip_level",
    "set_board_config_dc_bus_overvoltage_trip_level",
    "set_board_config_dc_max_positive_current",
    "set_board_config_dc_max_negative_current",
    "set_board_config_max_regen_current",
    "set_board_config_inverter0_current_soft_max",
    "set_board_config_inverter0_current_hard_max",
    "set_board_config_inverter0_temp_limit_lower",
    "set_board_config_inverter0_temp_limit_upper",
    "set_board_config_inverter0_derating_start",
    "set_board_config_inverter0_current_soft_max_derated",
    "save_configuration",  # odrv0.save_configuration()
    "erase_configuration",  # odrv0.erase_configuration()
    "reboot",  # odrv0.reboot()
]
